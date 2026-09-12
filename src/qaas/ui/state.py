"""The dashboard's read model: a run's ledger folded into one view object.

Built **incrementally**. `apply()` takes one `LedgerEntry` and updates the view,
so a finished run (replay the file) and a live run (replay, then keep applying
tailed entries) go through exactly the same code. There is no second renderer
for history that can drift from the live one.

Deliberately free of web imports. `qaas dashboard` needs the `[ui]` extra, but
this module does not -- which is what lets the read model be tested in the
default offline suite whether or not starlette is installed.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from qaas import trace
from qaas.config import AgentSpec
from qaas.envelope import DefectEnvelope, Severity
from qaas.store import DEFAULT_ROOT, LedgerEntry, LedgerKind, RunStore, list_runs

#: The router's phases, in the order `Router.run` calls them (`router.py:233-242`).
PHASES: tuple[str, ...] = ("map", "discover", "reproduce", "file", "verify", "report")

#: An agent's `layer` maps onto a phase -- except `triage`, which holds *both*
#: REPRODUCER and TRIAGE. Splitting that one by name is not a special case
#: smuggled in: the router dispatches those two phases by name too
#: (`specs.get("REPRODUCER")`, `specs.get("TRIAGE")` -- router.py:344,372),
#: whereas discovery and reporting dispatch by layer.
_LAYER_PHASE = {
    "control": "map",
    "discovery": "discover",
    "remediation": "verify",
    "reporting": "report",
}
_NAME_PHASE = {"REPRODUCER": "reproduce", "TRIAGE": "file"}


def phase_of(agent: str, layer: str | None) -> str | None:
    """Which phase an agent belongs to, or None if nothing here can say.

    None rather than a guess. Ledgers on disk name agents from an earlier roster
    (CARTOGRAPHER, FORGE, VAULT...) that no config knows about, and inventing a
    phase for them would light a phase rail that never ran.
    """
    if agent in _NAME_PHASE:
        return _NAME_PHASE[agent]
    return _LAYER_PHASE.get(layer or "")


def read_ledger(store: RunStore) -> list[LedgerEntry]:
    """Every entry in the ledger, skipping any line that will not parse.

    `trace.read_ledger` goes through `store.ledger()`, which validates strictly
    and raises. That is right for a finished run and wrong here: the dashboard
    reads runs that are still being written, and the last line of a live ledger
    can be half a JSON object. Same tolerance `trace.tail` already applies
    (trace.py:116-122), for the same reason.
    """
    path = store.ledger_path
    if not path.exists():
        return []
    entries: list[LedgerEntry] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(line))
        except Exception:
            continue
    return entries


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class AgentView:
    """One agent's state within a run.

    `invocations` is not decoration: REPRODUCER runs once per finding and FIXER
    once per review round trip, so cost and turns accumulate across several
    dispatches of the same name.
    """

    name: str
    layer: str | None = None
    model: str | None = None
    status: str = "queued"  # queued | running | done | failed | skipped
    invocations: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    findings: int = 0
    tool_calls: int = 0
    denials: int = 0
    last_tool: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    reason: str | None = None  # why it was skipped

    @property
    def phase(self) -> str | None:
        return phase_of(self.name, self.layer)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "layer": self.layer,
            "model": self.model,
            "status": self.status,
            "phase": self.phase,
            "invocations": self.invocations,
            "turns": self.turns,
            "cost_usd": round(self.cost_usd, 4),
            "duration_s": round(self.duration_s, 2),
            "findings": self.findings,
            "tool_calls": self.tool_calls,
            "denials": self.denials,
            "last_tool": self.last_tool,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "error": self.error,
            "reason": self.reason,
        }


@dataclass
class FindingView:
    """An envelope, summarised. The full document is fetched on demand."""

    id: str
    discovered_by: str
    discovered_at: datetime | None
    domain: str
    defect_class: str
    severity: str
    severity_rank: int
    title: str
    confidence: float
    paths: list[str] = field(default_factory=list)
    evidence_uris: list[str] = field(default_factory=list)
    repro_status: str = "unattempted"
    fileable: bool = False
    held_reason: str = ""
    fingerprint: str | None = None
    ticket_key: str | None = None

    @classmethod
    def of(cls, env: DefectEnvelope, *, min_confidence: float) -> "FindingView":
        ok, reason = env.is_fileable(min_confidence)
        return cls(
            id=env.id,
            discovered_by=env.discovered_by,
            discovered_at=env.discovered_at,
            domain=str(env.domain),
            defect_class=str(env.defect_class),
            severity=str(env.severity),
            severity_rank=env.severity.rank,
            title=env.title,
            confidence=env.confidence,
            paths=list(env.location.paths),
            evidence_uris=[e.uri for e in env.evidence],
            repro_status=str(env.reproduction.status),
            fileable=ok,
            held_reason=reason,
            fingerprint=env.dedupe.fingerprint,
            ticket_key=env.jira.key,
        )

    def to_json(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["discovered_at"] = _iso(self.discovered_at)
        return d


@dataclass
class DenialView:
    """One refused tool call -- what the guardrail stopped, and why."""

    at: datetime
    agent: str
    tool: str
    reason: str
    via: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "at": _iso(self.at),
            "agent": self.agent,
            "tool": self.tool,
            "reason": self.reason,
            "via": self.via,
            "args": self.args,
        }


@dataclass
class TicketView:
    key: str
    project: str | None = None
    severity: str | None = None
    status: str | None = None
    verdict: str | None = None
    reopens: int = 0
    envelope_id: str | None = None
    restricted: bool = False

    def to_json(self) -> dict[str, Any]:
        return self.__dict__.copy()


#: How many rendered ledger lines a view keeps for a newly-connected client. A
#: real run logs ~2400 `tool_call` lines against ~150 of everything else
#: (trace.py:231-233), so this is a scrollback, not the ledger -- anything older
#: is fetched from `/api/runs/<id>/events`, which reads the file.
RECENT_MAX = 500


@dataclass
class RunView:
    """Everything the dashboard knows about one run.

    Hold one per run, not one per browser tab: the file is read once and every
    connected client is served from this object.
    """

    run_id: str
    store: RunStore
    specs: Mapping[str, AgentSpec] = field(default_factory=dict)
    min_confidence: float = 0.6

    mode: str | None = None
    started: datetime | None = None
    finished: datetime | None = None
    completed: bool = False
    stopped_early: str | None = None
    target_root: str | None = None
    target_sha: str | None = None
    target_dirty: bool | None = None
    target_branch: str | None = None
    #: Almost always None -- no shipped run mode sets a dollar cap, deliberately
    #: (system.yaml:11-15). Do not build a progress bar on it.
    budget_usd: float | None = None
    #: Always set, and the only honest denominator this view has.
    wall_clock_s: int | None = None

    roster: list[str] = field(default_factory=list)
    agents: dict[str, AgentView] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    findings: list[FindingView] = field(default_factory=list)
    denials: list[DenialView] = field(default_factory=list)
    tickets: dict[str, TicketView] = field(default_factory=dict)
    escalations: list[str] = field(default_factory=list)
    recent: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=RECENT_MAX))
    seq: int = 0

    # -- derived ----------------------------------------------------------

    @property
    def target_name(self) -> str | None:
        """The run's own target, never today's config.

        `run_started` records a path and a sha but not the profile name, and
        `system.yaml` may have been repointed since. The basename of the path the
        run actually used is the honest answer.
        """
        return Path(self.target_root).name if self.target_root else None

    @property
    def elapsed_s(self) -> float:
        if self.started is None:
            return 0.0
        end = self.finished or datetime.now(timezone.utc)
        return max(0.0, (end - self.started).total_seconds())

    @property
    def cost_usd(self) -> float:
        """Summed from `agent_finished`, one line per invocation.

        Not from `results/*.json`. `put_result` writes a file and logs a line
        together (store.py:255-271), so for any run written since result files
        were named per-invocation the two agree exactly. Where they disagree the
        ledger is the survivor, because it is append-only and the files were
        not: one run on disk here has fifteen `agent_finished` lines for FORGE
        totalling $26.06 and a single `FORGE.json` holding $1.75 -- a run from
        before the `-NN` suffix existed, clobbered down to its last dispatch.
        That is the under-reporting the comment at store.py:256-259 records, and
        summing the ledger is what reads back through it.
        """
        return sum(a.cost_usd for a in self.agents.values())

    @property
    def phase_status(self) -> dict[str, str]:
        """pending | active | done | absent, per phase.

        `absent` is load-bearing: `pr-check` has no VERIFIER and no REPORTER, so
        its verify and report phases never run. Showing them as forever-pending
        would read as a stalled run.
        """
        out: dict[str, str] = {}
        for phase in PHASES:
            members = [a for a in self.agents.values() if a.phase == phase]
            if not members:
                out[phase] = "absent"
            elif any(a.status == "running" for a in members):
                out[phase] = "active"
            elif any(a.status in ("done", "failed", "skipped") for a in members):
                out[phase] = "done" if all(
                    a.status in ("done", "failed", "skipped") for a in members
                ) else "active"
            else:
                out[phase] = "pending"
        return out

    @property
    def phase(self) -> str:
        if self.completed:
            return "done"
        status = self.phase_status
        active = [p for p in PHASES if status[p] == "active"]
        if active:
            return active[-1]
        done = [p for p in PHASES if status[p] == "done"]
        if done:
            return done[-1]
        # Every phase absent means no agent in this run could be placed -- a
        # ledger from the earlier roster, where no name matches a spec. Saying
        # "map" there would light the first phase of a run that never mapped.
        if all(v == "absent" for v in status.values()):
            return "unknown"
        return PHASES[0]

    # -- incremental build ------------------------------------------------

    def _agent(self, name: str) -> AgentView:
        view = self.agents.get(name)
        if view is None:
            spec = self.specs.get(name)
            # An agent the config has never heard of is rendered, not dropped.
            # Every ledger written before the roster was renamed names agents
            # that no longer exist; a dashboard that crashed on them could not
            # open the runs most worth looking at.
            view = AgentView(
                name=name,
                layer=getattr(spec, "layer", None),
                model=getattr(spec, "model", None),
            )
            self.agents[name] = view
        return view

    def apply(self, entry: LedgerEntry) -> None:
        kind, detail, agent = entry.kind, entry.detail, entry.agent
        self.counts[str(kind)] = self.counts.get(str(kind), 0) + 1
        self.seq += 1
        self.recent.append(
            {
                "seq": self.seq,
                "at": _iso(entry.at),
                "agent": agent,
                "kind": str(kind),
                "detail": detail,
                "text": trace.describe(entry),
            }
        )

        if kind == LedgerKind.RUN_STARTED:
            # A resumed run (`qaas run --run-id`) appends a *second*
            # `run_started` to the same ledger, under whatever mode the resume
            # asked for. The latest one wins on every field, `started`
            # included: the router restarts the wall clock across a resume and
            # deliberately does not carry it over (router.py:120-124), so
            # measuring elapsed from the first start would show a run against a
            # cap it is not being held to.
            self.started = entry.at
            self.mode = detail.get("mode")
            self.roster = list(detail.get("agents") or [])
            self.budget_usd = detail.get("budget_usd")
            self.wall_clock_s = detail.get("wall_clock_s")
            self.target_root = detail.get("target_root")
            self.target_sha = detail.get("target_sha")
            self.target_dirty = detail.get("target_dirty")
            self.target_branch = detail.get("target_branch")
            for name in self.roster:
                self._agent(name)

        elif kind == LedgerKind.RUN_FINISHED:
            self.finished = entry.at
            self.completed = True
            self.stopped_early = detail.get("stopped_early")
            for view in self.agents.values():
                # "queued" on a run that is over is a card waiting for something
                # that already happened. The roster named it and the run ended
                # without dispatching it -- usually a wall-clock or budget stop.
                if view.status == "queued":
                    view.status = "never_ran"

        elif kind == LedgerKind.AGENT_STARTED and agent:
            view = self._agent(agent)
            view.status = "running"
            view.invocations += 1
            view.started_at = view.started_at or entry.at
            view.model = detail.get("model") or view.model

        elif kind == LedgerKind.AGENT_FINISHED and agent:
            view = self._agent(agent)
            view.status = "failed" if detail.get("error") else "done"
            view.finished_at = entry.at
            view.cost_usd += float(detail.get("cost_usd") or 0.0)
            view.turns += int(detail.get("num_turns") or 0)
            view.error = detail.get("error") or view.error
            if view.started_at:
                view.duration_s = (entry.at - view.started_at).total_seconds()

        elif kind == LedgerKind.AGENT_ERROR and agent:
            view = self._agent(agent)
            view.status = "failed"
            view.error = detail.get("error")

        elif kind == LedgerKind.TOOL_CALL and agent:
            view = self._agent(agent)
            view.tool_calls += 1
            view.last_tool = detail.get("tool")

        elif kind == LedgerKind.DENIAL:
            name = agent or str(detail.get("agent") or "?")
            self._agent(name).denials += 1
            self.denials.append(
                DenialView(
                    at=entry.at,
                    agent=name,
                    tool=str(detail.get("tool") or "?"),
                    reason=str(detail.get("reason") or ""),
                    via=detail.get("via"),
                    args=detail.get("args") or {},
                )
            )

        elif kind == LedgerKind.ENVELOPE:
            if agent:
                self._agent(agent).findings += 1
            self._add_finding(str(detail.get("envelope_id") or ""), entry, detail)

        elif kind == LedgerKind.SKIPPED:
            reason = detail.get("reason")
            for name in detail.get("agents") or ([agent] if agent else []):
                view = self._agent(str(name))
                if view.status == "queued":
                    view.status = "skipped"
                    view.reason = reason

        elif kind == LedgerKind.TICKET:
            self._apply_ticket(detail)

        elif kind == LedgerKind.VERDICT:
            key = detail.get("ticket_key")
            if key:
                self._ticket(str(key)).verdict = detail.get("verdict")

        elif kind == LedgerKind.VERIFIED:
            key = detail.get("ticket_key")
            if key:
                ticket = self._ticket(str(key))
                ticket.verdict = "VERIFIED"
                ticket.reopens = int(detail.get("reopens") or ticket.reopens)

        elif kind == LedgerKind.REOPENED:
            key = detail.get("ticket_key")
            if key:
                self._ticket(str(key)).reopens = int(detail.get("attempt") or 0)

        elif kind == LedgerKind.ESCALATION:
            reason = detail.get("reason")
            if reason:
                self.escalations.append(f"{agent + ': ' if agent else ''}{reason}")

    def _ticket(self, key: str) -> TicketView:
        ticket = self.tickets.get(key)
        if ticket is None:
            ticket = TicketView(key=key)
            self.tickets[key] = ticket
        return ticket

    def _apply_ticket(self, detail: dict[str, Any]) -> None:
        key = detail.get("key")
        if not key:
            return
        ticket = self._ticket(str(key))
        action = detail.get("action")
        if action == "created":
            ticket.project = detail.get("project")
            ticket.severity = detail.get("severity")
            ticket.envelope_id = detail.get("envelope_id")
            ticket.restricted = bool(detail.get("restricted"))
            ticket.status = ticket.status or "created"
        elif action == "transitioned":
            ticket.status = detail.get("status") or ticket.status
        # An envelope filed after the fact should show its key on the finding.
        for finding in self.findings:
            if finding.id and finding.id == ticket.envelope_id:
                finding.ticket_key = ticket.key

    def _add_finding(self, envelope_id: str, entry: LedgerEntry, detail: dict[str, Any]) -> None:
        if not envelope_id or any(f.id == envelope_id for f in self.findings):
            return
        env = None
        try:
            env = self.store.get_envelope(envelope_id)
        except Exception:
            env = None
        if env is not None:
            self.findings.append(FindingView.of(env, min_confidence=self.min_confidence))
            return
        # The ledger line exists but the document does not (or will not parse).
        # The five fields the line itself carries still beat showing nothing.
        severity = str(detail.get("severity") or Severity.MINOR)
        try:
            rank = Severity(severity).rank
        except ValueError:
            rank = len(Severity)
        self.findings.append(
            FindingView(
                id=envelope_id,
                discovered_by=entry.agent or "?",
                discovered_at=entry.at,
                domain=str(detail.get("domain") or ""),
                defect_class="",
                severity=severity,
                severity_rank=rank,
                title="(envelope document not readable)",
                confidence=float(detail.get("confidence") or 0.0),
                fingerprint=detail.get("fingerprint"),
            )
        )

    # -- serialisation ----------------------------------------------------

    def to_json(self, *, recent: int = RECENT_MAX) -> dict[str, Any]:
        findings = sorted(self.findings, key=lambda f: (f.severity_rank, -f.confidence))
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started": _iso(self.started),
            "finished": _iso(self.finished),
            "completed": self.completed,
            "stopped_early": self.stopped_early,
            "target_name": self.target_name,
            "target_root": self.target_root,
            "target_sha": self.target_sha,
            "target_dirty": self.target_dirty,
            "target_branch": self.target_branch,
            "budget_usd": self.budget_usd,
            "wall_clock_s": self.wall_clock_s,
            "elapsed_s": round(self.elapsed_s, 1),
            "cost_usd": round(self.cost_usd, 4),
            "roster": self.roster,
            "phase": self.phase,
            "phase_status": self.phase_status,
            "phases": list(PHASES),
            "agents": {n: a.to_json() for n, a in self.agents.items()},
            "counts": self.counts,
            "findings": [f.to_json() for f in findings],
            "denials": [d.to_json() for d in self.denials],
            "tickets": {k: t.to_json() for k, t in self.tickets.items()},
            "escalations": self.escalations,
            "recent": list(self.recent)[-recent:],
            "seq": self.seq,
        }


# -- loading ---------------------------------------------------------------


def load(
    store: RunStore,
    specs: Mapping[str, AgentSpec] | None = None,
    *,
    min_confidence: float = 0.6,
    entries: Iterable[LedgerEntry] | None = None,
) -> RunView:
    """Replay a ledger into a view. The same `apply` the live tail calls."""
    view = RunView(
        run_id=store.run_id,
        store=store,
        specs=dict(specs or {}),
        min_confidence=min_confidence,
    )
    for entry in entries if entries is not None else read_ledger(store):
        view.apply(entry)
    return view


def is_live(store: RunStore) -> bool:
    """A run is live until its ledger carries `run_finished`.

    There is no "current run" pointer anywhere, and there deliberately is not:
    the ledger is the only thing that knows, and it knows without being told.
    """
    if not store.ledger_path.exists():
        return False
    return '"kind":"run_finished"' not in store.ledger_path.read_text(
        encoding="utf-8", errors="replace"
    )


def pick_run(root: Path | str = DEFAULT_ROOT, run_id: str | None = None) -> str | None:
    """Which run to open: the one asked for, else the live one, else the newest."""
    if run_id:
        return run_id
    ids = list_runs(root)
    if not ids:
        return None
    for candidate in ids:  # newest first
        if is_live(RunStore(candidate, root=root, create=False)):
            return candidate
    return ids[0]


def list_runs_summary(root: Path | str = DEFAULT_ROOT, limit: int = 50) -> list[dict[str, Any]]:
    """One row per run for the run rail. Cheap: no full ledger parse.

    `qaas runs` reports an envelope count and an invocation count and stops
    short of the cost, though `total_cost_usd()` is right there (cli.py:860-878).
    The rail shows it.
    """
    rows: list[dict[str, Any]] = []
    for run_id in list_runs(root)[:limit]:
        store = RunStore(run_id, root=root, create=False)
        results = []
        try:
            results = store.results()
        except Exception:
            pass
        # The *last* `run_started`, not the first: a resumed run has two, and the
        # rail disagreeing with the header it opens is worse than either answer.
        entries = read_ledger(store)
        starts = [e for e in entries if e.kind == LedgerKind.RUN_STARTED]
        first = starts[-1] if starts else (entries[0] if entries else None)
        rows.append(
            {
                "run_id": run_id,
                "mode": (first.detail.get("mode") if first else None),
                "started": _iso(first.at) if first else None,
                "live": is_live(store),
                "cost_usd": round(sum(r.cost_usd for r in results), 4),
                "agents": len({r.agent for r in results}),
                "findings": len(list((store.dir / "envelopes").glob("*.json")))
                if (store.dir / "envelopes").exists()
                else 0,
            }
        )
    return rows


def system_map(root: Path | str = DEFAULT_ROOT, version: str | None = None) -> dict[str, Any] | None:
    from qaas.store import SystemMapStore

    try:
        return SystemMapStore(root).get(version)
    except Exception:
        return None


def artifact_names(store: RunStore) -> list[dict[str, Any]]:
    directory = store.dir / "artifacts"
    if not directory.exists():
        return []
    return sorted(
        (
            {"name": p.name, "bytes": p.stat().st_size, "suffix": p.suffix.lstrip(".")}
            for p in directory.iterdir()
            if p.is_file()
        ),
        key=lambda d: d["name"],
    )


def envelope_json(store: RunStore, envelope_id: str) -> dict[str, Any] | None:
    env = store.get_envelope(envelope_id)
    return json.loads(env.to_json()) if env is not None else None
