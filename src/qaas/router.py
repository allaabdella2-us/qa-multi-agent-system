"""ROUTER — the run state machine.

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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from qaas.config import AgentSpec, SystemConfig, load_config
from qaas.envelope import DefectEnvelope
from qaas.target import agent_usable
from qaas.mcp.context import ToolContext
from qaas.runner import RunOutcome, run_agent
from qaas.store import RunStore, SystemMapStore
from qaas import tasks


class BudgetExceeded(RuntimeError):
    """The run hit its spend or wall-clock cap. Not an error — a control working."""


def target_revision(root: Path | str | None) -> dict[str, Any]:
    """What commit of the target this run is looking at, for `run_started`.

    Without this a run is not pinned to any code: the ledger said which agents
    ran and what they spent, but nothing said *what they read*, so a finding
    could never be replayed against the tree that produced it. Now `qaas show`
    can name the commit.

    `dirty` is not decoration — a run against an edited working tree is not
    pinned by its sha either, and that has to be visible rather than implied.

    Never raises. A target that is not a git checkout (or has no git at all) is
    an ordinary, supported state: the fields come back None and the run
    proceeds. Provenance is worth recording, never worth failing a run for.
    """
    if root is None:
        return {"target_root": None, "target_sha": None, "target_dirty": None}
    root = Path(root)
    info: dict[str, Any] = {"target_root": str(root), "target_sha": None, "target_dirty": None}

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    if sha is None:
        return info  # not a repo, no git, or an empty repo with no commits yet
    info["target_sha"] = sha.strip()
    status = git("status", "--porcelain")
    if status is not None:
        info["target_dirty"] = bool(status.strip())
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        info["target_branch"] = branch.strip()
    return info


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

    def __init__(self, max_usd: float | None, max_seconds: int, *, already_spent: float = 0.0):
        self.max_usd = max_usd
        self.max_seconds = max_seconds
        #: What this run has already cost, including earlier invocations.
        #:
        #: A resumed run (`qaas run --run-id <existing>`) used to start the
        #: counter at zero, so the cap was per *invocation*, not per run --
        #: resume three times against a $20 mode and you could spend $60 while
        #: every individual pass reported itself within budget. One real run
        #: shows the effect: `cost $32.90 of $20.00 budget`. The wall clock is
        #: deliberately NOT carried across: it measures this process, and a run
        #: resumed the next morning has not been running all night.
        self.spent = already_spent
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining_usd(self) -> float | None:
        return None if self.max_usd is None else max(0.0, self.max_usd - self.spent)

    def spend(self, amount: float) -> None:
        self.spent += amount

    def check(self) -> None:
        # `max_usd is None` means no spend ceiling -- the shipped config sets
        # none, because a dollar figure bakes one vendor's pricing into a tool
        # meant to run against local models too. The wall-clock cap and each
        # agent's `max_turns` still bound a run; those are model-agnostic.
        if self.max_usd is not None and self.spent >= self.max_usd:
            raise BudgetExceeded(f"spend cap reached: ${self.spent:.2f} of ${self.max_usd:.2f}")
        if self.elapsed >= self.max_seconds:
            raise BudgetExceeded(
                f"wall-clock cap reached: {self.elapsed:.0f}s of {self.max_seconds}s"
            )

    def allowance(self, spec: AgentSpec) -> float | None:
        """What this agent may spend, or None when neither it nor the run caps it."""
        caps = [c for c in (spec.max_budget_usd, self.remaining_usd) if c is not None]
        return max(0.01, min(caps)) if caps else None


class Router:
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

    def _target_root(self) -> Path | None:
        """The checkout under examination, or None when nothing is configured.

        Deliberately the same value the agents' tools are pointed at, so the
        commit recorded in the ledger is the commit they actually read. This
        used to compute `self.repo_root / config.target_app`; both of those are
        gone -- `repo_root` conflated the qaas project with the target, and
        `target_app` was the demo-shaped default that made the conflation look
        like it worked.
        """
        return self.target_root

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
        # Carry forward what this run id has already spent, so a cap survives a
        # resumption instead of resetting with it.
        budget = Budget(
            run_mode.max_budget_usd,
            run_mode.max_wall_clock_s,
            already_spent=store.total_cost_usd() if run_id else 0.0,
        )
        report = RunReport(run_id=store.run_id, mode=mode)

        store.log(
            "run_started",
            mode=mode,
            agents=sorted(specs),
            budget_usd=run_mode.max_budget_usd,
            wall_clock_s=run_mode.max_wall_clock_s,
            **target_revision(self._target_root()),
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
            await self._phase_report(specs, store, budget, report, mode, map_version)
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
        spec = specs.get("MAPPER")
        if spec is None:
            return self.maps.latest_version()

        budget.check()
        before = self.maps.latest_version()
        outcome = await self._dispatch(spec, store, budget, report, tasks.mapper(self.config), None)

        version = self.maps.latest_version()
        if version == before or version is None:
            # Everything downstream reads the map. A stale one is a worse failure
            # than a missing one, so say plainly which we are running on.
            note = "MAPPER published no map" + (
                f"; continuing on the previous map {before}" if before else "; no map exists"
            )
            report.escalations.append(note)
            store.log("escalation", agent="MAPPER", reason=note)
        return version or before

    async def _phase_discover(self, specs, store, budget, report, mode, map_version) -> None:
        """Discovery agents are independent. Run them concurrently, bounded."""
        discovery = [s for name, s in specs.items() if s.layer == "discovery"]

        # Skip agents this target cannot support. `qaas doctor` has always
        # reported these ("agents that cannot: BROWSER"), but nothing acted on
        # it, so a run against a target with no reachable UI would still
        # dispatch BROWSER and spend its entire budget hunting a browser that
        # was never there. Being told an agent cannot work and then watching it
        # run is worse than not being told.
        profile = getattr(self.config, "profile", None)
        if profile is not None:
            caps = profile.capabilities()
            unusable = [s for s in discovery if not agent_usable(s.name, caps)]
            if unusable:
                discovery = [s for s in discovery if s not in unusable]
                store.log(
                    "skipped",
                    reason="target cannot support these agents",
                    agents=[s.name for s in unusable],
                )
                for spec in unusable:
                    self._emit("skipped", agent=spec.name, reason="target lacks the capability")

        if not discovery:
            return

        # API and BROWSER keep bespoke tasks because they name tools only
        # they have. Everything else gets the generic discovery task, which is
        # what makes "a new agent is a prompt plus a YAML" true: this used to be
        # a closed dict, so a new discovery agent was skipped with `no task
        # builder` -- it validated, it assembled, it showed up in `--dry-run`,
        # and then it silently did nothing.
        builders = {
            "API": lambda spec: tasks.api(self.config, mode),
            "BROWSER": lambda spec: tasks.browser(self.config, mode),
        }
        jobs = [
            (spec, builders.get(spec.name, lambda sp: tasks.discovery(self.config, mode, sp))(spec))
            for spec in discovery
        ]

        await self._gather(jobs, store, budget, report, map_version, self.config.run_modes[mode].max_concurrency)

    async def _phase_report(self, specs, store, budget, report, mode, map_version) -> None:
        """Reporting agents run last, over what the run itself produced.

        Dispatched by LAYER, like discovery, and deliberately not by name. Every
        other phase looks up a specific agent (`specs.get("REPRODUCER")`), which is
        why REPORTER could be configured, validated, assembled and shown in
        `--dry-run` while never running: no phase asked for it. That is the same
        silent skip DBA and AUDITOR exposed for discovery, and it is worth
        fixing the shape rather than the instance -- a second reporting agent
        now needs no Python either.

        A run with no reporting agent is the ordinary case and not worth a log
        line; most modes have none.
        """
        reporting = [s for s in specs.values() if s.layer == "reporting"]
        if not reporting:
            return

        for spec in reporting:
            budget.check()
            await self._dispatch(
                spec, store, budget, report, tasks.report(self.config, mode), map_version
            )

    async def _phase_reproduce(self, specs, store, budget, report, map_version) -> None:
        """One REPRODUCER invocation per finding.

        Separate contexts on purpose: reproducing finding B should not inherit
        whatever REPRODUCER talked itself into while working on finding A.
        """
        spec = specs.get("REPRODUCER")
        if spec is None:
            return

        drafts = [e for e in store.envelopes() if e.reproduction.status.value == "unattempted"]
        if not drafts:
            store.log("skipped", agent="REPRODUCER", reason="no findings to reproduce")
            return

        cap = self.config.thresholds.max_findings_per_agent_run
        if len(drafts) > cap:
            note = f"{len(drafts)} findings exceed the per-run cap of {cap}; triaging the most severe"
            report.escalations.append(note)
            store.log("escalation", agent="REPRODUCER", reason=note)
            drafts = sorted(drafts, key=lambda e: (e.severity.rank, -e.confidence))[:cap]

        jobs = [
            (spec, tasks.reproducer(draft, self.config, self.config.thresholds.flake_runs))
            for draft in drafts
        ]
        await self._gather(jobs, store, budget, report, map_version, concurrency=2)

    async def _phase_file(self, specs, store, budget, report, map_version) -> None:
        spec = specs.get("TRIAGE")
        if spec is None:
            return
        fileable = [
            e for e in store.envelopes()
            if e.is_fileable(self.config.thresholds.min_confidence_to_file)[0]
        ]
        if not fileable:
            store.log("skipped", agent="TRIAGE", reason="nothing passed the gates")
            return
        cap = min(spec.policy.max_tickets_per_run, self.config.thresholds.max_tickets_per_run)
        await self._dispatch(spec, store, budget, report, tasks.triage(self.config, cap), map_version)

    async def _phase_verify(self, specs, store, budget, report, map_version) -> None:
        spec = specs.get("VERIFIER")
        if spec is None:
            return
        pending = [e for e in store.envelopes() if e.jira.key]
        if self.tickets:
            pending = [e for e in pending if e.jira.key in self.tickets]
            unknown = self.tickets - {e.jira.key for e in store.envelopes() if e.jira.key}
            if unknown:
                store.log("skipped", reason="unknown tickets", tickets=sorted(unknown))
        if not pending:
            store.log("skipped", agent="VERIFIER", reason="no tickets to verify")
            return
        for envelope in pending:
            budget.check()
            await self._verify_loop(envelope, specs, store, budget, report, map_version)

    async def _verify_loop(self, envelope, specs, store, budget, report, map_version) -> None:
        """VERIFIER -> NOT_FIXED -> remediate -> VERIFIER, bounded by §8.3.

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
        # VERIFIER back there after a remediation round made this loop unable to
        # ever reach VERIFIED: FIXER would fix, REVIEWER approve, and VERIFIER
        # re-verify the unfixed branch it had just failed on, burn a reopen and
        # escalate. A live run recorded exactly that ("this branch cannot carry
        # a fix"). Where FIXER put the fix is only knowable after the fact, so
        # it is read back out of the ledger below.
        repro_branch = envelope.reproduction.environment.branch or "main"
        fix_branch: str | None = None

        while True:
            await self._dispatch(
                specs["VERIFIER"], store, budget, report,
                tasks.verifier(ticket, envelope, branch=fix_branch or repro_branch), map_version,
            )
            verdict = self._latest_verdict(store, ticket)

            if verdict is None:
                self._escalate(report, store, "VERIFIER",
                    f"{ticket}: VERIFIER returned no verdict; the ticket stays in review")
                return
            if verdict == "VERIFIED":
                store.log("verified", agent="VERIFIER", ticket_key=ticket, reopens=reopens)
                return
            if verdict == "REGRESSED":
                self._escalate(report, store, "VERIFIER",
                    f"{ticket}: REGRESSED — the fix broke something else; blocking for a human")
                return

            # NOT_FIXED from here.
            if reopens >= max_reopens:
                self._escalate(report, store, "VERIFIER",
                    f"{ticket}: still NOT_FIXED after {reopens} reopen(s), the limit. "
                    "Escalating rather than cycling further")
                return

            reopens += 1
            store.log("reopened", agent="VERIFIER", ticket_key=ticket, attempt=reopens)
            mark = len(list(store.ledger("vcs")))
            if not await self._remediate(envelope, specs, store, budget, report, map_version):
                return
            # Keep the previous branch if this round wrote nothing: a re-verify
            # of the last fix beats silently falling back to the repro branch.
            fix_branch = self._branch_written_since(store, mark) or fix_branch

    async def _remediate(self, envelope, specs, store, budget, report, map_version) -> bool:
        """FIXER -> REVIEWER, bounded. Returns whether a fix is ready to re-verify.

        Phase 3 agents. In a Phase 1 roster neither exists, so a NOT_FIXED
        verdict escalates to a human immediately — which is correct, and much
        better than the loop silently re-running VERIFIER against unchanged code.
        """
        ticket = envelope.jira.key
        fixer, reviewer = specs.get("FIXER"), specs.get("REVIEWER")

        if fixer is None:
            self._escalate(report, store, "VERIFIER",
                f"{ticket}: NOT_FIXED and no FIXER in this run's roster. "
                "Nothing here can produce a fix; a human takes it from here")
            return False

        for trip in range(1, self.config.thresholds.max_mender_arbiter_round_trips + 1):
            budget.check()
            await self._dispatch(fixer, store, budget, report,
                                 tasks.fixer(ticket, envelope), map_version)
            if reviewer is None:
                return True

            await self._dispatch(reviewer, store, budget, report,
                                 tasks.reviewer(ticket, envelope), map_version)
            review = self._latest_review(store, ticket)
            if review == "APPROVE":
                return True
            if review == "ESCALATE_TO_HUMAN":
                self._escalate(report, store, "REVIEWER", f"{ticket}: REVIEWER escalated the fix")
                return False
            store.log("review_round_trip", agent="REVIEWER", ticket_key=ticket, trip=trip)

        self._escalate(report, store, "REVIEWER",
            f"{ticket}: {self.config.thresholds.max_mender_arbiter_round_trips} "
            "FIXER/REVIEWER round trips without approval; escalating")
        return False

    @staticmethod
    def _branch_written_since(store, mark: int) -> str | None:
        """The branch FIXER actually wrote to during one remediation round.

        Scoped to the ledger entries added since `mark` rather than searched
        run-wide, because a run verifies several tickets against one ledger and
        an earlier ticket's `fix/*` branch is the wrong answer here. The last
        write wins: FIXER ends a successful round on `push` or `open_pr`.
        """
        for entry in reversed(list(store.ledger("vcs"))[mark:]):
            if entry.agent != "FIXER":
                continue
            branch = entry.detail.get("branch")
            if branch:
                return str(branch)
        return None

    @staticmethod
    def _latest_verdict(store, ticket_key: str) -> str | None:
        """VERIFIER's verdict is a typed ledger entry, never parsed from prose."""
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


def build(config_dir: Path | str = "config", root: Path | str = ".qaas") -> Router:
    config = load_config(config_dir)
    return Router(config, root=root)
