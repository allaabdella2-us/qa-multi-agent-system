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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS defects (
    fingerprint         TEXT PRIMARY KEY,
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
    resolved_ticket_key TEXT
);
CREATE INDEX IF NOT EXISTS defects_domain ON defects(domain);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(root: Path | str) -> sqlite3.Connection:
    """Open (and, first time, create) the shared defect memory."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / MEMORY_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


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


def _row_view(row: sqlite3.Row, score: float | None = None) -> dict[str, Any]:
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

        conn = connect(root)
        try:
            rows = conn.execute("SELECT * FROM defects").fetchall()
        finally:
            conn.close()

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

        candidates = [_row_view(r, s) for s, r in matches]
        lines = [
            f"  {c['similarity']:.2f}  {c['ticket_key'] or 'unfiled'}  "
            f"x{c['occurrence_count']}  last seen {c['last_seen']}  {c['title']}"
            + ("  [RESOLVED — a recurrence is a regression]" if c["resolved"] else "")
            for c in candidates
        ]
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
        envelope = ctx.store.get_envelope(args["envelope_id"])
        if envelope is None:
            return err(f"No envelope '{args['envelope_id']}' in this run. Emit it first.")

        fp = envelope.fingerprint()
        ticket_key = args.get("ticket_key") or None
        now = _utcnow_iso()

        conn = connect(root)
        try:
            row = conn.execute("SELECT * FROM defects WHERE fingerprint = ?", (fp,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO defects (fingerprint, title, summary, domain, defect_class, "
                    "service, endpoint, ui_route, paths, ticket_key, occurrence_count, "
                    "first_seen, last_seen, last_run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
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
                    ),
                )
                conn.commit()
                ctx.store.log(
                    "defect_memory", agent=ctx.agent.name, action="new",
                    fingerprint=fp, envelope_id=envelope.id, ticket_key=ticket_key,
                )
                return ok(
                    f"New defect recorded. Fingerprint {fp}, occurrence 1"
                    + (f", ticket {ticket_key}." if ticket_key else ", no ticket yet."),
                    fingerprint=fp,
                    occurrence_count=1,
                    ticket_key=ticket_key,
                    regression=False,
                    first_time=True,
                )

            was_resolved = row["resolved_at"] is not None
            count = row["occurrence_count"] + 1
            # resolved_at is cleared unconditionally: a defect that is back is
            # open again, whatever the tracker still says.
            conn.execute(
                "UPDATE defects SET occurrence_count = ?, last_seen = ?, last_run_id = ?, "
                "ticket_key = COALESCE(?, ticket_key), resolved_at = NULL "
                "WHERE fingerprint = ?",
                (count, now, ctx.store.run_id, ticket_key, fp),
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
        conn = connect(root)
        try:
            row = conn.execute(
                "SELECT * FROM defects WHERE fingerprint = ?", (args["fingerprint"],)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return err(
                f"Fingerprint {args['fingerprint']} is not in defect memory. "
                "Either it is genuinely new, or you have the wrong fingerprint."
            )
        state = "resolved" if row["resolved_at"] else "open"
        return ok(
            f"{row['occurrence_count']} occurrence(s), first {row['first_seen']}, "
            f"last {row['last_seen']}, ticket {row['ticket_key'] or 'none'} ({state}).",
            **_row_view(row),
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
        fp = args["fingerprint"]
        ticket_key = args.get("ticket_key") or None
        now = _utcnow_iso()
        conn = connect(root)
        try:
            row = conn.execute("SELECT * FROM defects WHERE fingerprint = ?", (fp,)).fetchone()
            if row is None:
                return err(f"Fingerprint {fp} is not in defect memory; nothing to resolve.")
            resolved_under = ticket_key or row["ticket_key"]
            conn.execute(
                "UPDATE defects SET resolved_at = ?, resolved_ticket_key = ?, "
                "ticket_key = COALESCE(?, ticket_key) WHERE fingerprint = ?",
                (now, resolved_under, ticket_key, fp),
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
