"""The `defect_memory` MCP server — the thing that stops duplicate storms.

§10 names duplicate storms as a top failure mode and makes this server
mandatory. The memory outlives the run: it lives in one SQLite file under the
state root, because dedupe across *runs* is the whole point — a defect found
again next week must land on the ticket filed today, not beside it.

Similarity here is deterministic and offline on purpose. An embedding model
would make matching fuzzier and the system less explainable: two agents asking
the same question a minute apart must get the same answer, and a reviewer must
be able to say exactly why two reports were called the same defect. So the
score combines three things a human would also use — the structural fingerprint,
the code location, and the words — with fixed weights.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from qaas.envelope import DefectEnvelope, Location, _strip_line_number
from qaas.mcp.context import ToolContext, err, ok

MEMORY_DB = "memory.db"

# Weights sum to 1.0. The fingerprint is the strongest single signal because it
# is already a structural identity; prose is the weakest because two agents
# describing one defect share surprisingly few words.
W_FINGERPRINT = 0.40
W_LOCATION = 0.35
W_TOKENS = 0.25

# Below this, a candidate is not worth an agent's attention. Set so that a
# location match alone clears it (0.35) but prose overlap alone never does.
MIN_SIMILARITY = 0.35

# A different domain is strong evidence of a different defect, so it halves an
# otherwise convincing score rather than being another additive term.
CROSS_DOMAIN_PENALTY = 0.5

DEFAULT_LIMIT = 10

# Words that appear in nearly every defect report carry no discriminating
# signal; leaving them in makes every pair of reports look 30% alike.
_STOPWORDS = frozenset(
    """
    the and for with that this from when then than but not are was were will
    have has had does did doing any all its it's you your our their there here
    into onto over under also only just some more most much very can could
    should would may might must shall while which who whom whose what where why
    how because since about after before between during without within
    bug issue defect error problem fault failure broken breaks fails failing
    endpoint request response server client user users page screen app
    """.split()
)

# The two tables are templates over their own name because the key migration
# (`_KEYS`) has to create each one a second time under a scratch name and copy
# into it. One definition serves both, so a rebuilt table cannot drift from a
# fresh one.
#
# Keyed by (target, fingerprint). It was keyed by fingerprint alone, and the
# fingerprint does not include the target, so the `target` column filtered
# `search_similar` and nothing else: target A recorded `GET /v1/users` as
# SHOP-7, and in target B a different defect on the same endpoint was told
# "looks new" by `search_similar` and then "already tracked as SHOP-7. Add
# evidence there; do not file again" by `record`. One project's ticket
# suppressing another project's defect, persisted, in every future run.
_DEFECTS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    fingerprint         TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    domain              TEXT NOT NULL,
    defect_class        TEXT NOT NULL DEFAULT '',
    service             TEXT,
    endpoint            TEXT,
    ui_route            TEXT,
    paths               TEXT NOT NULL DEFAULT '[]',
    ticket_key          TEXT,
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    last_run_id         TEXT,
    resolved_at         TEXT,
    resolved_ticket_key TEXT,
    target              TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (target, fingerprint)
)"""

# Keyed with the target for the same reason. The column was always here and no
# read ever consulted it, so `search_similar` rendered one project's "could NOT
# be reproduced" or "was verified" beside another project's defect.
_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    fingerprint TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    run_id      TEXT NOT NULL,
    at          TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    agent       TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (target, fingerprint, run_id, outcome)
)"""

_SCHEMA = f"""
{_DEFECTS_TABLE.format(name="defects")};
CREATE INDEX IF NOT EXISTS defects_domain ON defects(domain);

-- What happened to a defect after it was reported. The table that turns memory
-- into learning: `defects` can say "seen before", and only this can say "and it
-- was wrong", "and the fix did not hold", "and it came back".
--
-- Written by the ROUTER and by `qaas score`, never by an agent. That is not
-- tidiness -- an agent that can write its own outcome can raise its own
-- precision without finding anything, which is the same failure mode as an
-- agent that can retire an entry from the golden ledger. Agents read this
-- through `search_similar` and cannot reach it any other way.
{_OUTCOMES_TABLE.format(name="outcomes")};
CREATE INDEX IF NOT EXISTS outcomes_fingerprint ON outcomes(fingerprint);
"""

#: Added after 0.0.1 shipped. SQLite has no `ADD COLUMN IF NOT EXISTS`, so these
#: are applied by reading `PRAGMA table_info` -- an existing `memory.db` migrates
#: in place on the next `connect`, and a fresh one gets them from the start.
_MIGRATIONS = {
    # Memory was one file per *state root*, with no target column and an
    # unfiltered `SELECT * FROM defects`. `qaas run --repo` clones every target
    # under that one root, so one project's memory answered another project's
    # questions -- and could suppress a real defect as "already tracked as
    # PROJ-N, do not file again", persisting that suppression into every future
    # run. `''` means "recorded before this column existed" and stays visible, so
    # no existing memory is orphaned.
    "defects": [("target", "TEXT NOT NULL DEFAULT ''")],
}

#: Primary keys changed after release, and the table each is declared by.
#: SQLite cannot alter a primary key, so these are applied by rebuilding the
#: table (`_rekey`) when `PRAGMA table_info` reports a different key. Adding the
#: `target` column alone left `fingerprint` the whole key, so the partition
#: existed for `search_similar` and for no write -- see `_DEFECTS_TABLE`.
_KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "defects": (("target", "fingerprint"), _DEFECTS_TABLE),
    "outcomes": (("target", "fingerprint", "run_id", "outcome"), _OUTCOMES_TABLE),
}

#: What `record_outcome` accepts. A closed set for the same reason `LedgerKind`
#: is one: `search_similar` renders these back to an agent, so they are a wire
#: format rather than labels.
OUTCOMES = frozenset(
    {
        "verified", "not_fixed", "regressed", "review_rejected", "held",
        "not_reproducible",
        # Written only by `qaas score`/`qaas sweep`, from
        # `Scorecard.regressions_on_planted` -- a finding that matched something
        # the golden ledger plants as correct-but-suspicious. The one outcome
        # that is a judgement on the *report* rather than on the defect, and the
        # reason it is safe is that it comes from a process with no agent in it,
        # measured against a ledger no agent may write.
        "false_positive",
    }
)

#: How an outcome reads to an agent. Written as a *consequence*, not as a label:
#: "held" alone says nothing an agent can act on, whereas "held, so it was never
#: filed" tells it what to do differently this time.
_OUTCOME_NOTES = {
    "verified": "a fix for this was verified in an earlier run",
    "not_fixed": "an earlier fix for this did NOT hold when verified",
    "regressed": "an earlier fix for this broke something else",
    "review_rejected": "an earlier fix for this was rejected in review",
    "held": "an earlier report of this was HELD, not filed — check the evidence bar",
    "not_reproducible": "an earlier report of this could NOT be reproduced",
    "false_positive": (
        "an earlier report of this was scored a FALSE POSITIVE against the golden "
        "ledger — the code it describes is correct. Read it again before reporting it"
    ),
}


def _target(ctx: "ToolContext") -> str:
    """Which target this memory row belongs to. Never a tool argument: an agent
    that can name its own partition can read another target's memory."""
    return str(getattr(ctx.config, "target", "") or "")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(root: Path | str) -> sqlite3.Connection:
    """Open (and, first time, create) the shared defect memory."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / MEMORY_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
    except BaseException:
        # A migration that rolled back raises out of here; the caller never gets
        # the connection to close, so close it rather than leak the handle.
        conn.close()
        raise
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a `memory.db` written by an older version up to this schema.

    Additive columns only, applied by inspecting what is actually there. The
    memory deliberately outlives a release -- dedupe across runs is the whole
    point of it -- so a schema change that orphaned an existing file would throw
    away the thing the file exists for.

    The one exception is a primary key (`_KEYS`), which SQLite cannot alter:
    that table is rebuilt, every row copied, `target = ''` rows included and
    unchanged. So the rebuild is the first migration here that drops a table,
    and it gets two protections the additive ones never needed: the whole of
    `_migrate` runs in one `BEGIN IMMEDIATE` transaction, and the file is copied
    to `memory.db.bak-<epoch>` first -- the name the one hand-made backup in
    this checkout's `.qaas/` already uses, and one `memory.db*` in
    `STATE_GITIGNORE` already ignores.

    Checked without a lock first, because `connect` runs on every tool call and
    the ordinary answer is "nothing to do"; checked again under the lock,
    because several agents open the memory at once and only one of them should
    migrate it.
    """
    if not _missing_columns(conn) and not _stale_keys(conn):
        return
    # Taken before the lock, not under it: `Connection.backup` from a
    # connection holding its own write transaction never finishes -- it waits
    # on a lock it holds itself.
    #
    # Never deleted afterwards, even when another process turns out to have
    # migrated first: the first copy taken is always from before any rebuild
    # committed, and a same-second second copy is skipped rather than written
    # over it, so cleaning up "our redundant one" could delete the only real one.
    if _stale_keys(conn):
        _backup(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, columns in _MIGRATIONS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # After the columns, so the copy has a `target` to carry across.
        for table in _stale_keys(conn):
            _rekey(conn, table)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    missing = []
    for table, columns in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing += [(table, name) for name, _ in columns if name not in have]
    return missing


def _primary_key(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The table's primary key as declared, in key order (`pk` is 1-based)."""
    info = [r for r in conn.execute(f"PRAGMA table_info({table})") if r["pk"]]
    return tuple(r["name"] for r in sorted(info, key=lambda r: r["pk"]))


def _stale_keys(conn: sqlite3.Connection) -> list[str]:
    return [table for table, (key, _) in _KEYS.items() if _primary_key(conn, table) != key]


def _rekey(conn: sqlite3.Connection, table: str) -> None:
    """Rebuild `table` under its current key. Must run inside a transaction.

    SQLite's documented procedure for a change ALTER cannot make: create the new
    table, copy the rows, drop the old table, rename the new one into place,
    recreate its indexes. A plain INSERT, not OR IGNORE: the new key is the old
    key plus `target`, so no two old rows can collide, and if they somehow did
    the right answer is a rollback, not a silently shorter memory.
    """
    _, template = _KEYS[table]
    scratch = f"{table}__rekey"
    indexes = [
        r["sql"]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
            "AND sql IS NOT NULL",
            (table,),
        )
    ]
    conn.execute(f"DROP TABLE IF EXISTS {scratch}")
    conn.execute(template.format(name=scratch))
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    columns = ", ".join(
        r["name"] for r in conn.execute(f"PRAGMA table_info({scratch})") if r["name"] in have
    )
    conn.execute(f"INSERT INTO {scratch} ({columns}) SELECT {columns} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {scratch} RENAME TO {table}")
    for sql in indexes:
        conn.execute(sql)


def _backup(conn: sqlite3.Connection) -> Path | None:
    """Copy the memory aside before a migration that drops a table.

    Best-effort. The transaction is what makes the rebuild safe against a crash;
    this is for a rebuild that commits and turns out wrong. A backup that cannot
    be written is not a reason to keep a schema that suppresses defects across
    projects, so failing here migrates anyway rather than refusing to open.
    """
    main = next((r["file"] for r in conn.execute("PRAGMA database_list") if r["name"] == "main"), "")
    if not main:
        return None  # in-memory: nothing on disk to lose
    source = Path(main)
    dest = source.with_name(f"{source.name}.bak-{int(time.time())}")
    if dest.exists():
        return None  # someone else's copy from this same second; not ours to replace
    try:
        copy = sqlite3.connect(dest)
        try:
            conn.backup(copy)
        finally:
            copy.close()
    except (sqlite3.Error, OSError):
        dest.unlink(missing_ok=True)
        return None
    return dest


# -- the write path Python owns ---------------------------------------------
#
# These three are module-level functions taking a root and plain values, not
# closures over a ToolContext, because the router has to call them and the
# router holds no agent. That separation is the design, not a convenience:
#
#   * `resolve` closes the regression loop. It was unreachable before -- the
#     REGRESSION branch fires only on `resolved_at`, the only writer was the
#     `mark_resolved` *tool*, and VERIFIER, the one agent that closes a ticket,
#     has no `defect_memory` server. So no shipped roster could ever report "this
#     was fixed and it came back", which is the single most valuable thing a
#     system with a memory can say. The fix is not to grant VERIFIER the server:
#     it is for the router to write the fact it already knows.
#
#   * `record_outcome` is how a run's judgements survive it. Held findings,
#     REQUEST_CHANGES and NOT_FIXED were all typed ledger entries that the router
#     read back *within* the run and nothing read afterwards.
#
# An agent can read these through `search_similar` and cannot write them at all.


def resolve(
    root: Path | str,
    fingerprint: str,
    ticket_key: str | None,
    run_id: str,
    *,
    target: str = "",
) -> bool:
    """Mark a defect fixed in `target`'s memory. Returns whether that target
    knew anything by that fingerprint.

    `target` is the run's (`''` for a run with no named target), the same value
    `record_outcome` is given. Unscoped, a VERIFIED in one project resolved every
    project's row with that fingerprint, so another project's next sighting of
    its own open defect was announced as "REGRESSION of <the first project's
    ticket>". A legacy `''` row is not touched from a named target: it may be
    another project's, and `record` no longer lets it speak for this one.
    """
    conn = connect(root)
    try:
        cursor = conn.execute(
            "UPDATE defects SET resolved_at = ?, resolved_ticket_key = COALESCE(?, ticket_key), "
            "last_run_id = ? WHERE target = ? AND fingerprint = ?",
            (_utcnow_iso(), ticket_key, run_id, target or "", fingerprint),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def record_outcome(
    root: Path | str,
    *,
    fingerprint: str,
    run_id: str,
    outcome: str,
    target: str = "",
    agent: str | None = None,
    detail: str = "",
) -> None:
    """Persist what became of one finding. Idempotent per (target, fingerprint,
    run, outcome): `INSERT OR REPLACE` against exactly that primary key."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome '{outcome}'; expected one of {sorted(OUTCOMES)}")
    conn = connect(root)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO outcomes "
            "(fingerprint, target, run_id, at, outcome, agent, detail) VALUES (?,?,?,?,?,?,?)",
            (fingerprint, target or "", run_id, _utcnow_iso(), outcome, agent, detail[:500]),
        )
        conn.commit()
    finally:
        conn.close()


def outcomes_for(
    root: Path | str, fingerprint: str, *, target: str | None = None
) -> list[dict[str, Any]]:
    """Everything this system has learned about one defect, newest first.

    With `target` (`''` included), only what `record_outcome` wrote under that
    target. Without one, every target's, each row carrying its `target` -- this
    is a reader for Python and for a person, and no agent reaches it. What an
    agent reads is `search_similar`, which is always scoped to its own target.
    """
    conn = connect(root)
    try:
        if target is None:
            rows = conn.execute(
                "SELECT * FROM outcomes WHERE fingerprint = ? ORDER BY at DESC", (fingerprint,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outcomes WHERE target = ? AND fingerprint = ? ORDER BY at DESC",
                (target, fingerprint),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# -- similarity -----------------------------------------------------------


def tokens(text: str) -> set[str]:
    """Content words of a report, lowercased and de-noised."""
    words = "".join(c if c.isalnum() else " " for c in text.lower()).split()
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _paths(raw: Any) -> set[str]:
    """Normalised path set. Line numbers are dropped for the same reason
    `DefectEnvelope.fingerprint` drops them: code moves, the defect does not."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return {_strip_line_number(p) for p in (raw or []) if p}


def location_score(query: dict[str, Any], row: dict[str, Any]) -> float:
    """How much of the location the two reports agree on.

    Only fields *both* sides supplied are compared — a query that omits
    `ui_route` should not be penalised against a row that has one, or every
    partially-specified search would score zero.
    """
    parts: list[float] = []
    for field in ("service", "endpoint", "ui_route"):
        a, b = (query.get(field) or "").strip().lower(), (row.get(field) or "").strip().lower()
        if a and b:
            parts.append(1.0 if a == b else 0.0)
    qp, rp = _paths(query.get("paths")), _paths(row.get("paths"))
    if qp and rp:
        parts.append(len(qp & rp) / len(qp | rp))
    return sum(parts) / len(parts) if parts else 0.0


def similarity(query: dict[str, Any], row: dict[str, Any]) -> float:
    """Deterministic 0..1 similarity between a search and a remembered defect."""
    fingerprint_match = 1.0 if query.get("fingerprint") and query["fingerprint"] == row.get("fingerprint") else 0.0
    location = location_score(query, row)
    prose = jaccard(
        tokens(f"{query.get('title', '')} {query.get('summary', '')}"),
        tokens(f"{row.get('title', '')} {row.get('summary', '')}"),
    )
    score = W_FINGERPRINT * fingerprint_match + W_LOCATION * location + W_TOKENS * prose
    if query.get("domain") and row.get("domain") and query["domain"] != row["domain"]:
        score *= CROSS_DOMAIN_PENALTY
    return round(score, 4)


def _probe_fingerprint(ctx: ToolContext, args: dict[str, Any]) -> str | None:
    """The structural fingerprint the searched-for defect *would* have.

    Built by constructing a throwaway envelope so the hash comes from
    `DefectEnvelope.fingerprint()` and cannot drift from the one stored on real
    envelopes. Needs the defect class, so it returns None when the caller only
    has a partial description — the other two signals still apply.
    """
    if not args.get("class"):
        return None
    try:
        probe = DefectEnvelope(
            run_id=ctx.store.run_id,
            discovered_by=ctx.agent.name,
            domain=args["domain"],
            **{"class": args["class"]},
            title=(args.get("title") or "probe")[:90],
            summary=args.get("summary") or "probe",
            severity="minor",
            confidence=0.5,
            location=Location(
                service=args.get("service"),
                endpoint=args.get("endpoint"),
                ui_route=args.get("ui_route"),
                paths=list(args.get("paths") or []),
            ),
        )
    except ValidationError:
        return None
    return probe.fingerprint()


#: How a legacy row reads to an agent. It is still shown, because the memory
#: deliberately outlives a release, and it is labelled, because it is the one
#: kind of match that may be another project's.
_LEGACY_NOTE = "recorded before targets were tracked, so it may belong to another project"


def _is_legacy(row: sqlite3.Row, target: str) -> bool:
    """A `target = ''` row, seen from a named target. From an unnamed one it is
    simply that target's own memory."""
    return bool(target) and row["target"] == ""


def _row_view(
    row: sqlite3.Row, score: float | None = None, *, legacy: bool = False
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "fingerprint": row["fingerprint"],
        "title": row["title"],
        "domain": row["domain"],
        "class": row["defect_class"],
        "ticket_key": row["ticket_key"],
        "occurrence_count": row["occurrence_count"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "resolved": row["resolved_at"] is not None,
        "location": {
            "service": row["service"],
            "endpoint": row["endpoint"],
            "ui_route": row["ui_route"],
            "paths": json.loads(row["paths"] or "[]"),
        },
        "legacy": legacy,
    }
    if score is not None:
        view["similarity"] = score
    return view


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["title", "summary", "domain"],
    "properties": {
        "title": {"type": "string", "description": "The candidate defect's title."},
        "summary": {"type": "string", "description": "The candidate defect's summary."},
        "domain": {
            "type": "string",
            "enum": ["architecture", "database", "api", "websocket", "frontend", "ux", "security", "performance"],
        },
        "class": {
            "type": "string",
            "enum": ["bug", "regression", "ux-friction", "tech-debt", "vulnerability", "perf-regression"],
            "description": "Supply it when you have it: it enables exact fingerprint matching.",
        },
        "service": {"type": "string"},
        "endpoint": {"type": "string", "description": "e.g. 'GET /v1/orders'"},
        "ui_route": {"type": "string", "description": "e.g. '/checkout/review'"},
        "paths": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative paths."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
}


def build_tools(ctx: ToolContext) -> list:
    """The defect memory tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """
    root = ctx.store.root

    @tool(
        "search_similar",
        "Search remembered defects for prior reports of this one. Call this BEFORE filing "
        "anything: a match means increment the existing ticket, not open a second one.",
        SEARCH_SCHEMA,
    )
    async def search_similar(args: dict[str, Any]) -> dict[str, Any]:
        query = dict(args)
        query["fingerprint"] = _probe_fingerprint(ctx, args)
        limit = int(args.get("limit") or DEFAULT_LIMIT)

        target = _target(ctx)
        conn = connect(root)
        try:
            # Scoped to this target, plus the legacy rows that predate the
            # column. Unscoped, a second project under the same state root -- the
            # ordinary case with `qaas run --repo` -- answered this project's
            # questions, and a close-enough match from an unrelated codebase came
            # back as "already tracked as PROJ-N, do not file again". A
            # suppression, persisted, repeating every run.
            rows = conn.execute(
                "SELECT * FROM defects WHERE target IN (?, '')", (target,)
            ).fetchall()
            # This target's outcomes only. This read was unscoped after the
            # defects read above was fixed, so a candidate from *this* project
            # was rendered with "an earlier report of this could NOT be
            # reproduced" learned in a different project -- a soft suppression
            # by the same route as the hard one.
            learned = {
                r["fingerprint"]: r
                for r in conn.execute(
                    "SELECT fingerprint, outcome, at, agent, detail FROM outcomes "
                    "WHERE target = ? ORDER BY at ASC",
                    (target,),
                )
            }
        finally:
            conn.close()

        # Once this target has recorded a fingerprint for itself, the legacy row
        # of the same fingerprint is superseded here: `record` created the scoped
        # row precisely because the legacy one could not be trusted to be ours.
        # Keyed by (target, fingerprint), both rows exist, and without this the
        # same defect came back as two candidates with two different histories.
        own = {r["fingerprint"] for r in rows if r["target"] == target}
        rows = [r for r in rows if r["target"] == target or r["fingerprint"] not in own]

        scored = [(similarity(query, dict(r)), r) for r in rows]
        matches = sorted(
            ((s, r) for s, r in scored if s >= MIN_SIMILARITY),
            key=lambda pair: (-pair[0], pair[1]["fingerprint"]),
        )[:limit]

        if not matches:
            return ok(
                f"No prior defect resembles '{args['title']}' "
                f"({len(rows)} in memory, none above {MIN_SIMILARITY}). This looks new.",
                candidates=[],
                searched=len(rows),
            )

        candidates = []
        for s, r in matches:
            view = _row_view(r, s, legacy=_is_legacy(r, target))
            # What the system learned about this defect *after* it was reported.
            # Without it memory can only say "seen before", which tells an agent
            # nothing about whether reporting it was right.
            outcome = learned.get(r["fingerprint"])
            view["last_outcome"] = dict(outcome) if outcome else None
            candidates.append(view)

        lines = []
        for c in candidates:
            line = (
                f"  {c['similarity']:.2f}  {c['ticket_key'] or 'unfiled'}  "
                f"x{c['occurrence_count']}  last seen {c['last_seen']}  {c['title']}"
            )
            if c["resolved"]:
                # Not "a recurrence is a regression" for a legacy row: `record`
                # will not call it one, because the fix may have been another
                # project's.
                line += "  [RESOLVED]" if c["legacy"] else "  [RESOLVED — a recurrence is a regression]"
            if c["legacy"]:
                line += f"\n      ({_LEGACY_NOTE}; check the ticket is this repository's before using it)"
            if c["last_outcome"]:
                line += f"\n      last outcome: {_OUTCOME_NOTES.get(c['last_outcome']['outcome'], c['last_outcome']['outcome'])}"
                if c["last_outcome"]["detail"]:
                    line += f" ({c['last_outcome']['detail']})"
            lines.append(line)
        return ok(
            f"{len(candidates)} prior defect(s) resemble this one:\n" + "\n".join(lines),
            candidates=candidates,
            searched=len(rows),
        )

    @tool(
        "fingerprint",
        "Return the structural fingerprint of an envelope you already emitted. "
        "Two reports of the same defect share it regardless of wording.",
        {
            "type": "object",
            "required": ["envelope_id"],
            "properties": {"envelope_id": {"type": "string"}},
        },
    )
    async def fingerprint(args: dict[str, Any]) -> dict[str, Any]:
        envelope = ctx.store.get_envelope(args["envelope_id"])
        if envelope is None:
            return err(f"No envelope '{args['envelope_id']}' in this run. Emit it first.")
        return ok(
            f"{envelope.fingerprint()} — {envelope.title}",
            envelope_id=envelope.id,
            fingerprint=envelope.fingerprint(),
        )

    @tool(
        "record",
        "Remember this defect so future runs can dedupe against it. A second record of a "
        "known fingerprint increments its occurrence count instead of duplicating it.",
        {
            "type": "object",
            "required": ["envelope_id"],
            "properties": {
                "envelope_id": {"type": "string"},
                "ticket_key": {"type": "string", "description": "The tracker key, if one was filed."},
            },
        },
    )
    async def record(args: dict[str, Any]) -> dict[str, Any]:
        # Gated like every comparable write: `record_reproduction` is
        # REPRODUCER's, `record_verdict` is VERIFIER's, `put_system_map` is
        # MAPPER's. This one mutates the store that outlives the run and was open
        # to any agent holding the server -- so a discovery agent could write
        # "already tracked as PROJ-N" against its own finding and suppress it in
        # every future run.
        if not ctx.agent.policy.may_create_tickets:
            return err(
                f"{ctx.agent.name} may not record into the cross-run defect memory "
                "(§8.1: TRIAGE files, and filing is what makes a defect worth "
                "remembering). Emit your finding as an envelope."
            )
        envelope = ctx.store.get_envelope(args["envelope_id"])
        if envelope is None:
            return err(f"No envelope '{args['envelope_id']}' in this run. Emit it first.")

        fp = envelope.fingerprint()
        ticket_key = args.get("ticket_key") or None
        now = _utcnow_iso()
        target = _target(ctx)

        conn = connect(root)
        try:
            # This target's row and no other. It was `WHERE fingerprint = ?`, and
            # the fingerprint carries no target, so after target A recorded
            # `GET /v1/users` as SHOP-7, a different defect on that endpoint in
            # target B was answered "already tracked as SHOP-7. Add evidence
            # there; do not file again" -- one sentence after `search_similar`,
            # which *was* scoped, had told the same agent it looked new.
            row = conn.execute(
                "SELECT * FROM defects WHERE target = ? AND fingerprint = ?", (target, fp)
            ).fetchone()
            if row is None:
                # A legacy row (recorded before targets were tracked) is never
                # counted as this target's: it may be another project's, and
                # counting it would bring back the "do not file again"
                # suppression and the false REGRESSION with it. So this target
                # gets a row of its own, occurrence 1, and the legacy row is left
                # exactly as it was -- it is still shown, labelled, by
                # `search_similar`, and still the answer for a run with no named
                # target. Its ticket is named below as a lead to check, not an
                # instruction: the agent holding this tool also holds the
                # tracker, and every ticket carries its repository's label.
                legacy = None
                if target:
                    legacy = conn.execute(
                        "SELECT * FROM defects WHERE target = '' AND fingerprint = ?", (fp,)
                    ).fetchone()
                conn.execute(
                    "INSERT INTO defects (fingerprint, title, summary, domain, defect_class, "
                    "service, endpoint, ui_route, paths, ticket_key, occurrence_count, "
                    "first_seen, last_seen, last_run_id, target) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                    (
                        fp,
                        envelope.title,
                        envelope.summary,
                        envelope.domain.value,
                        envelope.defect_class.value,
                        envelope.location.service,
                        envelope.location.endpoint,
                        envelope.location.ui_route,
                        json.dumps(sorted(envelope.location.paths)),
                        ticket_key,
                        now,
                        now,
                        ctx.store.run_id,
                        target,
                    ),
                )
                conn.commit()
                ctx.store.log(
                    "defect_memory", agent=ctx.agent.name, action="new",
                    fingerprint=fp, envelope_id=envelope.id, ticket_key=ticket_key,
                )
                text = (
                    f"New defect recorded. Fingerprint {fp}, occurrence 1"
                    + (f", ticket {ticket_key}." if ticket_key else ", no ticket yet.")
                )
                legacy_match = None
                if legacy is not None:
                    legacy_match = {
                        "ticket_key": legacy["ticket_key"],
                        "occurrence_count": legacy["occurrence_count"],
                        "last_seen": legacy["last_seen"],
                        "resolved": legacy["resolved_at"] is not None,
                    }
                    text += (
                        f" The same fingerprint was also {_LEGACY_NOTE}, under "
                        f"{legacy['ticket_key'] or 'no ticket'}"
                        + (" (marked resolved)" if legacy["resolved_at"] else "")
                        + ". It was not counted as this target's. If that ticket carries "
                        "this repository's label, link to it; otherwise file as new."
                    )
                return ok(
                    text,
                    fingerprint=fp,
                    occurrence_count=1,
                    ticket_key=ticket_key,
                    regression=False,
                    first_time=True,
                    legacy_match=legacy_match,
                )

            was_resolved = row["resolved_at"] is not None
            count = row["occurrence_count"] + 1
            # resolved_at is cleared unconditionally: a defect that is back is
            # open again, whatever the tracker still says.
            conn.execute(
                "UPDATE defects SET occurrence_count = ?, last_seen = ?, last_run_id = ?, "
                "ticket_key = COALESCE(?, ticket_key), resolved_at = NULL "
                "WHERE target = ? AND fingerprint = ?",
                (count, now, ctx.store.run_id, ticket_key, target, fp),
            )
            conn.commit()
            known_ticket = ticket_key or row["ticket_key"]
        finally:
            conn.close()

        if was_resolved:
            closed_under = row["resolved_ticket_key"] or row["ticket_key"] or "an earlier ticket"
            ctx.store.log(
                "regression", agent=ctx.agent.name, fingerprint=fp,
                envelope_id=envelope.id, resolved_ticket_key=closed_under,
            )
            return ok(
                f"REGRESSION: this is a regression of {closed_under}. The defect with "
                f"fingerprint {fp} was marked resolved on {row['resolved_at']} and has come "
                f"back (occurrence {count}). File it as a regression and link it to "
                f"{closed_under} — do not close it as a duplicate.",
                fingerprint=fp,
                occurrence_count=count,
                ticket_key=known_ticket,
                regression=True,
                regression_of=closed_under,
                previously_resolved_at=row["resolved_at"],
                first_time=False,
            )

        ctx.store.log(
            "defect_memory", agent=ctx.agent.name, action="occurrence",
            fingerprint=fp, envelope_id=envelope.id, occurrence_count=count,
        )
        return ok(
            f"Known defect: occurrence {count} of fingerprint {fp}"
            + (f", already tracked as {known_ticket}. Add evidence there; do not file again."
               if known_ticket else ". Still unfiled."),
            fingerprint=fp,
            occurrence_count=count,
            ticket_key=known_ticket,
            regression=False,
            first_time=False,
        )

    @tool(
        "get_occurrences",
        "How often this fingerprint has been seen, when, and under which ticket.",
        {
            "type": "object",
            "required": ["fingerprint"],
            "properties": {"fingerprint": {"type": "string"}},
        },
    )
    async def get_occurrences(args: dict[str, Any]) -> dict[str, Any]:
        target = _target(ctx)
        conn = connect(root)
        try:
            # This target's history, falling back to a legacy row -- the same
            # visibility `search_similar` has, so a candidate it showed can be
            # looked up. It was `WHERE fingerprint = ?`, which returned whichever
            # project had recorded the fingerprint and presented that project's
            # count and ticket as this one's.
            row = conn.execute(
                "SELECT * FROM defects WHERE target = ? AND fingerprint = ?",
                (target, args["fingerprint"]),
            ).fetchone()
            if row is None and target:
                row = conn.execute(
                    "SELECT * FROM defects WHERE target = '' AND fingerprint = ?",
                    (args["fingerprint"],),
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            return err(
                f"Fingerprint {args['fingerprint']} is not in defect memory. "
                "Either it is genuinely new, or you have the wrong fingerprint."
            )
        legacy = _is_legacy(row, target)
        state = "resolved" if row["resolved_at"] else "open"
        return ok(
            f"{row['occurrence_count']} occurrence(s), first {row['first_seen']}, "
            f"last {row['last_seen']}, ticket {row['ticket_key'] or 'none'} ({state})."
            + (f" This was {_LEGACY_NOTE}." if legacy else ""),
            **_row_view(row, legacy=legacy),
        )

    @tool(
        "mark_resolved",
        "Mark this defect resolved. A later recurrence is then reported as a regression "
        "rather than a duplicate — which is the difference between reopening and ignoring it.",
        {
            "type": "object",
            "required": ["fingerprint"],
            "properties": {
                "fingerprint": {"type": "string"},
                "ticket_key": {"type": "string", "description": "The ticket it was resolved under."},
            },
        },
    )
    async def mark_resolved(args: dict[str, Any]) -> dict[str, Any]:
        if not ctx.agent.policy.may_transition_tickets:
            return err(
                f"{ctx.agent.name} may not mark a defect resolved. A recurrence after "
                "this is reported as a REGRESSION, so writing it wrongly suppresses "
                "the highest-value signal this system has."
            )
        fp = args["fingerprint"]
        ticket_key = args.get("ticket_key") or None
        now = _utcnow_iso()
        target = _target(ctx)
        conn = connect(root)
        try:
            # This target's row only, never a legacy one: resolving is what makes
            # the next sighting a REGRESSION, and it resolved every project's row
            # with this fingerprint, so a fix in one project announced a false
            # regression in another.
            row = conn.execute(
                "SELECT * FROM defects WHERE target = ? AND fingerprint = ?", (target, fp)
            ).fetchone()
            if row is None:
                return err(
                    f"Fingerprint {fp} is not in defect memory for this target; nothing to "
                    "resolve. Record it first if this target has seen it."
                )
            resolved_under = ticket_key or row["ticket_key"]
            conn.execute(
                "UPDATE defects SET resolved_at = ?, resolved_ticket_key = ?, "
                "ticket_key = COALESCE(?, ticket_key) WHERE target = ? AND fingerprint = ?",
                (now, resolved_under, ticket_key, target, fp),
            )
            conn.commit()
        finally:
            conn.close()
        ctx.store.log(
            "defect_memory", agent=ctx.agent.name, action="resolved",
            fingerprint=fp, ticket_key=resolved_under,
        )
        return ok(
            f"Marked {fp} resolved under {resolved_under or 'no ticket'}. "
            "If it comes back, it will be reported as a regression.",
            fingerprint=fp,
            resolved_at=now,
            resolved_ticket_key=resolved_under,
        )

    return [search_similar, fingerprint, record, get_occurrences, mark_resolved]


def build(ctx: ToolContext):
    """Construct the defect memory MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="defect_memory", version="1.0.0", tools=build_tools(ctx))
