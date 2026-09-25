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
import re
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from qaas import trace
from qaas.config import AgentSpec
from qaas.envelope import DefectEnvelope, Severity
from qaas.quota import is_quota_error
from qaas.store import DEFAULT_ROOT, LedgerEntry, LedgerKind, RunStore, list_runs

#: The router's phases, in the order `Router.run` calls them (`router.py:233-242`).
PHASES: tuple[str, ...] = (
    "map", "discover", "synthesise", "reproduce", "file", "verify", "report",
)

#: An agent's `layer` maps onto a phase -- except `triage`, which holds *both*
#: REPRODUCER and TRIAGE. Splitting that one by name is not a special case
#: smuggled in: the router dispatches those two phases by name too
#: (`specs.get("REPRODUCER")`, `specs.get("TRIAGE")` -- router.py:344,372),
#: whereas discovery and reporting dispatch by layer.
_LAYER_PHASE = {
    "control": "map",
    "discovery": "discover",
    "synthesis": "synthesise",
    "remediation": "verify",
    "reporting": "report",
}
_NAME_PHASE = {"REPRODUCER": "reproduce", "TRIAGE": "file"}

#: Agent statuses that mean "this one is not going to move again".
#: `never_ran` was missing, and `RUN_FINISHED` rewrites every still-`queued`
#: agent to it -- so a run that stopped early with one agent finished and one
#: never started fell through to `active` and the phase pulsed forever on a run
#: that had been over for an hour.
_TERMINAL = ("done", "failed", "skipped", "never_ran", "rate_limited", "interrupted")

#: Statuses that belong to a process. A new `run_started` means that process is
#: gone, so a card still showing one of these is showing something that stopped.
_IN_FLIGHT = ("running", "waiting")


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
    return _parse_lines(path.read_text(encoding="utf-8", errors="replace"))


def _parse_lines(text: str) -> list[LedgerEntry]:
    entries: list[LedgerEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entries.append(LedgerEntry.model_validate_json(line))
        except Exception:
            continue
    return entries


def replay(store: RunStore) -> tuple[list[LedgerEntry], int]:
    """Every whole line in the ledger, and the byte offset just past the last one.

    The offset is what a live tail must start from, and it has to come from
    *this* read. The watcher used to replay the file and then `stat()` it for
    the tail's start, so a line appended between the two was behind the offset
    and in neither half -- and half of a line being written when the replay ran
    was skipped by the replay as unparseable and skipped again by the tail,
    which started past it. Both losses were permanent: the damaged view was the
    one cached in `dash.watchers` and served to every tab until the process
    exited. Reading the bytes once and stopping at the last newline makes the
    two halves meet exactly, whatever is appended in between.
    """
    path = store.ledger_path
    try:
        data = path.read_bytes()
    except OSError:
        return [], 0
    end = data.rfind(b"\n") + 1
    return _parse_lines(data[:end].decode("utf-8", errors="replace")), end


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
    #: queued | running | waiting | done | failed | rate_limited | interrupted
    #: | skipped | never_ran. `waiting` is a provider limit being waited out;
    #: `rate_limited` is a run that stopped on one -- neither is this agent
    #: failing, and neither is drawn as if it were.
    status: str = "queued"
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
    reason: str | None = None  # why it was skipped, or is waiting
    #: When a provider limit this agent is waiting on lifts.
    waiting_until: datetime | None = None
    #: How the last invocation that *finished* ended. REPRODUCER runs once per
    #: finding and a resume can outlive a killed one; the card falls back to
    #: this rather than to "running" forever.
    settled: str | None = None

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
            "waiting_until": _iso(self.waiting_until),
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
    #: Not finished, and not being written either: the run was killed before it
    #: could log `run_finished` (see `trace.is_stale`). Without it such a run
    #: rendered as "live" with its clock ticking, forever.
    interrupted: bool = False
    #: When the last entry was written. An interrupted run's clock stops here
    #: rather than running on to now.
    last_at: datetime | None = None
    #: Bytes of the ledger this view has applied, when it was built by reading
    #: the file -- where a live tail must pick up. See `replay`.
    offset: int | None = None
    #: Set when this view is a finished run being played back (`/replay`). Its
    #: clock is the last replayed line's, not the wall's -- the run happened
    #: hours ago, and measuring to now would show every agent running for hours.
    replay_speed: int | None = None

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
        stopped = self.last_at if (self.interrupted or self.replay_speed) else None
        end = self.finished or stopped or datetime.now(timezone.utc)
        return max(0.0, (end - self.started).total_seconds())

    @property
    def live(self) -> bool:
        return not self.completed and not self.interrupted

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
            elif any(a.status in _IN_FLIGHT for a in members):
                out[phase] = "active"
            elif any(a.status in _TERMINAL for a in members):
                out[phase] = "done" if all(a.status in _TERMINAL for a in members) else "active"
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
        self.last_at = entry.at
        # A line arriving is proof something is writing this run, whatever an
        # earlier look at the file's age concluded.
        self.interrupted = False
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
            # A resume reopens a run that had already finished. Leaving these set
            # showed the header as "stopped early" over a run that was actively
            # dispatching agents.
            self.completed = False
            self.finished = None
            self.stopped_early = None
            self.mode = detail.get("mode")
            self.roster = list(detail.get("agents") or [])
            self.budget_usd = detail.get("budget_usd")
            self.wall_clock_s = detail.get("wall_clock_s")
            self.target_root = detail.get("target_root")
            self.target_sha = detail.get("target_sha")
            self.target_dirty = detail.get("target_dirty")
            self.target_branch = detail.get("target_branch")
            # The process that was running these is gone -- a resume starts a
            # new one. A SYNTHESIZER killed mid-dispatch showed "running" for
            # ever after, beside a run that had finished.
            for view in self.agents.values():
                if view.status in _IN_FLIGHT:
                    view.status = view.settled or "queued"
                    view.waiting_until = None
            for name in self.roster:
                view = self._agent(name)
                # A resume re-rosters agents the previous session marked as
                # never reached. They are queued again, not still never-run.
                if view.status == "never_ran":
                    view.status = "queued"

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
                elif view.status in _IN_FLIGHT:
                    view.status = view.settled or "interrupted"
                    view.waiting_until = None

        elif kind == LedgerKind.AGENT_STARTED and agent:
            view = self._agent(agent)
            view.status = "running"
            view.invocations += 1
            view.started_at = view.started_at or entry.at
            view.model = detail.get("model") or view.model

        elif kind == LedgerKind.AGENT_FINISHED and agent:
            view = self._agent(agent)
            error = detail.get("error")
            if error and is_quota_error(error):
                # The provider stopped serving; the agent did nothing wrong.
                # Red "failed" was drawn over a REPRODUCER that the resume then
                # ran to completion.
                view.status = "rate_limited"
                view.reason = "stopped by the provider's usage limit"
            elif error:
                view.status = "failed"
                view.error = error
            else:
                # The latest outcome is the card's: an earlier invocation's
                # error stays in the ledger, not on a box that has since succeeded.
                view.status = "done"
                view.error = None
                view.reason = None
            view.settled = view.status
            view.waiting_until = None
            view.finished_at = entry.at
            view.cost_usd += float(detail.get("cost_usd") or 0.0)
            view.turns += int(detail.get("num_turns") or 0)
            if view.started_at:
                view.duration_s = (entry.at - view.started_at).total_seconds()

        elif kind == LedgerKind.AGENT_ERROR and agent:
            view = self._agent(agent)
            if is_quota_error(detail.get("error")):
                view.status = "rate_limited"
                view.reason = "stopped by the provider's usage limit"
            else:
                view.status = "failed"
                view.error = detail.get("error")

        elif kind == LedgerKind.QUOTA_WAIT and agent:
            view = self._agent(agent)
            view.status = "waiting"
            view.error = None
            try:
                view.waiting_until = datetime.fromisoformat(str(detail.get("until")))
            except ValueError:
                view.waiting_until = None
            view.reason = "waiting for the provider's usage limit to reset"

        elif kind == LedgerKind.QUOTA_EXHAUSTED:
            # Names the agent it stopped on as "AGENT: error".
            name = str(detail.get("reason") or "").split(":", 1)[0].strip()
            if name in self.agents and detail.get("resume"):
                self.agents[name].reason = (
                    f"stopped by the provider's usage limit -- resume: {detail['resume']}"
                )

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
            # Count the envelope, not the line. An envelope is re-logged when a
            # later phase revises it -- REPRODUCER raising a held finding's
            # confidence writes a second `envelope` line naming the same id and
            # the same discovering agent -- and incrementing per line credited
            # AUDITOR with two findings for one defect. `_add_finding` already
            # owns the "have I seen this id" question; ask it rather than
            # keeping a second answer here that can disagree.
            first_time = self._add_finding(
                str(detail.get("envelope_id") or ""), entry, detail
            )
            if agent and first_time:
                self._agent(agent).findings += 1

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

    def _add_finding(
        self, envelope_id: str, entry: LedgerEntry, detail: dict[str, Any]
    ) -> bool:
        """Record a finding. True when it was new, so callers can count it once."""
        if not envelope_id or any(f.id == envelope_id for f in self.findings):
            return False
        env = None
        try:
            env = self.store.get_envelope(envelope_id)
        except Exception:
            env = None
        if env is not None:
            self.findings.append(FindingView.of(env, min_confidence=self.min_confidence))
            return True
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
        return True

    # -- serialisation ----------------------------------------------------

    def to_json(self, *, recent: int = RECENT_MAX) -> dict[str, Any]:
        findings = sorted(self.findings, key=lambda f: (f.severity_rank, -f.confidence))
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "started": _iso(self.started),
            "finished": _iso(self.finished),
            "completed": self.completed,
            "interrupted": self.interrupted,
            "live": self.live,
            "replay": self.replay_speed,
            "last_at": _iso(self.last_at),
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
    if entries is None:
        entries, view.offset = replay(store)
    for entry in entries:
        view.apply(entry)
    # Decided once the replay has told us the run's own cap. A view built from
    # entries handed in by a caller has no file age to go on and stays as-is.
    if view.offset is not None and not view.completed:
        view.interrupted = trace.is_stale(store.ledger_path, view.wall_clock_s)
    return view


# -- scanning a ledger without parsing it ------------------------------------


@dataclass
class _Scan:
    """What the run rail needs from one ledger, kept between requests.

    The ledger is append-only, so a scan can resume where the last one stopped:
    a finished run is read once for the life of the process, and a live one
    costs only the bytes written since the rail last looked.
    """

    offset: int = 0
    #: The bytes just before `offset`, compared on the next read. A file that no
    #: longer carries them was rewritten rather than appended to, and is
    #: rescanned from the start instead of being trusted.
    signature: bytes = b""
    starts: int = 0
    finishes: int = 0
    #: The newest `run_started` line, parsed; the oldest line of any kind, for a
    #: ledger that has none.
    last_start: dict[str, Any] | None = None
    first: dict[str, Any] | None = None


_SCANS: dict[str, _Scan] = {}
#: `/api/runs` runs on a worker thread, and so can two of them at once.
_SCANS_LOCK = threading.Lock()


def _scan(path: Path) -> _Scan | None:
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        with _SCANS_LOCK:
            _SCANS.pop(key, None)
        return None
    with _SCANS_LOCK:
        scan = _SCANS.get(key)
        try:
            with path.open("rb") as handle:
                if scan is not None and size < scan.offset:
                    scan = None
                elif scan is not None:
                    handle.seek(scan.offset - len(scan.signature))
                    if handle.read(len(scan.signature)) != scan.signature:
                        scan = None
                if scan is None:
                    scan = _Scan()
                    handle.seek(0)
                data = handle.read()
        except OSError:
            return None
        end = data.rfind(b"\n") + 1
        data = data[:end]
        if data:
            starts, finishes = trace.run_edges(data)
            scan.starts += len(starts)
            scan.finishes += len(finishes)
            if starts:
                scan.last_start = trace.line_at(data, starts[-1]) or scan.last_start
            if scan.offset == 0 and scan.first is None:
                scan.first = trace.line_at(data, 0)
            scan.offset += end
            scan.signature = (scan.signature + data[-64:])[-64:]
        _SCANS[key] = scan
        # A copy: the cached one is updated in place by the next caller, and
        # a reader must not see its counts half-way through that.
        return replace(scan)


def _detail(line: dict[str, Any] | None) -> dict[str, Any]:
    detail = (line or {}).get("detail")
    return detail if isinstance(detail, dict) else {}


def run_started_detail(store: RunStore) -> dict[str, Any]:
    """The newest `run_started`'s detail, from the raw scan. Empty if there is none."""
    scan = _scan(store.ledger_path)
    return _detail(scan.last_start) if scan is not None else {}


def is_live(store: RunStore, *, now: float | None = None) -> bool:
    """Whether this run is still being written.

    Not "does the ledger contain `run_finished`" -- `qaas run --run-id` appends a
    second `run_started` to a ledger that already carries one `run_finished`, and
    that test called a resumed run dead while REPRODUCER was still working in it.
    Counting is what distinguishes them: a run is live while it has been started
    more times than it has been finished.

    There is no "current run" pointer anywhere, and deliberately not: the ledger
    is the only thing that knows, and it knows without being told.

    Except when the run was killed. A ^C'd run never wrote its `run_finished`,
    so the count called it live for ever -- the rail pulsed over it and
    `pick_run` opened it ahead of the run actually going. So a count that says
    "live" is checked against the file's age: silent for longer than the run's
    own wall-clock cap allows, and nothing is writing it (`trace.is_stale`).
    """
    scan = _scan(store.ledger_path)
    if scan is None or scan.starts <= scan.finishes:
        return False
    wall_clock = _detail(scan.last_start).get("wall_clock_s")
    return not trace.is_stale(store.ledger_path, wall_clock, now=now)


def pick_run(root: Path | str = DEFAULT_ROOT, run_id: str | None = None) -> str | None:
    """Which run to open: the one asked for, else the live one, else the newest.

    "Live" is `is_live`'s, which already refuses a killed run whose ledger only
    *claims* to be open -- so a stale one from last week no longer wins over the
    run that is actually going, and with none going the newest is opened.
    """
    if run_id:
        return run_id
    ids = list_runs(root)
    if not ids:
        return None
    for candidate in ids:  # newest first
        if is_live(RunStore(candidate, root=root, create=False)):
            return candidate
    return ids[0]


def _started_iso(line: dict[str, Any] | None) -> str | None:
    raw = (line or {}).get("at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).isoformat()
    except ValueError:
        return raw


def list_runs_summary(root: Path | str = DEFAULT_ROOT, limit: int = 50) -> list[dict[str, Any]]:
    """One row per run for the run rail. Cheap: no full ledger parse.

    `qaas runs` reports an envelope count and an invocation count and stops
    short of the cost, though `total_cost_usd()` is right there (cli.py:860-878).
    The rail shows it.

    "Cheap" was the docstring and not the code: it validated every line of every
    listed ledger through pydantic, then read each one a second time for
    `is_live`. Forty runs of 30k lines took 3.2s, on the event loop, and every
    SSE stream froze while it ran. Now each ledger is scanned as raw bytes, and
    only from where the previous scan stopped (`_scan`).
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
        scan = _scan(store.ledger_path)
        first = (scan.last_start or scan.first) if scan is not None else None
        rows.append(
            {
                "run_id": run_id,
                "mode": _detail(first).get("mode"),
                "started": _started_iso(first),
                "live": is_live(store),
                "cost_usd": round(sum(r.cost_usd for r in results), 4),
                "agents": len({r.agent for r in results}),
                "findings": len(list((store.dir / "envelopes").glob("*.json")))
                if (store.dir / "envelopes").exists()
                else 0,
            }
        )
    return rows


#: What a system-map version may look like: the store names them
#: `<timestamp>-<hex>`, and nothing it writes contains a separator.
_MAP_VERSION_RE = re.compile(r"[\w.-]+")


class UnknownMapVersion(ValueError):
    """A `?version=` that is not one of the maps on disk."""


def system_map(root: Path | str = DEFAULT_ROOT, version: str | None = None) -> dict[str, Any] | None:
    """A system map by version, or the latest. None when there is none yet.

    `version` arrives from a query string, and it went straight into
    `SystemMapStore.get`, which joined it into a path: `?version=../../secret`
    read `<root>/../secret.json` -- any JSON file the user could read, served to
    whatever page could reach the port. It is now accepted only if it names a
    map the store actually lists; anything else raises `UnknownMapVersion`.
    """
    from qaas.store import SystemMapStore

    # `create=False`: this is a reader, and constructing the store used to mkdir
    # `.qaas/system-map/` in whatever directory the dashboard was started from.
    maps = SystemMapStore(root, create=False)
    version = version or None          # `?version=` with nothing after it means latest
    try:
        known = set(maps.versions())
        chosen = version if version is not None else maps.latest_version()
    except OSError:
        return None
    if chosen is None:
        return None
    # The `latest` pointer is a file on disk too, so it is held to the same rule.
    if not _MAP_VERSION_RE.fullmatch(chosen) or chosen not in known:
        if version is None:
            return None
        raise UnknownMapVersion(
            f"no system map version {version!r}"
            + (f"; known versions: {', '.join(sorted(known)[-5:])}" if known else "")
        )
    try:
        return maps.get(chosen)
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
