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

from pydantic import BaseModel, ConfigDict, Field

from qaas.envelope import DefectEnvelope

DEFAULT_ROOT = Path(".qaas")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerKind(StrEnum):
    """Every kind of line the ledger may contain.

    This was a bare `str` whose comment named 7 of the 28 kinds actually
    written, which left the ledger unreadable by anything but grep: nothing
    could enumerate what a run might contain, and a typo at a `store.log()`
    call site invented a 29th kind that no reader would ever look for. It is a
    closed set now, so a misspelling fails at the write instead of vanishing.

    `StrEnum` and not a plain `Enum` on purpose: members *are* their strings, so
    `entry.kind == "denial"` still holds, `model_dump_json()` still writes
    `"kind":"denial"`, and every ledger already on disk still parses. Adding a
    kind means adding a member here -- deliberately a visible act, because the
    conductor reads several of these back for control flow (`_latest_verdict`,
    `_latest_review`, `_branch_written_since`), so a rename is a breaking
    change to a wire format, not a rename.
    """

    # run lifecycle (conductor)
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    SKIPPED = "skipped"
    ESCALATION = "escalation"

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

    # side effects on the world outside the run
    VCS = "vcs"
    ENV = "env"
    DRY_RUN = "dry_run"


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

    def __init__(self, run_id: str, root: Path | str = DEFAULT_ROOT):
        self.run_id = run_id
        self.root = Path(root)
        self.dir = self.root / "runs" / run_id
        for sub in ("envelopes", "artifacts", "results"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

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

    def ledger(self, kind: LedgerKind | str | None = None) -> Iterator[LedgerEntry]:
        if not self.ledger_path.exists():
            return
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = LedgerEntry.model_validate_json(line)
            if kind is None or entry.kind == kind:
                yield entry

    # -- envelopes --------------------------------------------------------

    def put_envelope(self, envelope: DefectEnvelope) -> Path:
        """Persist an envelope, stamping its fingerprint if absent."""
        if envelope.dedupe.fingerprint is None:
            envelope = envelope.with_fingerprint()
        path = self.dir / "envelopes" / f"{envelope.id}.json"
        path.write_text(envelope.to_json(), encoding="utf-8")
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
        paths = sorted((self.dir / "envelopes").glob("*.json"))
        return [DefectEnvelope.from_json(p.read_text(encoding="utf-8")) for p in paths]

    def get_envelope(self, envelope_id: str) -> DefectEnvelope | None:
        path = self.dir / "envelopes" / f"{envelope_id}.json"
        return DefectEnvelope.from_json(path.read_text(encoding="utf-8")) if path.exists() else None

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
        """Store evidence and return the artifact:// uri that references it."""
        safe, path = self._artifact_path(name)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return f"artifact://{self.run_id}/{safe}"

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
        # One file per invocation, not per agent. FORGE runs once per finding and
        # MENDER once per review round trip, so a per-agent filename silently
        # keeps only the last one — and the persisted cost of a run then
        # under-reports by however much the repeated agents actually spent.
        existing = len(list((self.dir / "results").glob(f"{result.agent}-*.json")))
        path = self.dir / "results" / f"{result.agent}-{existing + 1:02d}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
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
        paths = sorted((self.dir / "results").glob("*.json"))
        return [AgentResult.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]

    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results())


class SystemMapStore:
    """Versioned Cartographer output, shared across runs.

    Agents pin a map version for the length of a run so a bad map cannot
    half-propagate mid-run (§10, context poisoning).
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT):
        self.dir = Path(root) / "system-map"
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, payload: dict[str, Any]) -> str:
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
        version = version or self.latest_version()
        if not version:
            return None
        path = self.dir / f"{version}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def versions(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))


def list_runs(root: Path | str = DEFAULT_ROOT) -> list[str]:
    runs = Path(root) / "runs"
    return sorted((p.name for p in runs.iterdir() if p.is_dir()), reverse=True) if runs.exists() else []
