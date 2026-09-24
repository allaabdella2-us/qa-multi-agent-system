"""Run state on disk: the ledger, the artifact store, the versioned system map.

Everything an agent produces lands here. The ledger is the audit trail the
architecture asks for (§2, §8) — every agent invocation, every cost, every
guardrail denial, appended and never rewritten.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from qaas.envelope import DefectEnvelope

DEFAULT_ROOT = Path(".qaas")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerKind(StrEnum):
    """Every kind of line the ledger may contain.

    This was a bare `str` whose comment named 7 of the 29 kinds actually
    written, which left the ledger unreadable by anything but grep: nothing
    could enumerate what a run might contain, and a typo at a `store.log()`
    call site invented a 29th kind that no reader would ever look for. It is a
    closed set now, so a misspelling fails at the write instead of vanishing.

    `StrEnum` and not a plain `Enum` on purpose: members *are* their strings, so
    `entry.kind == "denial"` still holds, `model_dump_json()` still writes
    `"kind":"denial"`, and every ledger already on disk still parses. Adding a
    kind means adding a member here -- deliberately a visible act, because the
    router reads several of these back for control flow (`_latest_verdict`,
    `_latest_review`, `_branch_written_since`), so a rename is a breaking
    change to a wire format, not a rename.
    """

    # run lifecycle (router)
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    SKIPPED = "skipped"
    ESCALATION = "escalation"
    #: The provider stopped accepting work -- a session/usage quota, not a
    #: failure of any agent. Its own kind rather than another `escalation`,
    #: because the two mean opposite things to whoever reads the ledger back:
    #: an escalation says "a human must look at this finding", and this says
    #: "nothing was wrong, run it again when the quota resets". Repurposing
    #: `escalation` would also bury it -- the run that taught us this
    #: (`run-20260919T152757-4c8c37`, $56.58) wrote eleven of them, one per
    #: agent that walked into the same wall, and none of them said the word.
    QUOTA_EXHAUSTED = "quota_exhausted"
    #: The provider's limit said when it lifts and the run is waiting for it,
    #: then retrying the agent it stopped. Not `quota_exhausted`, which means
    #: the run gave up and needs a resume.
    QUOTA_WAIT = "quota_wait"

    # agent lifecycle (runner, store)
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    AGENT_ERROR = "agent_error"
    SKILLS_MISSING = "skills_missing"

    # tool traffic and its refusals (guardrails, registry)
    TOOL_CALL = "tool_call"
    TOOL_ERROR = "tool_error"
    DENIAL = "denial"
    STOP_BLOCKED = "stop_blocked"
    CONTRACT_UNMET = "contract_unmet"

    # findings and the evidence behind them
    ENVELOPE = "envelope"
    REPRODUCTION = "reproduction"
    CONTRACT_TEST = "contract_test"
    SYSTEM_MAP = "system_map"
    DEFECT_MEMORY = "defect_memory"
    REGRESSION = "regression"

    # the file/verify/fix loop
    TICKET = "ticket"
    VERDICT = "verdict"
    VERIFIED = "verified"
    REOPENED = "reopened"
    REVIEW = "review"
    REVIEW_ROUND_TRIP = "review_round_trip"
    #: A human's answer to an escalation. Added rather than folded into
    #: `review`: the router reads it back for control flow (whether a ticket is
    #: worked at all, and what FIXER and REVIEWER are told), so it is a wire
    #: format, and `review` means "an agent judged a fix" -- a different fact
    #: with a different author. Written only by `qaas answer`, from a process
    #: with no agent in it; no `write_paths` and no MCP tool reach it.
    HUMAN_DECISION = "human_decision"

    # side effects on the world outside the run
    VCS = "vcs"
    ENV = "env"
    DRY_RUN = "dry_run"


class HumanDecision(StrEnum):
    """What a human may answer an escalation with.

    Closed for the reason `LedgerKind` is closed: the router reads these back
    and acts on them, so a misspelling must fail where it is typed rather than
    become a decision nothing recognises and every run silently ignores.

    Two members, and the third was deliberately dropped. `wont_fix` was drafted
    and cut because the router cannot tell it from `hold` -- both stop the
    ticket being worked, and a vocabulary carrying two words the reader cannot
    distinguish teaches the next run nothing while asking the human to choose
    between them. "We are not fixing this" is a `hold` whose note says so, and
    the note is the part that survives.
    """

    #: Carry on. The note is the answer, and it reaches FIXER and REVIEWER
    #: verbatim on the next fix cycle -- CORVID-7, where REVIEWER escalated a
    #: correct fix on a product question ("applying it exposes a UI regression
    #: filed as another ticket; ship now or hold?") and nothing could answer.
    PROCEED = "proceed"
    #: Stop working this ticket. The next fix cycle skips it before VERIFIER
    #: costs anything -- QAAS-31, where REVIEWER escalated because the fix lies
    #: outside FIXER's `write_paths`, which makes it human work by construction.
    #: Answering `proceed` later replaces it; the latest answer stands.
    HOLD = "hold"


class LedgerEntry(BaseModel):
    """One line in the run ledger. Append-only."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=_utcnow)
    kind: LedgerKind
    agent: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """What one agent invocation cost and produced."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    subtype: str = "success"
    cost_usd: float = 0.0
    #: True when `cost_usd` is a conservative estimate rather than a measured
    #: figure -- an agent whose stream dropped before it reported one. Flagged so
    #: `qaas show` and the dashboard can mark it instead of presenting an
    #: invented number as measured.
    cost_estimated: bool = False
    num_turns: int = 0
    duration_s: float = 0.0
    envelope_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class RunStore:
    """The filesystem home of a single run.

    Layout:
        .qaas/runs/<run_id>/ledger.jsonl
                            envelopes/<envelope_id>.json
                            artifacts/<name>
                            results/<AGENT>.json
        .qaas/system-map/<version>.json   (shared across runs)
        .qaas/system-map/latest           (pointer file)
    """

    def __init__(self, run_id: str, root: Path | str = DEFAULT_ROOT, *, create: bool = True):
        self.run_id = run_id
        self.root = Path(root)
        self.dir = self.root / "runs" / run_id
        # `create=False` for the read-only commands. Constructing a store used to
        # mkdir unconditionally, so `qaas show <typo>` left a permanent empty run
        # on disk that then appeared in `qaas runs` forever -- a reader that
        # writes, and the one thing an audit trail must not have.
        if create:
            for sub in ("envelopes", "artifacts", "results"):
                (self.dir / sub).mkdir(parents=True, exist_ok=True)
        #: (agent, scope) -> files it has written this run. See `touched_files`.
        self._touched: dict[tuple[str, str | None], set[str]] = {}
        #: (agent, scope) -> lines it has committed. See `committed_lines`.
        self._committed_lines: dict[tuple[str, str | None], int] = {}
        #: Lines the last `ledger()` scan could not parse. See `ledger`.
        self.unreadable_lines = 0
        #: Envelope and result files the last scan could not parse. See `envelopes`.
        self.unreadable_files = 0
        #: agent -> per-run tallies. See `counters`.
        self._counters: dict[str, dict[str, int]] = {}

    @classmethod
    def new(cls, root: Path | str = DEFAULT_ROOT, prefix: str = "run") -> "RunStore":
        run_id = f"{prefix}-{_utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        return cls(run_id, root)

    # -- ledger -----------------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return self.dir / "ledger.jsonl"

    def log(self, kind: LedgerKind | str, agent: str | None = None, **detail: Any) -> LedgerEntry:
        # `str` stays in the signature because ~60 call sites pass a literal and
        # reading `store.log("denial", ...)` at the call site beats reading
        # `store.log(LedgerKind.DENIAL, ...)`. Pydantic converts and, crucially,
        # rejects: an unknown kind raises here rather than appending a line no
        # reader will ever ask for.
        entry = LedgerEntry(kind=kind, agent=agent, detail=detail)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(entry.model_dump_json() + "\n")
        return entry

    def ledger(
        self, kind: LedgerKind | str | None = None, *, strict: bool = False
    ) -> Iterator[LedgerEntry]:
        """Entries in order, tolerating a line that does not parse.

        Tolerant by default because of when the intolerant version failed. A run
        killed mid-`log` -- ^C, an OOM, a full disk -- leaves a truncated last
        line, and `model_validate_json` raised out of *every* reader built on
        this: `qaas show`, `qaas trace`, `trace.summarise` and all nine dashboard
        routes. A crashed run is precisely when the audit trail is worth
        something, and it was the one run whose ledger could not be opened.

        A skipped line is not swallowed silently -- it lands in
        `unreadable_lines`, so a reader that cares can say "this trail is short
        by two lines" rather than quietly presenting a shortened history as a
        complete one. `strict=True` is for a caller that would rather fail than
        read a partial history. No new `LedgerKind` for this: a corrupt line is
        a property of the file, not an event in the run.
        """
        if not self.ledger_path.exists():
            return
        skipped = 0
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = LedgerEntry.model_validate_json(line)
            except ValidationError:
                if strict:
                    raise
                skipped += 1
                continue
            if kind is None or entry.kind == kind:
                yield entry
        self.unreadable_lines = skipped

    # -- envelopes --------------------------------------------------------

    def put_envelope(self, envelope: DefectEnvelope) -> Path:
        """Persist an envelope, stamping its fingerprint if absent."""
        if envelope.dedupe.fingerprint is None:
            envelope = envelope.with_fingerprint()
        path = self.dir / "envelopes" / f"{envelope.id}.json"
        _write_atomic(path, envelope.to_json())
        self.log(
            "envelope",
            agent=envelope.discovered_by,
            envelope_id=envelope.id,
            domain=envelope.domain.value,
            severity=envelope.severity.value,
            confidence=envelope.confidence,
            fingerprint=envelope.dedupe.fingerprint,
        )
        return path

    def envelopes(self) -> list[DefectEnvelope]:
        """Every readable envelope. A file that does not parse is skipped and counted.

        Tolerant for the reason `ledger` is. This raised on the first bad file,
        and every phase of the router, `run_agent` itself and every reader call
        it -- so one envelope truncated by a crash mid-write, or written by a
        release with a different schema, failed every resume of that run in the
        same place, forever. It is written atomically now, which closes the
        first cause; the tolerance is for the files already on disk.
        """
        found: list[DefectEnvelope] = []
        bad = 0
        for path in sorted((self.dir / "envelopes").glob("*.json")):
            try:
                found.append(DefectEnvelope.from_json(path.read_text(encoding="utf-8")))
            except (ValidationError, ValueError, OSError):
                bad += 1
        self.unreadable_files = bad
        return found

    def get_envelope(self, envelope_id: str) -> DefectEnvelope | None:
        """One envelope by id, or None. The id is agent-supplied, so it is checked.

        `record_reproduction`, `fingerprint`, `record` and `create_issue` all
        pass an agent's `envelope_id` straight here, and it was joined into a
        path unchecked: `../../<other-run>/envelopes/<id>` read another run's
        envelope, which `record_reproduction` then wrote into this one.
        """
        if not _ENVELOPE_ID.fullmatch(str(envelope_id)):
            return None
        path = self.dir / "envelopes" / f"{envelope_id}.json"
        if not path.exists():
            return None
        try:
            return DefectEnvelope.from_json(path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError):
            return None

    def counters(self, agent: str) -> dict[str, int]:
        """Per-agent tallies that must span the whole run, not one dispatch.

        Held here for the same reason `touched_files` is, and it is the same bug
        one file over: `ToolContext.counters` lived on a context the router
        rebuilds for *every* `_dispatch`, so `max_findings_per_agent_run` and
        `max_tickets_per_run` were per-invocation caps wearing per-run names.
        REPRODUCER runs once per finding and FIXER once per review round trip, so
        each of them got a fresh allowance every time -- and on a resumed run
        every cap in the system started again from zero.

        That last clause stayed true after the move, because a resume is a new
        process and a new store: the tally lived in memory, so a run capped at
        three tickets filed three more when it was resumed. It is seeded from
        disk on first use -- the tickets this agent created, from the ledger,
        and the findings it emitted, from the envelopes.
        """
        if agent not in self._counters:
            self._counters[agent] = self._spent_so_far(agent)
        return self._counters[agent]

    def _spent_so_far(self, agent: str) -> dict[str, int]:
        """What earlier processes of this run already counted against `agent`."""
        spent: dict[str, int] = {}
        tickets = sum(
            1 for e in self.ledger("ticket")
            if e.agent == agent and e.detail.get("action") == "created"
        )
        # Envelopes, not `envelope` ledger lines: `put_envelope` logs every
        # write, and stamping a ticket key or a reproduction rewrites the file.
        envelopes = sum(1 for e in self.envelopes() if e.discovered_by == agent)
        if tickets:
            spent["tickets"] = tickets
        if envelopes:
            spent["envelopes"] = envelopes
        return spent

    def touched_files(self, agent: str, scope: str | None = None) -> set[str]:
        """The distinct files one agent has written for one piece of work (§8.2).

        Held here rather than on `ToolContext` because a context is built per
        dispatch, and a FIXER/REVIEWER round trip on one ticket is several
        dispatches that must share one budget.

        Keyed by `scope` as well as by agent, because §8.2 bounds *a diff* --
        "the diff touches fewer than N files" -- and not a run. Keyed by agent
        alone, the budget was shared across every ticket in the run: after
        ticket A's fix touched four files, ticket B's FIXER got one edit and was
        refused, and every later ticket escalated as "wider than the envelope"
        -- in `fix-cycle --from-board`, the mode that exists to work ten. The
        router scopes FIXER by ticket key; `None` is the whole run.
        """
        return self._touched.setdefault((agent, scope), set())

    def committed_lines(self, agent: str, scope: str | None = None) -> int:
        """Lines one agent has committed for one piece of work -- the other half
        of §8.2's envelope. Scoped like `touched_files`."""
        return self._committed_lines.get((agent, scope), 0)

    def add_committed_lines(self, agent: str, scope: str | None, lines: int) -> int:
        total = self.committed_lines(agent, scope) + lines
        self._committed_lines[(agent, scope)] = total
        return total

    # -- artifacts --------------------------------------------------------

    def _artifact_path(self, name: str) -> tuple[str, Path]:
        """A flat, contained filename for an agent-supplied artifact name.

        `resolve_artifact` does resolve-then-contain, which is the right shape.
        These two did string replacement instead — `/` and `..` to `_` — which is
        a denylist, and denylists are wrong here for the ordinary reason: a
        backslash was not in it, and neither is anything else nobody thought of.
        The name is agent-supplied, so this keeps the flattening (an artifact
        store is deliberately one directory deep) and then *checks* the result
        rather than trusting the substitution.
        """
        flat = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".") or "artifact"
        base = (self.dir / "artifacts").resolve()
        path = (base / flat).resolve()
        if path.parent != base:
            raise ValueError(f"artifact name escapes the store: {name!r}")
        return flat, path

    def put_artifact(self, name: str, content: str | bytes) -> str:
        """Store evidence and return the artifact:// uri that references it.

        A name already taken by *different* content gets a counter rather than
        clobbering it. Names are agent-supplied and agents converge on the
        obvious one, so two findings calling their screenshot `orders.png` used
        to resolve to one file -- and the first envelope's `artifact://` uri then
        pointed at the second finding's evidence. Evidence that silently belongs
        to another defect is worse than no evidence: `is_fileable` still passes,
        and a human reads a ticket whose proof is of something else.

        Identical content keeps the same name, so an agent re-storing the same
        thing is idempotent rather than accumulating copies.
        """
        safe, path = self._artifact_path(name)
        blob = content.encode() if isinstance(content, str) else content
        if path.exists():
            try:
                if path.read_bytes() != blob:
                    safe, path = self._unique_artifact_path(safe)
            except OSError:
                safe, path = self._unique_artifact_path(safe)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return f"artifact://{self.run_id}/{safe}"

    def _unique_artifact_path(self, flat: str) -> tuple[str, Path]:
        """`orders.png` -> `orders-02.png`, first free number wins."""
        stem, dot, suffix = flat.rpartition(".")
        stem, suffix = (stem, f".{suffix}") if dot else (flat, "")
        for n in range(2, 1000):
            candidate = f"{stem}-{n:02d}{suffix}"
            path = (self.dir / "artifacts" / candidate).resolve()
            if not path.exists():
                return candidate, path
        raise ValueError(f"too many artifacts named like {flat!r}")

    def copy_artifact(self, name: str, source: Path | str) -> str:
        safe, path = self._artifact_path(name)
        shutil.copy2(source, path)
        return f"artifact://{self.run_id}/{safe}"

    def resolve_artifact(self, uri: str) -> Path:
        """artifact://<run_id>/<name> -> a real path. Raises if it escapes the store."""
        if not uri.startswith("artifact://"):
            raise ValueError(f"not an artifact uri: {uri}")
        run_id, _, name = uri[len("artifact://"):].partition("/")
        path = (self.root / "runs" / run_id / "artifacts" / name).resolve()
        base = (self.root / "runs" / run_id / "artifacts").resolve()
        if not path.is_relative_to(base):
            raise ValueError(f"artifact uri escapes the store: {uri}")
        return path

    # -- per-agent results ------------------------------------------------

    def put_result(self, result: AgentResult) -> None:
        # One file per invocation, not per agent. REPRODUCER runs once per finding and
        # FIXER once per review round trip, so a per-agent filename silently
        # keeps only the last one — and the persisted cost of a run then
        # under-reports by however much the repeated agents actually spent.
        existing = len(list((self.dir / "results").glob(f"{result.agent}-*.json")))
        path = self.dir / "results" / f"{result.agent}-{existing + 1:02d}.json"
        _write_atomic(path, result.model_dump_json(indent=2))
        self.log(
            "agent_finished",
            agent=result.agent,
            subtype=result.subtype,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            envelopes=len(result.envelope_ids),
            error=result.error,
        )

    def results(self) -> list[AgentResult]:
        """Every readable result; a truncated file is skipped rather than fatal.

        `total_cost_usd` reads these for a resume's `already_spent` and
        `qaas show` reads them for a summary, and one file cut short by a kill
        made both raise for exactly the run someone needed to look at.
        """
        found: list[AgentResult] = []
        for path in sorted((self.dir / "results").glob("*.json")):
            try:
                found.append(AgentResult.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValidationError, ValueError, OSError):
                continue
        return found

    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results())


class SystemMapStore:
    """Versioned Mapper output, shared across runs.

    Agents pin a map version for the length of a run so a bad map cannot
    half-propagate mid-run (§10, context poisoning).
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT, *, create: bool = True):
        self.dir = Path(root) / "system-map"
        # `create=False` for readers, mirroring `RunStore`, which carries a
        # comment about having removed exactly this: a reader that writes. `qaas
        # map` and the dashboard construct one of these to look, and an
        # unconditional mkdir left a `.qaas/system-map/` in whatever directory
        # the operator happened to be standing in. `get`, `latest_version` and
        # `versions` all already degrade to None/[] on a missing directory.
        if create:
            self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, payload: dict[str, Any]) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        # The suffix is not decoration: a bare second-resolution timestamp lets
        # two maps written in the same second collide, which would silently
        # rewrite a version another run had already pinned.
        version = f"{_utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        path = self.dir / f"{version}.json"
        if path.exists():
            raise RuntimeError(f"system map version {version} already exists")
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp = self.dir / "latest.tmp"
        tmp.write_text(version, encoding="utf-8")
        os.replace(tmp, self.dir / "latest")
        return version

    def latest_version(self) -> str | None:
        pointer = self.dir / "latest"
        return pointer.read_text(encoding="utf-8").strip() if pointer.exists() else None

    def get(self, version: str | None = None) -> dict[str, Any] | None:
        """One map version, or None. A version that is not a plain name is refused.

        The dashboard's `?version=` reached here unchecked, so
        `?version=../../secret` returned any `.json` file the user could read.
        """
        version = version or self.latest_version()
        if not version or not _MAP_VERSION.fullmatch(version):
            return None
        path = (self.dir / f"{version}.json").resolve()
        if path.parent != self.dir.resolve():
            return None
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def versions(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))


#: An envelope id is a uuid; the router and the servers only ever mint those.
_ENVELOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
#: `20260920T232352-3acbe3`, or any other plain name -- never a path.
_MAP_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file and `os.replace`, so a reader -- or a crash --
    sees the old file or the new one, never half of the new one."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def list_runs(root: Path | str = DEFAULT_ROOT) -> list[str]:
    runs = Path(root) / "runs"
    return sorted((p.name for p in runs.iterdir() if p.is_dir()), reverse=True) if runs.exists() else []
