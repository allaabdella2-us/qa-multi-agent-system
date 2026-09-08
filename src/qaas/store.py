"""Run state on disk: the ledger, the artifact store, the versioned system map.

Everything an agent produces lands here. The ledger is the audit trail the
architecture asks for (§2, §8) — every agent invocation, every cost, every
guardrail denial, appended and never rewritten.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from qaas.envelope import DefectEnvelope

DEFAULT_ROOT = Path(".qaas")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LedgerEntry(BaseModel):
    """One line in the run ledger. Append-only."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=_utcnow)
    kind: str  # run_started | agent_started | agent_finished | denial | envelope | escalation | run_finished
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

    def log(self, kind: str, agent: str | None = None, **detail: Any) -> LedgerEntry:
        entry = LedgerEntry(kind=kind, agent=agent, detail=detail)
        with self.ledger_path.open("a") as fh:
            fh.write(entry.model_dump_json() + "\n")
        return entry

    def ledger(self, kind: str | None = None) -> Iterator[LedgerEntry]:
        if not self.ledger_path.exists():
            return
        for line in self.ledger_path.read_text().splitlines():
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
        path.write_text(envelope.to_json())
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
        return [DefectEnvelope.from_json(p.read_text()) for p in paths]

    def get_envelope(self, envelope_id: str) -> DefectEnvelope | None:
        path = self.dir / "envelopes" / f"{envelope_id}.json"
        return DefectEnvelope.from_json(path.read_text()) if path.exists() else None

    # -- artifacts --------------------------------------------------------

    def put_artifact(self, name: str, content: str | bytes) -> str:
        """Store evidence and return the artifact:// uri that references it."""
        safe = name.replace("/", "_").replace("..", "_")
        path = self.dir / "artifacts" / safe
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return f"artifact://{self.run_id}/{safe}"

    def copy_artifact(self, name: str, source: Path | str) -> str:
        safe = name.replace("/", "_").replace("..", "_")
        shutil.copy2(source, self.dir / "artifacts" / safe)
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
        path.write_text(result.model_dump_json(indent=2))
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
        return [AgentResult.model_validate_json(p.read_text()) for p in paths]

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
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp = self.dir / "latest.tmp"
        tmp.write_text(version)
        os.replace(tmp, self.dir / "latest")
        return version

    def latest_version(self) -> str | None:
        pointer = self.dir / "latest"
        return pointer.read_text().strip() if pointer.exists() else None

    def get(self, version: str | None = None) -> dict[str, Any] | None:
        version = version or self.latest_version()
        if not version:
            return None
        path = self.dir / f"{version}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def versions(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))


def list_runs(root: Path | str = DEFAULT_ROOT) -> list[str]:
    runs = Path(root) / "runs"
    return sorted((p.name for p in runs.iterdir() if p.is_dir()), reverse=True) if runs.exists() else []
