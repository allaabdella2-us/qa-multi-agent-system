"""CONDUCTOR — the run state machine.

Deliberately not an LLM. §4.1 wants its reasoning shallow (routing, not analysis)
and §10 makes it the enforcement point for budget and concurrency — and a model
cannot enforce a budget it is itself spending. Everything here is ordinary code:
dispatch, phase ordering, concurrency limits, the spend governor, the §8.3 loop
breakers, retries and escalation.

The phases exist because the dependencies are real, not for tidiness:

    map  ->  discover  ->  reproduce  ->  file

Discovery cannot start without the map. Triage cannot start without findings.
Within a phase, agents are independent and run concurrently up to the mode's cap.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from qaas.config import AgentSpec, SystemConfig, load_config
from qaas.envelope import DefectEnvelope
from qaas.mcp.context import ToolContext
from qaas.runner import RunOutcome, run_agent
from qaas.store import RunStore, SystemMapStore
from qaas import tasks


class BudgetExceeded(RuntimeError):
    """The run hit its spend or wall-clock cap. Not an error — a control working."""


@dataclass
class RunReport:
    run_id: str
    mode: str
    outcomes: list[RunOutcome] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    stopped_early: str | None = None

    @property
    def cost_usd(self) -> float:
        return sum(o.result.cost_usd for o in self.outcomes)

    @property
    def failed(self) -> list[str]:
        return [o.result.agent for o in self.outcomes if not o.ok]

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "agents_run": len(self.outcomes),
            "failed": self.failed,
            "cost_usd": round(self.cost_usd, 4),
            "escalations": self.escalations,
            "stopped_early": self.stopped_early,
        }


class Budget:
    """The spend and wall-clock governor. Checked before every dispatch."""

    def __init__(self, max_usd: float, max_seconds: int):
        self.max_usd = max_usd
        self.max_seconds = max_seconds
        self.spent = 0.0
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.spent)

    def spend(self, amount: float) -> None:
        self.spent += amount

    def check(self) -> None:
        if self.spent >= self.max_usd:
            raise BudgetExceeded(f"spend cap reached: ${self.spent:.2f} of ${self.max_usd:.2f}")
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded(
                f"wall-clock cap reached: {self.elapsed:.0f}s of {self.max_seconds}s"
            )

    def allowance(self, spec: AgentSpec) -> float:
        """What this agent may spend: its own cap, or what the run has left."""
        return max(0.01, min(spec.max_budget_usd, self.remaining_usd))


class Conductor:
    """Owns one run from trigger to report."""

    def __init__(
        self,
        config: SystemConfig,
        target_root: Path | None = None,
        *,
        root: Path | str = ".qaas",
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        tickets: list[str] | None = None,
    ):
        self.config = config
        #: The application under test. Defaults to whatever the active profile
        #: says, which is the answer every caller wants; an explicit path is for
        #: tests and for a run pointed at a clone that has no profile yet.
        self.target_root = Path(target_root) if target_root is not None else config.target_root()
        self.maps = SystemMapStore(root)
        self.root = Path(root)
        self.on_event = on_event
        #: When set, a fix cycle works only these tickets. Verifying ten tickets
        #: costs ten times as much as verifying one, and during development you
        #: almost always want one.
        self.tickets = set(tickets) if tickets else None

    def _emit(self, kind: str, **detail: Any) -> None:
        if self.on_event:
            self.on_event(kind, detail)

    def _context(self, store: RunStore, spec: AgentSpec, map_version: str | None) -> ToolContext:
        return ToolContext(
            store=store,
            maps=self.maps,
            config=self.config,
            agent=spec,
            target_root=self.target_root,
            map_version=map_version,
        )

    # -- the run ----------------------------------------------------------

    async def run(self, mode: str, *, run_id: str | None = None) -> RunReport:
        specs = {s.name: s for s in self.config.enabled_agents(mode)}
        run_mode = self.config.run_modes[mode]
        store = RunStore(run_id, self.root) if run_id else RunStore.new(self.root)
        budget = Budget(run_mode.max_budget_usd, run_mode.max_wall_clock_s)
        report = RunReport(run_id=store.run_id, mode=mode)

        store.log(
            "run_started",
            mode=mode,
            agents=sorted(specs),
            budget_usd=run_mode.max_budget_usd,
            wall_clock_s=run_mode.max_wall_clock_s,
        )
        self._emit("run_started", run_id=store.run_id, mode=mode, agents=sorted(specs))

        try:
            map_version = await self._phase_map(specs, store, budget, report)
            await self._phase_discover(specs, store, budget, report, mode, map_version)
            await self._phase_reproduce(specs, store, budget, report, map_version)
            if run_mode.files_tickets:
                await self._phase_file(specs, store, budget, report, map_version)
            else:
                store.log("skipped", reason="mode does not file tickets", mode=mode)
            await self._phase_verify(specs, store, budget, report, map_version)
        except BudgetExceeded as exc:
            report.stopped_early = str(exc)
            report.escalations.append(str(exc))
            store.log("escalation", reason=str(exc))
            self._emit("stopped", reason=str(exc))

        store.log("run_finished", **report.summary())
        self._emit("run_finished", **report.summary())
        return report

    # -- phases -----------------------------------------------------------

    async def _phase_map(self, specs, store, budget, report) -> str | None:
        """Publish the map first. Everything downstream reads it."""
        spec = specs.get("CARTOGRAPHER")
        if spec is None:
            return self.maps.latest_version()

        budget.check()
        before = self.maps.latest_version()
        outcome = await self._dispatch(spec, store, budget, report, tasks.cartographer(self.config), None)

        version = self.maps.latest_version()
        if version == before or version is None:
            # Everything downstream reads the map. A stale one is a worse failure
            # than a missing one, so say plainly which we are running on.
            note = "CARTOGRAPHER published no map" + (
                f"; continuing on the previous map {before}" if before else "; no map exists"
            )
            report.escalations.append(note)
            store.log("escalation", agent="CARTOGRAPHER", reason=note)
        return version or before

    async def _phase_discover(self, specs, store, budget, report, mode, map_version) -> None:
        """Discovery agents are independent. Run them concurrently, bounded."""
        discovery = [s for name, s in specs.items() if s.layer == "discovery"]
        if not discovery:
            return

        builders = {
            "CONDUIT": lambda: tasks.conduit(self.config, mode),
            "SURFACE": lambda: tasks.surface(self.config, mode),
        }
        jobs = [
            (spec, builders[spec.name]())
            for spec in discovery
            if spec.name in builders
        ]
        unknown = [s.name for s in discovery if s.name not in builders]
        if unknown:
            store.log("skipped", reason="no task builder", agents=unknown)

        await self._gather(jobs, store, budget, report, map_version, self.config.run_modes[mode].max_concurrency)

    async def _phase_reproduce(self, specs, store, budget, report, map_version) -> None:
        """One FORGE invocation per finding.

        Separate contexts on purpose: reproducing finding B should not inherit
        whatever FORGE talked itself into while working on finding A.
        """
        spec = specs.get("FORGE")
        if spec is None:
            return

        drafts = [e for e in store.envelopes() if e.reproduction.status.value == "unattempted"]
        if not drafts:
            store.log("skipped", agent="FORGE", reason="no findings to reproduce")
            return

        cap = self.config.thresholds.max_findings_per_agent_run
        if len(drafts) > cap:
            note = f"{len(drafts)} findings exceed the per-run cap of {cap}; triaging the most severe"
            report.escalations.append(note)
            store.log("escalation", agent="FORGE", reason=note)
            drafts = sorted(drafts, key=lambda e: (e.severity.rank, -e.confidence))[:cap]

        jobs = [
            (spec, tasks.forge(draft, self.config, self.config.thresholds.flake_runs))
            for draft in drafts
        ]
        await self._gather(jobs, store, budget, report, map_version, concurrency=2)

    async def _phase_file(self, specs, store, budget, report, map_version) -> None:
        spec = specs.get("CLERK")
        if spec is None:
            return
        fileable = [
            e for e in store.envelopes()
            if e.is_fileable(self.config.thresholds.min_confidence_to_file)[0]
        ]
        if not fileable:
            store.log("skipped", agent="CLERK", reason="nothing passed the gates")
            return
        cap = min(spec.policy.max_tickets_per_run, self.config.thresholds.max_tickets_per_run)
        await self._dispatch(spec, store, budget, report, tasks.clerk(self.config, cap), map_version)

    async def _phase_verify(self, specs, store, budget, report, map_version) -> None:
        spec = specs.get("PROOF")
        if spec is None:
            return
        pending = [e for e in store.envelopes() if e.jira.key]
        if self.tickets:
            pending = [e for e in pending if e.jira.key in self.tickets]
            unknown = self.tickets - {e.jira.key for e in store.envelopes() if e.jira.key}
            if unknown:
                store.log("skipped", reason="unknown tickets", tickets=sorted(unknown))
        if not pending:
            store.log("skipped", agent="PROOF", reason="no tickets to verify")
            return
        for envelope in pending:
            budget.check()
            await self._verify_loop(envelope, specs, store, budget, report, map_version)

    async def _verify_loop(self, envelope, specs, store, budget, report, map_version) -> None:
        """PROOF -> NOT_FIXED -> remediate -> PROOF, bounded by §8.3.

        The bound is the point. Without `max_proof_reopens` a fix that keeps
        missing the defect cycles until the budget is gone, and the run ends with
        no verdict and no money left to reach one. Escalating after one reopen
        costs a human five minutes; not escalating costs the whole run.
        """
        ticket = envelope.jira.key
        max_reopens = self.config.thresholds.max_proof_reopens
        reopens = 0

        # The envelope names the *repro* branch, which by construction carries a
        # failing test and no fix -- it is written before any fix exists. Sending
        # PROOF back there after a remediation round made this loop unable to
        # ever reach VERIFIED: MENDER would fix, ARBITER approve, and PROOF
        # re-verify the unfixed branch it had just failed on, burn a reopen and
        # escalate. A live run recorded exactly that ("this branch cannot carry
        # a fix"). Where MENDER put the fix is only knowable after the fact, so
        # it is read back out of the ledger below.
        repro_branch = envelope.reproduction.environment.branch or "main"
        fix_branch: str | None = None

        while True:
            await self._dispatch(
                specs["PROOF"], store, budget, report,
                tasks.proof(ticket, envelope, branch=fix_branch or repro_branch), map_version,
            )
            verdict = self._latest_verdict(store, ticket)

            if verdict is None:
                self._escalate(report, store, "PROOF",
                    f"{ticket}: PROOF returned no verdict; the ticket stays in review")
                return
            if verdict == "VERIFIED":
                store.log("verified", agent="PROOF", ticket_key=ticket, reopens=reopens)
                return
            if verdict == "REGRESSED":
                self._escalate(report, store, "PROOF",
                    f"{ticket}: REGRESSED — the fix broke something else; blocking for a human")
                return

            # NOT_FIXED from here.
            if reopens >= max_reopens:
                self._escalate(report, store, "PROOF",
                    f"{ticket}: still NOT_FIXED after {reopens} reopen(s), the limit. "
                    "Escalating rather than cycling further")
                return

            reopens += 1
            store.log("reopened", agent="PROOF", ticket_key=ticket, attempt=reopens)
            mark = len(list(store.ledger("vcs")))
            if not await self._remediate(envelope, specs, store, budget, report, map_version):
                return
            # Keep the previous branch if this round wrote nothing: a re-verify
            # of the last fix beats silently falling back to the repro branch.
            fix_branch = self._branch_written_since(store, mark) or fix_branch

    async def _remediate(self, envelope, specs, store, budget, report, map_version) -> bool:
        """MENDER -> ARBITER, bounded. Returns whether a fix is ready to re-verify.

        Phase 3 agents. In a Phase 1 roster neither exists, so a NOT_FIXED
        verdict escalates to a human immediately — which is correct, and much
        better than the loop silently re-running PROOF against unchanged code.
        """
        ticket = envelope.jira.key
        mender, arbiter = specs.get("MENDER"), specs.get("ARBITER")

        if mender is None:
            self._escalate(report, store, "PROOF",
                f"{ticket}: NOT_FIXED and no MENDER in this run's roster. "
                "Nothing here can produce a fix; a human takes it from here")
            return False

        for trip in range(1, self.config.thresholds.max_mender_arbiter_round_trips + 1):
            budget.check()
            await self._dispatch(mender, store, budget, report,
                                 tasks.mender(ticket, envelope), map_version)
            if arbiter is None:
                return True

            await self._dispatch(arbiter, store, budget, report,
                                 tasks.arbiter(ticket, envelope), map_version)
            review = self._latest_review(store, ticket)
            if review == "APPROVE":
                return True
            if review == "ESCALATE_TO_HUMAN":
                self._escalate(report, store, "ARBITER", f"{ticket}: ARBITER escalated the fix")
                return False
            store.log("review_round_trip", agent="ARBITER", ticket_key=ticket, trip=trip)

        self._escalate(report, store, "ARBITER",
            f"{ticket}: {self.config.thresholds.max_mender_arbiter_round_trips} "
            "MENDER/ARBITER round trips without approval; escalating")
        return False

    @staticmethod
    def _branch_written_since(store, mark: int) -> str | None:
        """The branch MENDER actually wrote to during one remediation round.

        Scoped to the ledger entries added since `mark` rather than searched
        run-wide, because a run verifies several tickets against one ledger and
        an earlier ticket's `fix/*` branch is the wrong answer here. The last
        write wins: MENDER ends a successful round on `push` or `open_pr`.
        """
        for entry in reversed(list(store.ledger("vcs"))[mark:]):
            if entry.agent != "MENDER":
                continue
            branch = entry.detail.get("branch")
            if branch:
                return str(branch)
        return None

    @staticmethod
    def _latest_verdict(store, ticket_key: str) -> str | None:
        """PROOF's verdict is a typed ledger entry, never parsed from prose."""
        verdicts = [e for e in store.ledger("verdict") if e.detail.get("ticket_key") == ticket_key]
        return verdicts[-1].detail.get("verdict") if verdicts else None

    @staticmethod
    def _latest_review(store, ticket_key: str) -> str | None:
        reviews = [e for e in store.ledger("review") if e.detail.get("ticket_key") == ticket_key]
        return reviews[-1].detail.get("decision") if reviews else None

    def _escalate(self, report, store, agent: str, note: str) -> None:
        report.escalations.append(note)
        store.log("escalation", agent=agent, reason=note)
        self._emit("escalation", agent=agent, reason=note)

    # -- dispatch ---------------------------------------------------------

    async def _gather(self, jobs, store, budget, report, map_version, concurrency: int) -> None:
        """Run jobs concurrently, but stop dispatching once the budget is gone.

        The semaphore bounds how many run at once; the budget check inside each
        slot means a run that blows its cap stops starting new work rather than
        letting everything already queued through.
        """
        if not jobs:
            return
        sem = asyncio.Semaphore(max(1, concurrency))
        stopped: list[str] = []

        async def one(spec: AgentSpec, task: str) -> None:
            async with sem:
                if stopped:
                    return
                try:
                    budget.check()
                except BudgetExceeded as exc:
                    stopped.append(str(exc))
                    return
                await self._dispatch(spec, store, budget, report, task, map_version)

        await asyncio.gather(*(one(spec, task) for spec, task in jobs))
        if stopped:
            raise BudgetExceeded(stopped[0])

    async def _dispatch(self, spec, store, budget, report, task, map_version) -> RunOutcome:
        ctx = self._context(store, spec, map_version)
        allowance = budget.allowance(spec)

        self._emit("agent_started", agent=spec.name, budget=allowance)
        outcome = await run_agent(
            spec, ctx, task, max_budget_usd=allowance, on_event=self.on_event
        )

        budget.spend(outcome.result.cost_usd)
        report.outcomes.append(outcome)
        if not outcome.ok:
            note = f"{spec.name} failed: {outcome.result.error or outcome.result.subtype}"
            report.escalations.append(note)
            store.log("escalation", agent=spec.name, reason=note)
        return outcome


def build(config_dir: Path | str = "config", root: Path | str = ".qaas") -> Conductor:
    config = load_config(config_dir)
    return Conductor(config, root=root)
