"""ROUTER — the run state machine.

Deliberately not an LLM. §4.1 wants its reasoning shallow (routing, not analysis)
and §10 makes it the enforcement point for budget and concurrency — and a model
cannot enforce a budget it is itself spending. Everything here is ordinary code:
dispatch, phase ordering, concurrency limits, the spend governor, the §8.3 loop
breakers and escalation.

There is deliberately **no retry**. A failed agent is escalated and the run
continues without it. Four documents used to promise retries and a dead-letter
queue and no such code has ever existed, so a transient 429 was indistinguishable
from a real failure while the docs said otherwise. Building it properly needs a
classifier for which errors are transient and a guarantee that a retry cannot
duplicate side effects -- an agent that opened a branch before it died must not
open a second one. Until that exists, saying so is better than implying it.

The phases exist because the dependencies are real, not for tidiness:

    map  ->  discover  ->  reproduce  ->  file

Discovery cannot start without the map. Triage cannot start without findings.
Within a phase, agents are independent and run concurrently up to the mode's cap.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from qaas.config import AgentSpec, SystemConfig, load_config
from qaas.envelope import DefectEnvelope
from qaas.target import agent_usable
from qaas.mcp import defect_memory
from qaas.mcp.context import ToolContext
from qaas.runner import RunOutcome, run_agent
from qaas.store import AgentResult, RunStore, SystemMapStore
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


def _with_protected_test(spec: AgentSpec, envelope: DefectEnvelope) -> AgentSpec:
    """FIXER, for this ticket, with the failing test declared unwritable.

    `Guardrail._protected_path` and the `protected_paths` branch of `_check_bash`
    have existed since M3 and were dead code: no agent YAML sets
    `protected_paths` and nothing else populated it, so the predicate always
    returned False. Meanwhile `fixer.yaml` grants `write_paths: [api/app,
    web/src, qa/repro]` -- and `qa/repro` is REPRODUCER's sandbox, holding the
    very test `tasks.fixer` hands FIXER as "the test that defines success". So
    §10's whole symptom-fix guard ("a fixer that edits the test patches the
    symptom") was a sentence in a prompt with nothing behind it.

    It has to be per-invocation rather than in the YAML, because which test is
    protected depends on which ticket is being fixed. The spec is deep-copied so
    one ticket's protection cannot leak into the next one's dispatch -- the
    roster is shared across the whole run.
    """
    test = envelope.reproduction.failing_test
    if not test:
        return spec
    # `qa/repro/test_x.py::test_case` names a case; the file is what is written.
    path = test.split("::", 1)[0].strip()
    if not path:
        return spec
    guarded = spec.model_copy(deep=True)
    if path not in guarded.policy.protected_paths:
        guarded.policy.protected_paths = [*guarded.policy.protected_paths, path]
    return guarded


#: VERIFIER's verdict -> the outcome recorded against the defect. REGRESSED is
#: `regressed` and not a failure of the defect: the fix broke something else, and
#: a future run needs to know that this area is one where fixes have consequences.
_VERDICT_OUTCOMES = {
    "VERIFIED": "verified",
    "NOT_FIXED": "not_fixed",
    "REGRESSED": "regressed",
}


def _review_feedback(entry, observed: str) -> str:
    """What the last attempt got wrong, assembled from typed ledger data.

    Prose built in Python out of fields the agents already recorded -- no new
    tool, no new LedgerKind, and nothing a model has to be trusted to pass on.
    """
    parts: list[str] = []
    if observed:
        parts.append(f"VERIFIER observed: {observed}")
    reasoning = str(entry.detail.get("reasoning") or "").strip()
    if reasoning:
        parts.append(f"REVIEWER's reasoning: {reasoning}")
    concerns = entry.detail.get("concerns") or []
    if isinstance(concerns, str):
        concerns = [concerns]
    for concern in concerns:
        parts.append(f"  - {concern}")
    return "\n".join(parts)


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

    def __init__(
        self,
        max_usd: float | None,
        max_seconds: int,
        *,
        already_spent: float = 0.0,
        reserve_fraction: float = 0.0,
    ):
        self.max_usd = max_usd
        self.max_seconds = max_seconds
        #: See `RunMode.reserve_fraction`. The finding phases stop at
        #: `max_seconds * (1 - reserve_fraction)`; filing and reporting run
        #: against the full cap.
        self.reserve_fraction = max(0.0, min(0.9, reserve_fraction))
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

    def check(self, *, reserve: bool = False) -> None:
        # `max_usd is None` means no spend ceiling -- the shipped config sets
        # none, because a dollar figure bakes one vendor's pricing into a tool
        # meant to run against local models too. The wall-clock cap and each
        # agent's `max_turns` still bound a run; those are model-agnostic.
        if self.max_usd is not None and self.spent >= self.max_usd:
            raise BudgetExceeded(f"spend cap reached: ${self.spent:.2f} of ${self.max_usd:.2f}")
        limit = self.max_seconds
        if reserve and self.reserve_fraction:
            limit = self.max_seconds * (1.0 - self.reserve_fraction)
        if self.elapsed >= limit:
            raise BudgetExceeded(
                f"wall-clock cap reached: {self.elapsed:.0f}s of {limit:.0f}s"
                + (" (the reserve is held back for filing and reporting)" if reserve else "")
            )

    def allowance(self, spec: AgentSpec, *, slots: int = 1) -> float | None:
        """What this agent may spend, or None when neither it nor the run caps it.

        `slots` divides the remainder across the dispatches actually in flight.
        `spent` only moves in `_dispatch` *after* an agent returns, so `_gather`
        started up to `max_concurrency` agents each told it could spend the
        entire remaining budget -- three agents, one budget, handed out three
        times. The run-level check still stops the run, but only after the
        overspend has happened.
        """
        remaining = self.remaining_usd
        if remaining is not None and slots > 1:
            remaining = remaining / slots
        caps = [c for c in (spec.max_budget_usd, remaining) if c is not None]
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
            reserve_fraction=run_mode.reserve_fraction,
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

        map_version = self.maps.latest_version()
        try:
            # The finding phases run against the reserved clock, so that running
            # out of time means "stop looking" rather than "throw away what was
            # found". Filing, verifying and reporting run against the full cap
            # below, outside this try.
            map_version = await self._phase_map(specs, store, budget, report)
            await self._phase_discover(specs, store, budget, report, mode, map_version)
            await self._phase_synthesise(specs, store, budget, report, mode, map_version)
            await self._phase_reproduce(specs, store, budget, report, map_version)
        except BudgetExceeded as exc:
            report.stopped_early = str(exc)
            report.escalations.append(str(exc))
            store.log("escalation", reason=str(exc))
            self._emit("stopped", reason=str(exc))

        try:
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
        except Exception as exc:  # noqa: BLE001 — see below
            # `run()` caught only `BudgetExceeded`, so anything else -- a task
            # builder raising `ValueError` because no profile is loaded, an
            # OSError from a full disk -- propagated out of a run that had
            # already written `run_started`. The ledger then held an opening line
            # with no closing one, which `qaas runs`, `qaas show` and the
            # dashboard all read as "still running", forever. A run that died
            # has to say so.
            note = f"{type(exc).__name__}: {exc}"
            report.stopped_early = note
            report.escalations.append(note)
            store.log("escalation", reason=note)
            self._emit("stopped", reason=note)

        self._record_outcomes(store, mode)
        store.log("run_finished", **report.summary())
        self._emit("run_finished", **report.summary())
        return report

    def _record_outcomes(self, store: RunStore, mode: str) -> None:
        """Write what this run learned into the memory that outlives it.

        Called once, at the end, from the router — which is the point. Everything
        written here was already known *inside* a run and lost at the end of it:
        a finding the evidence gate held, a fix REVIEWER rejected, a verdict of
        NOT_FIXED. All typed ledger lines, all read back by `_verify_loop` and
        `_remediate`, none of them read by anything ever again. So the system
        could say "I have seen this defect before" and never "…and last time it
        was not reproducible", which is the difference between a memory and a
        lesson.

        Deliberately not a tool. An agent that can write its own outcomes can
        record itself as correct and raise its own apparent precision without
        finding anything — the same shape as an agent that can retire an entry
        from the golden ledger, and CLAUDE.md is explicit that that has to be
        designed out rather than trusted away. Agents read this back through
        `search_similar`; nothing gives them a way to write it.

        Never raises. A memory that cannot be written is a worse next run, not a
        failed this one.
        """
        target = str(self.config.target or "")
        try:
            verdicts = [e for e in store.ledger("verdict")]
            reviews = [e for e in store.ledger("review")]
            envelopes = store.envelopes()
        except OSError:
            return

        by_ticket = {e.jira.key: e for e in envelopes if e.jira.key}
        written = 0

        def write(envelope, outcome: str, detail: str = "") -> None:
            nonlocal written
            try:
                defect_memory.record_outcome(
                    self.root,
                    fingerprint=envelope.fingerprint(),
                    run_id=store.run_id,
                    outcome=outcome,
                    target=target,
                    agent=envelope.discovered_by,
                    detail=detail,
                )
                written += 1
            except (sqlite3.Error, OSError, ValueError):
                pass

        for entry in verdicts:
            envelope = by_ticket.get(entry.detail.get("ticket_key"))
            verdict = str(entry.detail.get("verdict") or "").upper()
            if envelope is None or verdict not in _VERDICT_OUTCOMES:
                continue
            write(envelope, _VERDICT_OUTCOMES[verdict], str(entry.detail.get("observed") or ""))
            if verdict == "VERIFIED":
                # This is the line that makes a regression reportable at all. The
                # REGRESSION branch in `defect_memory.record` fires only on
                # `resolved_at`, whose only writer was a tool no agent in the
                # shipped roster holds -- VERIFIER closes tickets and has no
                # `defect_memory` server. So the highest-value thing this system
                # can say was unreachable in every configuration it ships with.
                try:
                    defect_memory.resolve(
                        self.root, envelope.fingerprint(), envelope.jira.key, store.run_id
                    )
                except (sqlite3.Error, OSError):
                    pass

        for entry in reviews:
            envelope = by_ticket.get(entry.detail.get("ticket_key"))
            if envelope is None or entry.detail.get("decision") != "REQUEST_CHANGES":
                continue
            write(envelope, "review_rejected", str(entry.detail.get("reasoning") or ""))

        floor = self.config.thresholds.min_confidence_to_file
        for envelope in envelopes:
            if envelope.reproduction.status.value == "not_reproducible":
                write(envelope, "not_reproducible")
                continue
            fileable, why = envelope.is_fileable(floor)
            if not fileable:
                write(envelope, "held", why)

        if written:
            store.log("defect_memory", action="outcomes", count=written, mode=mode)

    # -- phases -----------------------------------------------------------

    async def _phase_map(self, specs, store, budget, report) -> str | None:
        """Publish the map first. Everything downstream reads it."""
        spec = specs.get("MAPPER")
        if spec is None:
            return self.maps.latest_version()

        budget.check(reserve=True)
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

        # Agents that already did their work in this run are not asked again.
        #
        # `--run-id` re-dispatched every discovery agent, so a run that died at
        # the twelfth of thirteen cost all thirteen to retry -- which made resume
        # useless exactly when it was needed, and is why three separate failures
        # (a misconfigured tracker, a git bug, an account rate limit) each cost
        # full price and none of them produced a score.
        #
        # Scoped to a *successful* finish. An agent that errored has produced
        # nothing and must run again; one that was rate-limited is in that
        # category, which is the case that matters most here.
        done = self._succeeded_agents(store)
        already = [s for s in discovery if s.name in done]
        if already:
            discovery = [s for s in discovery if s.name not in done]
            store.log(
                "skipped",
                reason="already completed in this run; resuming the rest",
                agents=sorted(s.name for s in already),
            )
            for spec in already:
                self._emit("skipped", agent=spec.name, reason="already completed in this run")

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

    async def _phase_synthesise(self, specs, store, budget, report, mode, map_version) -> None:
        """The join. Dispatched by LAYER, between discover and reproduce.

        Every other phase asks "what is wrong with this surface". This one asks
        "do two of these findings, each unremarkable on its own, describe one
        defect that is worse than either". Nothing in the system did that. Each
        discovery agent is its own process with its own context, and although it
        *can* read a peer's envelope -- `list_envelopes` re-globs the directory,
        so a finding emitted thirty seconds ago is visible -- nothing ever asked
        it to, and the prompts said the opposite: "leave the other surfaces to
        the agents that own them". So a tenant bypass that is provable only from
        API's "this handler filters by id, not org_id" *plus* DBA's "and there is
        no owning-org constraint behind it" was emitted as two sub-blocker
        findings in two domains -- which `fingerprint()`, leading with `domain`,
        guarantees will never converge.

        It sits *before* reproduce, and that placement is the whole design. A
        composite emitted here is an ordinary envelope: it earns a REPRODUCER
        context, a failing test and a ticket like any other finding. REPORTER
        already sees everything and can already emit, and it is useless for this
        precisely because it runs after file and verify -- anything it emits is
        written, logged, scored, and never filed.

        By layer, following `_phase_report`, so a second synthesis agent needs a
        prompt and a YAML and no Python.
        """
        synthesis = [s for s in specs.values() if s.layer == "synthesis"]
        if not synthesis:
            return
        # Nothing to join. One finding is not a conjunction, and dispatching a
        # frontier-model context to discover that is a bill for nothing.
        findings = store.envelopes()
        if len(findings) < 2:
            store.log(
                "skipped",
                reason=f"{len(findings)} finding(s); synthesis needs at least two to join",
                agents=[s.name for s in synthesis],
            )
            return

        for spec in synthesis:
            budget.check(reserve=True)
            await self._dispatch(
                spec, store, budget, report,
                tasks.synthesis(self.config, mode, len(findings)), map_version,
            )

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

        before = {e.id for e in store.envelopes()}
        for spec in reporting:
            budget.check()
            await self._dispatch(
                spec, store, budget, report, tasks.report(self.config, mode), map_version
            )

        # A reporting agent CAN emit -- REPORTER holds the envelope server and is
        # told to cite its report with one -- and this phase runs after file and
        # verify, so anything emitted here is written, logged, scored and never
        # filed. Re-running `_phase_file` would put a cycle into the one part of
        # the pipeline that has none, so the orphaning stays, and is said out
        # loud instead: a finding that silently goes nowhere is the worst shape a
        # failure takes here. (A conjunction that deserves a ticket belongs in
        # the synthesis phase, which runs before reproduce for exactly this
        # reason.) No new LedgerKind -- `skipped` already exists.
        orphaned = [e.id for e in store.envelopes() if e.id not in before]
        if orphaned:
            store.log(
                "skipped",
                reason=(
                    "emitted during the reporting phase, after filing; recorded for "
                    "the ledger and the scorecard, not filed"
                ),
                envelope_ids=orphaned,
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

        # Reproduction is the one phase whose cost scales with *findings*, and
        # each dispatch is a fresh frontier-model context. A nightly run on a
        # personal site produced 85 findings -- most of them minor, at high
        # confidence -- and every one of them queued its own context. The run
        # spent $45 and filed nothing.
        #
        # A less severe finding still gets filed: `is_fileable` asks for
        # evidence, confidence and "not not_reproducible", and discovery already
        # supplied the evidence -- `unattempted` passes that gate. What it does
        # not get is a committed failing test, which is the right thing to spend
        # a context on for a blocker and the wrong thing for a trivium.
        #
        # This cannot move `qaas score`: the scorecard reads envelopes, and
        # REPRODUCER emits none. It trades reproduction depth for cost, nothing else.
        floor = self.config.thresholds.reproduce_min_severity
        below = [e for e in drafts if e.severity.rank > floor.rank]
        if below:
            drafts = [e for e in drafts if e.severity.rank <= floor.rank]
            store.log(
                "skipped",
                agent="REPRODUCER",
                reason=(
                    f"{len(below)} findings below {floor.value} are filed on discovery's "
                    f"evidence rather than reproduced (thresholds.reproduce_min_severity)"
                ),
                count=len(below),
                floor=floor.value,
            )
        if not drafts:
            store.log(
                "skipped",
                agent="REPRODUCER",
                reason=f"no finding reached {floor.value}; nothing to reproduce",
            )
            return

        cap = self.config.thresholds.max_findings_per_agent_run
        if len(drafts) > cap:
            # Named first and counted, because this line is the one that explains
            # the bill and it used to arrive third in a list of sixteen.
            note = (
                f"REPRODUCER fan-out capped: {len(drafts)} findings at or above "
                f"{floor.value} exceed the per-run cap of {cap}; reproducing the "
                f"{cap} most severe and filing the rest on discovery's evidence"
            )
            report.escalations.insert(0, note)
            store.log(
                "escalation", agent="REPRODUCER", reason=note, cap=cap, findings=len(drafts)
            )
            drafts = sorted(drafts, key=lambda e: (e.severity.rank, -e.confidence))[:cap]

        jobs = [
            (spec, tasks.reproducer(draft, self.config, self.config.thresholds.flake_runs))
            for draft in drafts
        ]
        # One at a time. Two REPRODUCER invocations are separate contexts but not
        # separate sandboxes: both hold `vcs` and `env_control` against the same
        # `target_root`, so they branch, commit and reset the *same* working tree
        # and the same compose stack. Isolating them properly wants a worktree per
        # finding; serialising them is the cheap correct answer.
        await self._gather(jobs, store, budget, report, map_version, concurrency=1)

    async def _phase_file(self, specs, store, budget, report, map_version) -> None:
        spec = specs.get("TRIAGE")
        if spec is None:
            return
        fileable = [
            e for e in store.envelopes()
            if e.is_fileable(self.config.thresholds.min_confidence_to_file)[0]
        ]
        # A resumed run re-selects every envelope, and `create_issue` never looked
        # at `jira.key` -- so `--from-board`, which resumes by design, filed the
        # same defect a second time and the only thing standing between it and a
        # duplicate storm was TRIAGE remembering to call `search_similar`. The
        # dedupe is a courtesy; this is the gate.
        already = [e for e in fileable if e.jira.key]
        if already:
            fileable = [e for e in fileable if not e.jira.key]
            store.log(
                "skipped", agent="TRIAGE",
                reason=f"{len(already)} finding(s) already carry a ticket from an earlier pass",
                tickets=sorted(e.jira.key for e in already),
            )
        if not fileable:
            store.log("skipped", agent="TRIAGE", reason="nothing passed the gates")
            return
        budget.check()
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

        #: What the last VERIFIER actually observed, carried into the next fix
        #: attempt. Without it `_remediate` re-dispatches FIXER with a
        #: byte-identical prompt and the round trip buys a second bill.
        observed: str = ""

        while True:
            budget.check()
            verdict_mark = len(list(store.ledger("verdict")))
            await self._dispatch(
                specs["VERIFIER"], store, budget, report,
                tasks.verifier(ticket, envelope, branch=fix_branch or repro_branch), map_version,
            )
            entry = self._entry_since(store, "verdict", ticket, verdict_mark)
            verdict = entry.detail.get("verdict") if entry else None
            observed = str(entry.detail.get("observed") or "") if entry else ""

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
            if not await self._remediate(
                envelope, specs, store, budget, report, map_version, observed=observed
            ):
                return
            # Keep the previous branch if this round wrote nothing: a re-verify
            # of the last fix beats silently falling back to the repro branch.
            fix_branch = self._branch_written_since(store, mark) or fix_branch

    async def _remediate(
        self, envelope, specs, store, budget, report, map_version, *, observed: str = ""
    ) -> bool:
        """FIXER -> REVIEWER, bounded. Returns whether a fix is ready to re-verify.

        Phase 3 agents. In a Phase 1 roster neither exists, so a NOT_FIXED
        verdict escalates to a human immediately — which is correct, and much
        better than the loop silently re-running VERIFIER against unchanged code.

        `observed` and the review's concerns are what make this a loop rather
        than a retry. `record_review` refuses REQUEST_CHANGES without `concerns`
        on the stated grounds that "FIXER gets them verbatim" -- and then nothing
        carried them, so round 2 dispatched FIXER with the same two arguments and
        a byte-identical prompt. `max_mender_arbiter_round_trips: 2` bought a
        second attempt at the same coin flip and a second bill.
        """
        ticket = envelope.jira.key
        fixer, reviewer = specs.get("FIXER"), specs.get("REVIEWER")

        if fixer is None:
            self._escalate(report, store, "VERIFIER",
                f"{ticket}: NOT_FIXED and no FIXER in this run's roster. "
                "Nothing here can produce a fix; a human takes it from here")
            return False

        fixer = _with_protected_test(fixer, envelope)
        feedback = observed

        for trip in range(1, self.config.thresholds.max_mender_arbiter_round_trips + 1):
            budget.check()
            await self._dispatch(fixer, store, budget, report,
                                 tasks.fixer(ticket, envelope, feedback=feedback), map_version)
            if reviewer is None:
                return True

            budget.check()
            review_mark = len(list(store.ledger("review")))
            await self._dispatch(reviewer, store, budget, report,
                                 tasks.reviewer(ticket, envelope), map_version)
            entry = self._entry_since(store, "review", ticket, review_mark)
            review = entry.detail.get("decision") if entry else None
            if review == "APPROVE":
                return True
            if review == "ESCALATE_TO_HUMAN":
                self._escalate(report, store, "REVIEWER", f"{ticket}: REVIEWER escalated the fix")
                return False
            if entry is None:
                self._escalate(report, store, "REVIEWER",
                    f"{ticket}: REVIEWER recorded no decision; the fix stays unreviewed")
                return False
            feedback = _review_feedback(entry, observed)
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
    def _succeeded_agents(store) -> set[str]:
        """Agents that finished this run without an error.

        Read back from the ledger rather than tracked in memory, because the
        whole point is to survive a process that died. `agent_finished` carries
        `error`, and an agent that errored produced nothing worth keeping -- a
        rate-limited one most of all.
        """
        return {
            e.agent
            for e in store.ledger("agent_finished")
            if e.agent and not e.detail.get("error")
        }

    @staticmethod
    def _entry_since(store, kind: str, ticket_key: str, mark: int):
        """The entry *this* dispatch recorded, or None if it recorded nothing.

        Scoped to entries added since `mark`, the way `_branch_written_since`
        already is, and for the same reason one step further on. Unscoped, these
        returned the last entry for the ticket from anywhere in the ledger -- so
        a VERIFIER that finished without calling `record_verdict` silently
        inherited the *previous* verdict. That is not only a crash path: the Stop
        hook deliberately lets an agent through after one block, so a silent
        VERIFIER is an ordinary outcome. On a resumed run, where `_phase_verify`
        re-selects every envelope that carries a ticket, it means a VERIFIED
        nobody verified.

        The whole entry, not just the decision, because the reasoning is what
        makes the remediation loop a loop instead of a retry.
        """
        entries = [
            e for e in list(store.ledger(kind))[mark:]
            if e.detail.get("ticket_key") == ticket_key
        ]
        return entries[-1] if entries else None

    def _escalate(self, report, store, agent: str, note: str) -> None:
        report.escalations.append(note)
        store.log("escalation", agent=agent, reason=note)
        self._emit("escalation", agent=agent, reason=note)

    # -- dispatch ---------------------------------------------------------

    async def _gather(
        self, jobs, store, budget, report, map_version, concurrency: int, *, reserve: bool = True
    ) -> None:
        """Run jobs concurrently, but stop dispatching once the budget is gone.

        The semaphore bounds how many run at once; the budget check inside each
        slot means a run that blows its cap stops starting new work rather than
        letting everything already queued through.
        """
        if not jobs:
            return
        sem = asyncio.Semaphore(max(1, concurrency))
        in_flight = min(max(1, concurrency), len(jobs))
        stopped: list[str] = []

        async def one(spec: AgentSpec, task: str) -> None:
            async with sem:
                if stopped:
                    return
                try:
                    budget.check(reserve=reserve)
                except BudgetExceeded as exc:
                    stopped.append(str(exc))
                    return
                await self._dispatch(
                    spec, store, budget, report, task, map_version, slots=in_flight
                )

        await asyncio.gather(*(one(spec, task) for spec, task in jobs))
        if stopped:
            raise BudgetExceeded(stopped[0])

    async def _dispatch(
        self, spec, store, budget, report, task, map_version, *, slots: int = 1
    ) -> RunOutcome:
        ctx = self._context(store, spec, map_version)
        allowance = budget.allowance(spec, slots=slots)

        self._emit("agent_started", agent=spec.name, budget=allowance)
        # `max_wall_clock_s` was a gate checked *between* dispatches and nothing
        # more: once this await was entered, no clock could end it. One wedged
        # agent -- a stuck Playwright session, a Bash call with no timeout of its
        # own, an SDK stream that never yields a ResultMessage -- outlived the
        # run's own cap indefinitely, and `pr-check` advertised a 900-second
        # bound it could not keep. Bounded by what is left of the run's clock, so
        # no agent can outlive the run; floored so a nearly-exhausted budget
        # still gives the agent long enough to record what it has.
        deadline = max(30.0, budget.max_seconds - budget.elapsed)
        try:
            outcome = await asyncio.wait_for(
                run_agent(spec, ctx, task, max_budget_usd=allowance, on_event=self.on_event),
                timeout=deadline,
            )
        except (asyncio.TimeoutError, TimeoutError):
            # Partial work survives: envelopes and artifacts are written through
            # the MCP tools as they happen, not at the end.
            error = f"exceeded the run's remaining wall clock ({deadline:.0f}s)"
            store.log("agent_error", agent=spec.name, error=error)
            result = AgentResult(agent=spec.name, subtype="timeout", error=error)
            store.put_result(result)
            outcome = RunOutcome(result=result)

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
