"""M3/M6 verification: the run state machine, without spending a cent.

`run_agent` is replaced by a fake, so these tests exercise phase ordering, the
budget governor and the loop breakers — the parts that must work correctly
precisely when a real run is going wrong.
"""

from pathlib import Path

import pytest

from qaas import router as conductor_mod
from support import CONFIG_SEARCH

from qaas.router import Budget, BudgetExceeded, Router
from qaas.config import load_config
from qaas.envelope import DefectEnvelope, Domain, ReproStatus, Severity
from qaas.runner import RunOutcome
from qaas.store import AgentResult, RunStore

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return load_config(search=CONFIG_SEARCH)


@pytest.fixture
def fake_agents(monkeypatch):
    """Replace agent invocation with a scripted fake. Records the call order."""
    calls: list[tuple[str, str]] = []
    behaviour: dict[str, dict] = {}

    async def fake_run_agent(spec, ctx, task, *, options=None, max_budget_usd=None, on_event=None):
        calls.append((spec.name, task))
        cfg = behaviour.get(spec.name, {})

        if cfg.get("emit"):
            for i in range(cfg["emit"]):
                ctx.store.put_envelope(
                    DefectEnvelope(
                        run_id=ctx.store.run_id,
                        discovered_by=spec.name,
                        domain=Domain.API,
                        **{"class": "bug"},
                        title=f"{spec.name} finding {i}",
                        summary="Something is wrong.",
                        severity=cfg.get("severity", Severity.MAJOR),
                        confidence=0.9,
                        evidence=[{"type": "log", "uri": "artifact://a/b"}],
                    )
                )
        # A working FIXER ends a round with a commit on its branch. Modelled
        # here because the router now checks for one: a branch with no commit is
        # the "FIXER changed nothing" failure, and before this the fake never
        # wrote a vcs entry at all, so every fix-loop test was exercising the
        # empty-fix path without meaning to. `commits: False` opts into it.
        if spec.name == "FIXER" and cfg.get("commits", True):
            ctx.store.log(
                "vcs", agent="FIXER", action="commit",
                branch=f"fix/{cfg.get('branch', 'scripted')}", sha="0" * 40,
            )
        if cfg.get("publish_map"):
            ctx.maps.put({"services": ["orders-api"], "routes": []})
        if cfg.get("review"):
            # REVIEWER's decision reaches the router through the ledger, the
            # same route record_review takes in a real run.
            ctx.store.log(
                "review", agent=spec.name, ticket_key="CORVID-1",
                decision=cfg["review"], reasoning="scripted",
            )
        if cfg.get("hook"):
            cfg["hook"](ctx, spec)

        result = AgentResult(
            agent=spec.name,
            subtype=cfg.get("subtype", "success"),
            cost_usd=cfg.get("cost", 0.10),
            num_turns=3,
            error=cfg.get("error"),
        )
        ctx.store.put_result(result)
        return RunOutcome(result=result, final_text="done")

    monkeypatch.setattr(conductor_mod, "run_agent", fake_run_agent)
    return calls, behaviour


def make_conductor(cfg, tmp_path) -> Router:
    return Router(cfg, target_root=REPO, root=tmp_path)


# -- the budget governor ----------------------------------------------------


def test_budget_stops_on_spend():
    b = Budget(max_usd=1.0, max_seconds=3600)
    b.spend(0.9)
    b.check()
    b.spend(0.2)
    with pytest.raises(BudgetExceeded, match="spend cap"):
        b.check()


def test_budget_stops_on_wall_clock():
    b = Budget(max_usd=100.0, max_seconds=0)
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        b.check()


def test_agent_allowance_is_capped_by_what_the_run_has_left(cfg):
    b = Budget(max_usd=1.0, max_seconds=3600)
    reproducer = cfg.agents["REPRODUCER"]          # its own cap is $4
    assert b.allowance(reproducer) == pytest.approx(1.0), "the run's remainder wins"
    b.spend(0.7)
    assert b.allowance(reproducer) == pytest.approx(0.3)


def test_allowance_never_goes_to_zero_or_negative(cfg):
    b = Budget(max_usd=1.0, max_seconds=3600)
    b.spend(5.0)
    assert b.allowance(cfg.agents["API"]) > 0


# -- phase ordering ---------------------------------------------------------


async def test_phases_run_in_dependency_order(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2}
    behaviour["BROWSER"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    order = [name for name, _ in calls]

    assert order[0] == "MAPPER", "the map must exist before anything reads it"
    # Discovery runs between the map and triage. Which agents are in it is
    # config -- the roster grew from two to eight -- so assert the ordering,
    # not the membership.
    discovery = order[1:order.index("REPRODUCER")]
    assert {"API", "BROWSER"} <= set(discovery), "the core discovery pair must run"
    assert "MAPPER" not in discovery, "the map agent runs once, before discovery"
    assert order.count("REPRODUCER") == 3, "one REPRODUCER invocation per finding"
    assert order.index("TRIAGE") > order.index("REPRODUCER"), "filing comes after reproduction"
    assert order[-1] == "REPORTER", "reporting runs last, over what the run produced"
    assert report.cost_usd > 0


async def test_forge_is_skipped_when_discovery_found_nothing(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}

    store_root = tmp_path
    await make_conductor(cfg, store_root).run("nightly")
    assert "REPRODUCER" not in [n for n, _ in calls]
    assert "TRIAGE" not in [n for n, _ in calls]


async def test_incident_mode_discovers_but_never_files(cfg, tmp_path, fake_agents):
    """§9: incident runs are diagnostic. Nothing reaches a ticket."""
    calls, behaviour = fake_agents
    behaviour["API"] = {"emit": 2}

    await make_conductor(cfg, tmp_path).run("incident")
    names = [n for n, _ in calls]
    assert "API" in names
    assert "TRIAGE" not in names


async def test_each_forge_invocation_gets_its_own_finding(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 3}

    await make_conductor(cfg, tmp_path).run("pr-check")
    forge_tasks = [task for name, task in calls if name == "REPRODUCER"]
    assert len(forge_tasks) == 3
    assert len({t for t in forge_tasks}) == 3, "each task names a different finding"


# -- loop breakers ----------------------------------------------------------


async def test_a_spend_blowout_stops_the_run_and_escalates(cfg, tmp_path, fake_agents):
    """The governor still works; it just no longer ships with a dollar figure.

    Shipped config sets no `max_budget_usd` -- a price belongs to one vendor and
    this runs against local models too -- so a test about spend has to declare
    the cap it is testing.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True, "cost": 99.0}

    mode = cfg.run_modes["pr-check"].model_copy(update={"max_budget_usd": 10.0})
    capped = cfg.model_copy(update={"run_modes": {**cfg.run_modes, "pr-check": mode}})
    report = await make_conductor(capped, tmp_path).run("pr-check")
    assert report.stopped_early and "spend cap" in report.stopped_early
    assert "TRIAGE" not in [n for n, _ in calls]
    assert report.escalations


async def test_more_findings_than_the_cap_escalates_and_triages_the_worst(cfg, tmp_path, fake_agents):
    """§8.3: past K findings, pause and escalate rather than grind through."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 40}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    forge_calls = sum(1 for n, _ in calls if n == "REPRODUCER")
    assert forge_calls == cfg.thresholds.max_findings_per_agent_run
    assert any("exceed the per-run cap" in e for e in report.escalations)


async def test_a_failing_agent_escalates_without_killing_the_run(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"subtype": "failure", "error": "browser timeout"}
    behaviour["BROWSER"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    assert "API" in report.failed
    assert "REPRODUCER" in [n for n, _ in calls], "BROWSER's finding is still triaged"
    assert any("API failed" in e for e in report.escalations)


async def test_a_missing_map_is_escalated_not_ignored(cfg, tmp_path, fake_agents):
    """Everything downstream trusts the map. Running without one must be loud."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {}  # runs, publishes nothing

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    assert any("published no map" in e for e in report.escalations)


# -- the ledger -------------------------------------------------------------


async def test_the_run_is_fully_recorded_in_the_ledger(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    store = RunStore(report.run_id, tmp_path)
    kinds = [e.kind for e in store.ledger()]

    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "envelope" in kinds
    assert store.total_cost_usd() == pytest.approx(report.cost_usd)


# -- the remediation loop (§8.3) --------------------------------------------


def _filed(store, spec_name="API", key="CORVID-1", branch="fix/x"):
    """A finding that reached a ticket, ready for verification."""
    env = DefectEnvelope(
        run_id=store.run_id, discovered_by=spec_name, domain=Domain.API, **{"class": "bug"},
        title="Orders list ignores limit", summary="Unbounded result set.",
        severity=Severity.MAJOR, confidence=0.9,
        evidence=[{"type": "log", "uri": "artifact://a/b"}],
        reproduction={"status": "reproduced", "failing_test": "qa/repro/t.py::x",
                      "environment": {"branch": branch}},
        jira={"key": key, "project": "CORVID", "status": "In Review"},
    )
    store.put_envelope(env)
    return env


@pytest.fixture
def verdicts(fake_agents):
    """Script VERIFIER's successive verdicts; each dispatch pops the next one."""
    calls, behaviour = fake_agents
    queue: list[str] = []

    def script(*sequence: str) -> None:
        queue.extend(sequence)

    def proof_behaviour(ctx, spec):
        if queue:
            ctx.store.log("verdict", agent="VERIFIER", ticket_key="CORVID-1",
                          verdict=queue.pop(0), observed="...")

    behaviour["VERIFIER"] = {"hook": proof_behaviour}
    return calls, behaviour, script


async def test_a_verified_ticket_ends_the_loop(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("VERIFIED")
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert [n for n, _ in calls].count("VERIFIER") == 1
    assert not report.escalations


async def test_not_fixed_without_a_mender_escalates_immediately(cfg, tmp_path, verdicts):
    """A roster with no fixer must not cycle: re-running VERIFIER on unchanged code
    proves nothing and spends the budget discovering that."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    store = RunStore.new(tmp_path)
    _filed(store)

    # A verify-only roster, which is what every Phase 1 mode still is.
    mode = cfg.run_modes["fix-cycle"].model_copy(update={"agents": ["VERIFIER"]})
    verify_only = cfg.model_copy(update={"run_modes": {**cfg.run_modes, "fix-cycle": mode}})

    report = await Router(verify_only, target_root=REPO, root=tmp_path).run(
        "fix-cycle", run_id=store.run_id
    )
    assert [n for n, _ in calls].count("VERIFIER") == 1, "must not cycle without a fixer"
    assert any("no FIXER" in e for e in report.escalations)


async def test_not_fixed_with_a_mender_runs_the_fix_loop(cfg, tmp_path, verdicts):
    """The Phase 3 path: NOT_FIXED sends the ticket to FIXER, then REVIEWER, then
    back to VERIFIER — bounded by max_proof_reopens."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["REVIEWER"] = {"review": "APPROVE"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    assert order == ["VERIFIER", "FIXER", "REVIEWER", "VERIFIER"], order
    assert not report.escalations


def _mender_pushes(branch: str):
    """FIXER's hook: log the vcs write a real fix round leaves in the ledger."""
    def hook(ctx, spec):
        ctx.store.log("vcs", agent="FIXER", action="create_branch", branch=branch)
        ctx.store.log("vcs", agent="FIXER", action="push", branch=branch)
    return hook


async def test_proof_reverifies_on_menders_branch_not_the_repro_branch(cfg, tmp_path, verdicts):
    """The bug this pins: the envelope names the *repro* branch, which carries
    the failing test and no fix. Re-sending VERIFIER there after a remediation
    round made VERIFIED unreachable -- VERIFIER re-verified the unfixed branch it
    had just failed on, burned a reopen and escalated. A live run recorded
    exactly that."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["REVIEWER"] = {"review": "APPROVE"}
    behaviour["FIXER"] = {"hook": _mender_pushes("fix/CORVID-1-limit")}
    store = RunStore.new(tmp_path)
    _filed(store, branch="qa/repro/61297327-orders-limit-ignored")

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    proof_tasks = [task for name, task in calls if name == "VERIFIER"]
    assert len(proof_tasks) == 2, [n for n, _ in calls]
    assert "qa/repro/61297327-orders-limit-ignored" in proof_tasks[0]
    assert "fix/CORVID-1-limit" in proof_tasks[1]
    assert "qa/repro/61297327-orders-limit-ignored" not in proof_tasks[1].splitlines()[0]
    assert not report.escalations


async def test_a_remediation_round_that_writes_nothing_keeps_the_repro_branch(cfg, tmp_path, verdicts):
    """No vcs write means no fix branch to find. VERIFIER stays where it was rather
    than being sent to some other ticket's branch picked up from the ledger."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["REVIEWER"] = {"review": "APPROVE"}
    behaviour["FIXER"] = {}
    store = RunStore.new(tmp_path)
    # A stale entry from an earlier ticket in the same run: run-wide scanning
    # would hand VERIFIER this branch, which has nothing to do with CORVID-1.
    store.log("vcs", agent="FIXER", action="push", branch="fix/CORVID-99-unrelated")
    _filed(store, branch="qa/repro/orders-limit")

    await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    proof_tasks = [task for name, task in calls if name == "VERIFIER"]
    assert len(proof_tasks) == 2
    assert "qa/repro/orders-limit" in proof_tasks[1]
    assert "CORVID-99" not in proof_tasks[1]


async def test_arbiter_requesting_changes_sends_it_back_to_mender(cfg, tmp_path, verdicts):
    """§8.3: bounded round trips. Two REQUEST_CHANGES exhausts the limit and
    escalates rather than looping until the budget is gone."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    behaviour["REVIEWER"] = {"review": "REQUEST_CHANGES"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    trips = cfg.thresholds.max_mender_arbiter_round_trips
    assert order.count("FIXER") == trips
    assert order.count("REVIEWER") == trips
    assert any("round trips without approval" in e for e in report.escalations)


async def test_arbiter_escalation_stops_the_loop_immediately(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    behaviour["REVIEWER"] = {"review": "ESCALATE_TO_HUMAN"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    assert order.count("FIXER") == 1, "one attempt, then a human takes it"
    assert any("escalated" in e.lower() for e in report.escalations)


async def test_a_regression_blocks_rather_than_retrying(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("REGRESSED")
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert [n for n, _ in calls].count("VERIFIER") == 1
    assert any("REGRESSED" in e for e in report.escalations)


async def test_a_silent_proof_is_escalated_not_treated_as_success(cfg, tmp_path, verdicts):
    """A missing verdict must never read as a pass."""
    calls, behaviour, script = verdicts
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert any("no verdict" in e for e in report.escalations)


async def test_the_reopen_limit_is_honoured(cfg, tmp_path, verdicts, monkeypatch):
    """With a fixer present, NOT_FIXED loops — but only as far as the limit."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "NOT_FIXED", "NOT_FIXED")
    behaviour["FIXER"] = {}
    store = RunStore.new(tmp_path)
    _filed(store)

    cfg2 = cfg.model_copy(deep=True)
    cfg2.agents["FIXER"] = cfg.agents["REPRODUCER"].model_copy(
        update={"name": "FIXER", "layer": "remediation", "must_call": []}
    )
    cfg2.run_modes["fix-cycle"].agents = ["VERIFIER", "FIXER"]

    report = await make_conductor(cfg2, tmp_path).run("fix-cycle", run_id=store.run_id)
    names = [n for n, _ in calls]
    assert names.count("VERIFIER") == cfg.thresholds.max_proof_reopens + 1
    assert names.count("FIXER") == cfg.thresholds.max_proof_reopens
    assert any("the limit" in e for e in report.escalations)


async def test_a_discovery_agent_cannot_certify_its_own_finding(cfg, tmp_path):
    """§2: the finder never grades its own homework.

    A discovery agent claiming `reproduced` in its own envelope would skip REPRODUCER
    entirely — which is not hypothetical: it happened on the first full pipeline
    run and the triage phase was silently skipped for every finding.
    """
    from qaas.mcp import envelope_server
    from qaas.mcp.context import ToolContext, handlers
    from qaas.store import RunStore, SystemMapStore

    store = RunStore.new(root=tmp_path)
    ctx = ToolContext(
        store=store, maps=SystemMapStore(tmp_path), config=cfg,
        agent=cfg.agents["API"], target_root=REPO,
    )
    tools = handlers(envelope_server.build_tools(ctx))
    # Real evidence, stored first: `emit_envelope` now resolves any
    # `artifact://` uri it is handed, because `has_evidence()` was satisfied by
    # a well-formed string naming a file that had never been written.
    uri = store.put_artifact("orders.log", "500 on GET /v1/orders")
    await tools["emit_envelope"]({
        "domain": "api", "class": "bug",
        "title": "Something is broken",
        "summary": "And I have already reproduced it, honestly.",
        "severity": "major", "confidence": 0.95,
        "evidence": [{"type": "log", "uri": uri}],
        "reproduction": {
            "status": "reproduced",
            "steps": ["call the endpoint"],
            "failing_test": "tests/i_made_this_up.py::test_x",
            "verified_by": "API",
        },
    })

    envelope = store.envelopes()[0]
    assert envelope.reproduction.status.value == "unattempted"
    assert envelope.reproduction.failing_test is None
    assert envelope.reproduction.verified_by is None
    assert envelope.reproduction.steps == ["call the endpoint"], "the steps are kept for REPRODUCER"


async def test_concurrent_discovery_agents_do_not_steal_each_others_findings(cfg, tmp_path):
    """Per-agent cost-per-finding is wrong if attribution is 'new since I started'.

    Two discovery agents run concurrently against one store, so a plain set
    difference credits whichever finished last with everything emitted meanwhile.
    """
    from qaas.envelope import DefectEnvelope, Domain, Severity
    from qaas.mcp.context import ToolContext
    from qaas.runner import run_agent
    from qaas.store import RunStore, SystemMapStore

    store = RunStore.new(root=tmp_path)
    maps = SystemMapStore(tmp_path)

    def ctx_for(name: str) -> ToolContext:
        return ToolContext(store=store, maps=maps, config=cfg,
                           agent=cfg.agents[name], target_root=REPO)

    # A neighbour emitted while this agent was working.
    store.put_envelope(DefectEnvelope(
        run_id=store.run_id, discovered_by="BROWSER", domain=Domain.FRONTEND,
        **{"class": "bug"}, title="Neighbour's finding", summary="Emitted by BROWSER.",
        severity=Severity.MINOR, confidence=0.8,
        evidence=[{"type": "log", "uri": "artifact://a/b"}],
    ))

    async def fake_query(**kwargs):
        store.put_envelope(DefectEnvelope(
            run_id=store.run_id, discovered_by="API", domain=Domain.API,
            **{"class": "bug"}, title="Mine", summary="Emitted by API.",
            severity=Severity.MAJOR, confidence=0.9,
            evidence=[{"type": "log", "uri": "artifact://a/b"}],
        ))
        return
        yield  # pragma: no cover

    import qaas.runner as runner_mod
    original = runner_mod.query
    runner_mod.query = fake_query
    try:
        outcome = await run_agent(cfg.agents["API"], ctx_for("API"), "task")
    finally:
        runner_mod.query = original

    assert len(outcome.result.envelope_ids) == 1, "only its own finding"
    assert store.get_envelope(outcome.result.envelope_ids[0]).discovered_by == "API"


async def test_a_resumed_run_keeps_the_budget_it_already_spent(cfg, tmp_path, fake_agents):
    """A cap that resets on resumption is not a cap.

    `qaas run --run-id <existing>` appends to an existing ledger, but the
    governor used to build a fresh Budget starting at zero -- so resuming three
    times against a $20 mode could spend $60 while every individual pass
    reported itself within budget. A real run recorded exactly that:
    `cost $32.90 of $20.00 budget`.

    The wall clock is deliberately not carried across. It measures this process,
    and a run resumed the next morning has not been running all night.
    """
    calls, behaviour = fake_agents
    store = RunStore.new(tmp_path)

    # A first invocation that has already consumed most of the mode's cap.
    capped_mode = cfg.run_modes["pr-check"].model_copy(update={"max_budget_usd": 5.0})
    capped = cfg.model_copy(update={"run_modes": {**cfg.run_modes, "pr-check": capped_mode}})
    store.put_result(AgentResult(agent="API", subtype="success", cost_usd=4.95, num_turns=1))
    assert store.total_cost_usd() == pytest.approx(4.95)

    behaviour["MAPPER"] = {"cost": 1.0, "publish_map": True}
    # The shipped config sets no cap, so this test supplies one -- the governor
    # is still there, it just no longer bakes a dollar figure into defaults.
    report = await make_conductor(capped, tmp_path).run("pr-check", run_id=store.run_id)

    assert report.stopped_early, "the resumed run ignored what the run had already spent"
    assert "spend cap" in report.stopped_early


async def test_a_new_discovery_agent_runs_without_a_hand_written_task(cfg, tmp_path, fake_agents):
    """The architecture's central claim, finally tested.

    `_phase_discover` used to dispatch from a closed dict of task builders, so a
    discovery agent that was not in it got `skipped: no task builder` -- it
    passed `qaas validate`, assembled correctly, appeared in `--dry-run`, and
    then silently did nothing. DBA and AUDITOR were added as a prompt plus a
    YAML each and found exactly that.

    A silent skip is the worst shape this failure could take, which is why this
    test asserts the agent RAN rather than asserting the config loaded.
    """
    calls, behaviour = fake_agents
    report = await make_conductor(cfg, tmp_path).run("nightly")
    ran = {name for name, _ in calls}
    assert {"DBA", "AUDITOR"} <= ran, f"a config-only agent was skipped: {sorted(ran)}"


async def test_an_agent_the_target_cannot_support_is_not_dispatched(cfg, tmp_path, fake_agents):
    """`qaas doctor` has always reported "agents that cannot: BROWSER" for a
    target with no reachable UI. Nothing acted on it: the run dispatched BROWSER
    anyway and spent its budget looking for a browser that was never there.

    Being told an agent cannot work and then watching it run is worse than not
    being told, which is why this asserts the dispatch, not the report.
    """
    calls, behaviour = fake_agents
    from qaas.target import Environment, TargetProfile

    static_only = TargetProfile(name="static", root=".", environment=Environment(mode="none"))
    cfg2 = cfg.model_copy(update={"profile": static_only})

    await make_conductor(cfg2, tmp_path).run("pr-check")
    ran = {name for name, _ in calls}
    assert "BROWSER" not in ran, "dispatched BROWSER at a target with no UI"
    assert "API" in ran, "static analysis agents must still run"


def _files_tickets(ctx, spec):
    """A fake TRIAGE that stamps ticket keys, so the verify phase has work."""
    for i, envelope in enumerate(ctx.store.envelopes(), start=1):
        if not envelope.jira.key:
            envelope.jira.key = f"PROJ-{i}"
            ctx.store.put_envelope(envelope)


async def test_only_the_tickets_that_reach_remediation_pay_for_a_test(
    cfg, tmp_path, fake_agents
):
    """Reproduction is scheduled by whoever will consume the test.

    It ran as a phase over every finding above the floor, and on a real run that
    meant twenty contexts producing twenty committed failing tests of which
    three were ever executed -- ~$56 of a $76 run, and 87% of a three-hour cap
    spent preparing work, leaving twenty minutes to do it.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 4, "severity": Severity.BLOCKER}

    # Only two of the four findings become tickets, so only two are worth a test.
    def _file_two(ctx, spec):
        for i, envelope in enumerate(ctx.store.envelopes()[:2], start=1):
            envelope.jira.key = f"PROJ-{i}"
            ctx.store.put_envelope(envelope)

    behaviour["TRIAGE"] = {"hook": _file_two}

    await make_conductor(cfg, tmp_path).run("full-loop")
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 2, (
        "a finding that never reaches a fix cycle must not buy a context"
    )


async def test_reproduction_happens_before_the_ticket_is_verified(cfg, tmp_path, fake_agents):
    """Order matters: VERIFIER reads the repro branch off the envelope."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1, "severity": Severity.BLOCKER}
    behaviour["TRIAGE"] = {"hook": _files_tickets}

    await make_conductor(cfg, tmp_path).run("full-loop")
    order = [n for n, _ in calls]
    assert order.index("REPRODUCER") < order.index("VERIFIER")


async def test_a_roster_with_no_fix_loop_still_reproduces_eagerly(cfg, tmp_path, fake_agents):
    """`pr-check` and `nightly` carry REPRODUCER without VERIFIER.

    There the committed failing test is the deliverable rather than an input,
    and there is no fix loop for it to starve. Making reproduction lazy in
    *every* roster would have silently stopped those modes producing the thing
    they exist to hand a human.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 3, "severity": Severity.BLOCKER}

    await make_conductor(cfg, tmp_path).run("nightly")
    assert "VERIFIER" not in {s.name for s in cfg.enabled_agents("nightly")}
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 3


async def test_a_finding_below_the_floor_still_gets_its_fix_cycle(cfg, tmp_path, fake_agents):
    """The floor decides whether a ticket earns a committed test, not whether
    it earns a fix. VERIFIER falls back to exercising the running application,
    which it already does and already says so when it does."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1, "severity": Severity.MINOR}
    behaviour["TRIAGE"] = {"hook": _files_tickets}

    await make_conductor(cfg, tmp_path).run("full-loop")
    names = [n for n, _ in calls]
    assert "REPRODUCER" not in names, "minor is below the floor"
    assert "VERIFIER" in names, "but it is still a ticket, and still gets verified"


async def test_a_fixer_that_changed_nothing_escalates_instead_of_being_reviewed(
    cfg, tmp_path, verdicts
):
    """The failure this system keeps producing, caught where it happens.

    FIXER reporting success having changed nothing has three known causes --
    write paths matching no file in the target, a diff budget narrower than the
    fix, and a branch created but never committed to -- and all three reached
    REVIEWER as "there is no fix to review", after a second frontier-model
    context had been paid for. On one full-loop run every remaining ticket
    ended that way.

    `_branch_written_since` cannot catch it: `create_branch` puts a branch name
    in the ledger, so an empty branch looks exactly like a real one.
    """
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    behaviour["FIXER"] = {"commits": False, "hook": lambda ctx, spec: ctx.store.log(
        "vcs", agent="FIXER", action="create_branch", branch="fix/empty")}
    behaviour["REVIEWER"] = {"review": "APPROVE"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)

    assert "REVIEWER" not in {n for n, _ in calls}, (
        "REVIEWER must not be paid to discover an empty branch"
    )
    assert any("changed nothing" in e for e in report.escalations), report.escalations


async def test_the_escalation_names_the_diff_budget_when_that_is_the_cause(
    cfg, tmp_path, verdicts
):
    """A budget refusal means the fix is wider than the envelope, not wrong.

    That distinction is why the refusal is named: a human widens
    `max_diff_files` for this target or splits the ticket, rather than going to
    look for a bad fix that was never written.
    """
    calls, behaviour, script = verdicts
    script("NOT_FIXED")

    def _blocked(ctx, spec):
        ctx.store.log("vcs", agent="FIXER", action="create_branch", branch="fix/blocked")
        ctx.store.log(
            "denial", agent="FIXER", tool="Edit",
            reason="FIXER has already changed 5 files, which is its limit of 5 (§8.2). "
                   "A fix this wide is outside the autonomy envelope.",
        )

    behaviour["FIXER"] = {"commits": False, "hook": _blocked}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    said = " ".join(report.escalations)
    assert "wider than the envelope" in said, said
    assert "max_diff_files" in said, said


async def test_a_ticket_already_verified_is_not_verified_again(cfg, tmp_path, fake_agents):
    """Resuming must not re-open a ticket this run already closed.

    Every other phase learned this and this one had not: map skips a published
    map, file skips envelopes already carrying a key, reproduce re-selects only
    `unattempted`. `_phase_verify` took every ticketed envelope, so resuming the
    full-loop run that stopped on its wall clock -- one ticket closed, one
    NOT_FIXED, five never touched -- would have re-verified the closed one.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1, "severity": Severity.BLOCKER}
    behaviour["TRIAGE"] = {"hook": _files_tickets}

    first = await make_conductor(cfg, tmp_path).run("full-loop")
    store = RunStore(first.run_id, root=tmp_path, create=False)
    ticket = next(e.jira.key for e in store.envelopes() if e.jira.key)
    store.log("verdict", agent="VERIFIER", ticket_key=ticket,
              verdict="VERIFIED", observed="scripted")

    calls.clear()
    await make_conductor(cfg, tmp_path).run("full-loop", run_id=first.run_id)
    assert "VERIFIER" not in {n for n, _ in calls}


async def test_a_ticket_left_unfinished_is_attempted_again(cfg, tmp_path, fake_agents):
    """NOT_FIXED is unfinished work, not a settled answer.

    Only VERIFIED is terminal. A run cut off mid-remediation is exactly what a
    resume is for, so anything short of proven-fixed has to come back round.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1, "severity": Severity.BLOCKER}
    behaviour["TRIAGE"] = {"hook": _files_tickets}

    first = await make_conductor(cfg, tmp_path).run("full-loop")
    store = RunStore(first.run_id, root=tmp_path, create=False)
    ticket = next(e.jira.key for e in store.envelopes() if e.jira.key)
    store.log("verdict", agent="VERIFIER", ticket_key=ticket,
              verdict="NOT_FIXED", observed="scripted")

    calls.clear()
    await make_conductor(cfg, tmp_path).run("full-loop", run_id=first.run_id)
    assert "VERIFIER" in {n for n, _ in calls}


async def test_every_shipped_agent_is_actually_dispatchable(cfg, tmp_path, fake_agents):
    """No agent may validate, assemble, and then silently do nothing.

    This is the generalisation of two real bugs. DBA and AUDITOR were added as
    prompt + YAML and were skipped with `no task builder`, because
    `_phase_discover` dispatched from a closed dict. REPORTER would have been
    skipped for a different reason -- every phase except discovery dispatches by
    NAME, and nothing asked for the reporting layer.

    Both failures look identical from outside: `qaas validate` passes, the agent
    appears in `--dry-run`, and the run reports success having never called it.
    That is the worst shape a failure can take here, so this asserts dispatch
    for the whole roster rather than for the agents someone remembered.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    # Two findings, not one: SYNTHESIZER's whole job is the join, and a phase
    # that dispatched a frontier-model context to join a single finding would be
    # a bill for nothing. It skips below two and says so in the ledger, which is
    # the sanctioned shape (`_phase_reproduce` skips the same way) -- but it does
    # mean this test has to supply something joinable to prove dispatch.
    behaviour["API"] = {"emit": 2}

    # TRIAGE has to actually file, because REPRODUCER is no longer dispatched by
    # a phase of its own in a roster that has a fix loop -- `_verify_loop`
    # reproduces the ticket it is about to work on, so nothing reaches
    # REPRODUCER until something carries a ticket key. Without this the test
    # failed for a fixture reason while the real pipeline was fine, which is the
    # opposite of what it is for.
    def _file_them(ctx, spec):
        for i, envelope in enumerate(ctx.store.envelopes(), start=1):
            if not envelope.jira.key:
                envelope.jira.key = f"PROJ-{i}"
                ctx.store.put_envelope(envelope)

    behaviour["TRIAGE"] = {"hook": _file_them}

    await make_conductor(cfg, tmp_path).run("full-loop")
    dispatched = {name for name, _ in calls}

    # FIXER and REVIEWER need a NOT_FIXED verdict, which the fake VERIFIER has
    # no way to record; they are covered by the fix-loop tests instead.
    expected = {s.name for s in cfg.enabled_agents("full-loop")} - {"FIXER", "REVIEWER"}
    missing = sorted(expected - dispatched)
    assert not missing, (
        f"{missing} are configured into full-loop and were never dispatched. "
        "An agent that loads but never runs is the failure this test exists for."
    )


# -- what reproduction is worth spending a context on -----------------------
#
# Reproduction is the only phase whose cost scales with findings, and each
# dispatch is a fresh frontier-model context. A nightly run on a personal site
# produced 85 findings, most of them minor at high confidence, and spent $45
# without filing anything. These pin the gate that stops that.


async def test_a_finding_below_the_floor_is_filed_but_not_reproduced(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 6, "severity": Severity.MINOR}

    report = await make_conductor(cfg, tmp_path).run("nightly")

    assert "REPRODUCER" not in [n for n, _ in calls], "minor findings earn no context"
    # Filing is unaffected: `is_fileable` wants evidence, confidence and "not
    # not_reproducible", and `unattempted` passes. Discovery already produced
    # the evidence, so the finding still reaches TRIAGE.
    assert "TRIAGE" in [n for n, _ in calls]
    assert report.stopped_early is None


async def test_the_skip_says_how_many_and_which_knob(cfg, tmp_path, fake_agents):
    _, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 4, "severity": Severity.TRIVIAL}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    store = RunStore(report.run_id, root=tmp_path, create=False)
    skips = [e for e in store.ledger("skipped") if e.agent == "REPRODUCER"]

    assert any(e.detail.get("count") == 4 for e in skips)
    assert any("reproduce_min_severity" in (e.detail.get("reason") or "") for e in skips)


async def test_naming_a_ticket_reproduces_that_one_and_not_its_eight_neighbours(
    cfg, tmp_path, fake_agents
):
    """`--ticket` means the same thing in reproduce as it does in verify.

    It scoped verify and not this, so "take one ticket end to end" opened a
    fresh frontier-model context for every unreproduced finding in the run and
    reproduced the other eight on the way to the one that was asked for. On a
    real run that is the difference between one context and nine.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 3, "severity": Severity.BLOCKER}

    first = await make_conductor(cfg, tmp_path).run("nightly")
    store = RunStore(first.run_id, root=tmp_path, create=False)

    # Stamp the keys the tracker would have written, and put the findings back
    # to unattempted so the second pass has something to reproduce.
    keys = []
    for i, envelope in enumerate(store.envelopes(), start=1):
        envelope.jira.key = f"PROJ-{i}"
        envelope.reproduction.status = ReproStatus.UNATTEMPTED
        store.put_envelope(envelope)
        keys.append(envelope.jira.key)
    assert len(keys) >= 2, "this test needs more than one finding to be about anything"

    calls.clear()
    router = Router(cfg, target_root=REPO, root=tmp_path, tickets={keys[0]})
    await router.run("nightly", run_id=first.run_id)

    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 1


async def test_a_ticket_no_envelope_carries_does_not_silence_the_phase(cfg, tmp_path, fake_agents):
    """On a first pass nothing is filed yet.

    An empty scope there has to mean "reproduce everything", not "reproduce
    nothing" -- the keys simply do not exist until TRIAGE has run.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2, "severity": Severity.BLOCKER}

    router = Router(cfg, target_root=REPO, root=tmp_path, tickets={"NOBODY-1"})
    await router.run("nightly")
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 2


async def test_severe_findings_are_still_reproduced_one_context_each(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 3, "severity": Severity.BLOCKER}

    await make_conductor(cfg, tmp_path).run("nightly")
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 3


async def test_a_mixed_run_reproduces_only_what_clears_the_floor(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2, "severity": Severity.CRITICAL}
    behaviour["BROWSER"] = {"emit": 7, "severity": Severity.MINOR}

    await make_conductor(cfg, tmp_path).run("nightly")
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 2


async def test_lowering_the_floor_restores_the_old_behaviour(cfg, tmp_path, fake_agents):
    """One line in system.yaml buys back pre-0.0.2 reproduction of everything."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 5, "severity": Severity.TRIVIAL}

    loose = cfg.model_copy(
        update={"thresholds": cfg.thresholds.model_copy(
            update={"reproduce_min_severity": Severity.TRIVIAL})}
    )
    await make_conductor(loose, tmp_path).run("nightly")
    assert sum(1 for n, _ in calls if n == "REPRODUCER") == 5


async def test_the_fan_out_escalation_leads_the_list(cfg, tmp_path, fake_agents):
    """It is the line that explains the bill; it used to arrive third of sixteen."""
    _, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 40, "severity": Severity.BLOCKER}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    assert report.escalations, "a capped fan-out must be reported"
    assert "REPRODUCER fan-out capped" in report.escalations[0]
    assert str(cfg.thresholds.max_findings_per_agent_run) in report.escalations[0]


# -- the loop is a loop -----------------------------------------------------


def test_a_silent_verifier_does_not_inherit_the_previous_verdict(cfg, tmp_path):
    """`_latest_verdict` scanned the whole ledger and took the last entry.

    A VERIFIER that ends without calling `record_verdict` is a normal path, not
    only a crash: the Stop hook deliberately lets an agent through after one
    block. Unscoped, that agent silently inherited whatever verdict was recorded
    last -- and on a resumed run, where `_phase_verify` re-selects every envelope
    carrying a ticket, that is a VERIFIED nobody verified.
    """
    store = RunStore.new(root=tmp_path)
    store.log("verdict", agent="VERIFIER", ticket_key="CORVID-1", verdict="VERIFIED")
    mark = len(list(store.ledger("verdict")))

    # This dispatch records nothing.
    assert Router._entry_since(store, "verdict", "CORVID-1", mark) is None

    store.log("verdict", agent="VERIFIER", ticket_key="CORVID-1", verdict="NOT_FIXED")
    entry = Router._entry_since(store, "verdict", "CORVID-1", mark)
    assert entry is not None and entry.detail["verdict"] == "NOT_FIXED"


async def test_a_second_fix_attempt_is_told_what_the_first_got_wrong(cfg, tmp_path, fake_agents):
    """`_remediate` was a retry, not a loop.

    `record_review` refuses REQUEST_CHANGES without `concerns` on the stated
    grounds that "FIXER gets them verbatim" -- and then nothing carried them, so
    round two dispatched FIXER with a byte-identical prompt. Two round trips
    bought a second attempt at the same coin flip and a second bill.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1}

    def file_the_ticket(ctx, spec):
        envelope = ctx.store.envelopes()[0]
        ctx.store.put_envelope(
            envelope.model_copy(update={"jira": envelope.jira.model_copy(
                update={"key": "CORVID-1"})})
        )

    behaviour["TRIAGE"] = {"hook": file_the_ticket}
    behaviour["VERIFIER"] = {"hook": lambda ctx, spec: ctx.store.log(
        "verdict", agent="VERIFIER", ticket_key="CORVID-1", verdict="NOT_FIXED",
        observed="the original test still fails at line 12",
    )}
    behaviour["REVIEWER"] = {"review": "REQUEST_CHANGES"}

    await make_conductor(cfg, tmp_path).run("full-loop")

    fixer_tasks = [task for name, task in calls if name == "FIXER"]
    assert len(fixer_tasks) >= 2, "the round trip did not happen"
    assert fixer_tasks[0] != fixer_tasks[1], (
        "round two got a byte-identical prompt; the round trip bought nothing"
    )
    assert "the original test still fails at line 12" in fixer_tasks[0]
    assert "REVIEWER" in fixer_tasks[1]


# -- a human answers an escalation ------------------------------------------
#
# Escalation is a designed terminal state and nothing could end one. CORVID-7 --
# REVIEWER escalated a correct one-line fix on a product question ("applying it
# exposes a UI regression already filed as another ticket; ship now or hold?")
# and the answer had to be typed into a Python script calling the tracker
# adapter. QAAS-31 -- REVIEWER escalated because the fix lay outside FIXER's
# write_paths, correctly refusing to REQUEST_CHANGES, since demanding a change
# the author may not make deadlocks the loop.
#
# `qaas answer` appends one `human_decision` line. These tests are about what
# the router does with it: it schedules nothing new, and the answer reaches the
# two agents that would otherwise ask the same question again.


def _loop_behaviour(behaviour, *, ticket="CORVID-1"):
    """A run that files one ticket and then fails to verify it."""
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1}

    def file_the_ticket(ctx, spec):
        envelope = ctx.store.envelopes()[0]
        ctx.store.put_envelope(
            envelope.model_copy(update={"jira": envelope.jira.model_copy(update={"key": ticket})})
        )

    behaviour["TRIAGE"] = {"hook": file_the_ticket}
    behaviour["VERIFIER"] = {"hook": lambda ctx, spec: ctx.store.log(
        "verdict", agent="VERIFIER", ticket_key=ticket, verdict="NOT_FIXED",
        observed="the original test still fails",
    )}
    behaviour["REVIEWER"] = {"review": "ESCALATE_TO_HUMAN"}


async def test_a_human_answer_reaches_both_agents_that_asked(cfg, tmp_path, fake_agents):
    """CORVID-7's shape: answered once, and every later run has to know.

    FIXER already had a slot (`feedback`); REVIEWER had none at all -- so the
    reviewer that raised the question re-raised it on the next run having never
    been told the answer, and a person answered the same escalation once per
    run. Both are assembled in Python out of the typed ledger line, the way
    `_review_feedback` is: no new tool, nothing an agent writes.
    """
    calls, behaviour = fake_agents
    _loop_behaviour(behaviour)
    report = await make_conductor(cfg, tmp_path).run("full-loop")
    assert any("REVIEWER escalated" in e for e in report.escalations)

    # What `qaas answer` writes, from a process with no agent in it.
    store = RunStore(report.run_id, tmp_path, create=False)
    store.log(
        "human_decision", ticket_key="CORVID-1", decision="proceed",
        note="Ship it; the UI regression is already filed as CORVID-9.",
        author="a human",
    )

    calls.clear()
    behaviour["API"] = {}
    await make_conductor(cfg, tmp_path).run("full-loop", run_id=report.run_id)

    fixer = [task for name, task in calls if name == "FIXER"]
    reviewer = [task for name, task in calls if name == "REVIEWER"]
    assert fixer and reviewer, "the loop did not reopen the ticket"
    assert "already filed as CORVID-9" in fixer[0]
    assert "already filed as CORVID-9" in reviewer[0]


async def test_a_held_ticket_is_not_verified_again(cfg, tmp_path, fake_agents):
    """QAAS-31's shape: the fix is human work, so the run must stop touching it.

    Gated before VERIFIER rather than inside the fix loop, because a held ticket
    re-verified every night is the cost the escalation was already imposing. It
    is a filter on *work*, exactly like `--from-board`: which tickets this run
    touches, never the order or the dispatch.
    """
    calls, behaviour = fake_agents
    _loop_behaviour(behaviour)
    report = await make_conductor(cfg, tmp_path).run("full-loop")

    store = RunStore(report.run_id, tmp_path, create=False)
    store.log(
        "human_decision", ticket_key="CORVID-1", decision="hold",
        note="the fix is outside FIXER's write_paths; I will make it by hand",
        author="a human",
    )

    calls.clear()
    behaviour["API"] = {}
    second = await make_conductor(cfg, tmp_path).run("full-loop", run_id=report.run_id)

    assert "VERIFIER" not in {name for name, _ in calls}, "a held ticket cost a dispatch"
    assert "FIXER" not in {name for name, _ in calls}
    assert not second.escalations, "a ticket a human has parked is not still blocked"
    skipped = [e for e in store.ledger("skipped") if e.detail.get("ticket_key") == "CORVID-1"]
    assert skipped and "held by a human" in skipped[-1].detail["reason"]


def test_the_latest_answer_is_the_one_that_stands(tmp_path):
    """A decision belongs to the ticket and stands until a human replaces it.

    Deliberately not scoped to a mark, unlike `_entry_since`: if it expired with
    the run that received it, the next fix cycle would re-ask the question that
    was already answered.
    """
    from qaas.router import _human_answer

    store = RunStore.new(root=tmp_path)
    assert _human_answer(store, "CORVID-1") is None
    store.log("human_decision", ticket_key="CORVID-1", decision="hold", note="wait")
    store.log("human_decision", ticket_key="CORVID-2", decision="proceed", note="other ticket")
    store.log("human_decision", ticket_key="CORVID-1", decision="proceed", note="released")

    answer = _human_answer(store, "CORVID-1")
    assert answer is not None and answer.decision == "proceed" and answer.note == "released"


async def test_an_escalation_names_the_ticket_it_blocks(cfg, tmp_path, fake_agents):
    """`qaas escalations` keys on this field.

    It can be recovered from the reason -- every verify-loop escalation leads
    with `f"{ticket}: ..."` -- and the reader still does that for the ledgers
    written before the field existed. Prose is not a field, though: reword one
    note and the queue quietly empties.
    """
    _, behaviour = fake_agents
    _loop_behaviour(behaviour)
    report = await make_conductor(cfg, tmp_path).run("full-loop")

    store = RunStore(report.run_id, tmp_path, create=False)
    blocked = [e for e in store.ledger("escalation") if e.detail.get("ticket_key")]
    assert blocked and blocked[-1].detail["ticket_key"] == "CORVID-1"
    # And a fan-out or agent-failure escalation carries no key rather than a
    # null one: a file people read by eye should not be full of `ticket_key:
    # null`.
    assert all("ticket_key" in e.detail or "CORVID-1" not in str(e.detail.get("reason"))
               for e in store.ledger("escalation"))


def test_fixer_may_not_rewrite_the_test_that_defines_success(cfg):
    """§10's symptom-fix guard had nothing behind it.

    `protected_paths` is set by no agent YAML and was set by nothing else, so
    `_protected_path` always returned False -- while `fixer.yaml` grants write
    access to `qa/repro`, which is where the failing test lives.
    """
    from qaas.router import _with_protected_test

    envelope = DefectEnvelope(
        run_id="r", discovered_by="API", domain=Domain.API, **{"class": "bug"},
        title="t", summary="s", severity=Severity.MAJOR, confidence=0.9,
        evidence=[{"type": "log", "uri": "artifact://a/b"}],
        reproduction={"status": "reproduced",
                      "failing_test": "qa/repro/test_orders.py::test_tenant_leak"},
    )
    guarded = _with_protected_test(cfg.agents["FIXER"], envelope)
    assert "qa/repro/test_orders.py" in guarded.policy.protected_paths
    # The shared roster must not be mutated: a later ticket would inherit this one.
    assert not cfg.agents["FIXER"].policy.protected_paths


async def test_a_resumed_run_does_not_file_the_same_finding_twice(cfg, tmp_path, fake_agents):
    """`--from-board` resumes by design, and `_phase_file` re-selected everything.

    `create_issue` never looked at `jira.key` either, so the only thing between a
    resume and a duplicate storm was TRIAGE remembering to call `search_similar`.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1, "hook": lambda ctx, spec: None}

    router = make_conductor(cfg, tmp_path)
    report = await router.run("pr-check")

    store = RunStore(report.run_id, tmp_path, create=False)
    envelope = store.envelopes()[0]
    store.put_envelope(
        envelope.model_copy(update={"jira": envelope.jira.model_copy(update={"key": "CORVID-9"})})
    )

    # Nothing new on the resume, so the only fileable finding is the one that is
    # already filed. TRIAGE must not be dispatched at all.
    calls.clear()
    behaviour["API"] = {}
    await make_conductor(cfg, tmp_path).run("pr-check", run_id=report.run_id)

    assert "TRIAGE" not in {name for name, _ in calls}, (
        "TRIAGE was dispatched over a finding that already carries a ticket"
    )
    skips = [
        e for e in store.ledger("skipped")
        if "already carry a ticket" in str(e.detail.get("reason", ""))
    ]
    assert skips, "the skip was silent; it has to say what it held back"
    assert skips[-1].detail["tickets"] == ["CORVID-9"]


async def test_a_verified_fix_makes_the_next_sighting_a_regression(cfg, tmp_path, fake_agents):
    """End to end, the loop that was unreachable in every shipped roster.

    REGRESSION fires only on `resolved_at`; its only writer was the
    `mark_resolved` *tool*; and VERIFIER, the one agent that closes a ticket, has
    no `defect_memory` server. So the single most valuable thing a system with a
    memory can say -- "this was fixed and it came back" -- could not be said in
    any configuration qaas ships with. The router writes it now, which is also
    what keeps an agent from being able to mark its own work resolved.
    """
    from qaas.mcp import defect_memory

    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 1}

    def file_the_ticket(ctx, spec):
        envelope = ctx.store.envelopes()[0]
        ctx.store.put_envelope(
            envelope.model_copy(
                update={"jira": envelope.jira.model_copy(update={"key": "CORVID-1"})}
            )
        )

    behaviour["TRIAGE"] = {"hook": file_the_ticket}
    behaviour["VERIFIER"] = {"hook": lambda ctx, spec: ctx.store.log(
        "verdict", agent="VERIFIER", ticket_key="CORVID-1", verdict="VERIFIED",
    )}

    report = await make_conductor(cfg, tmp_path).run("full-loop")

    store = RunStore(report.run_id, tmp_path, create=False)
    fingerprint = store.envelopes()[0].fingerprint()

    conn = defect_memory.connect(tmp_path)
    try:
        row = conn.execute(
            "SELECT resolved_at FROM defects WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
    finally:
        conn.close()
    # `record` only runs if TRIAGE called it; what the router must have written
    # either way is the outcome, and the resolution where the defect is known.
    outcomes = {o["outcome"] for o in defect_memory.outcomes_for(tmp_path, fingerprint)}
    assert "verified" in outcomes, (
        "the run verified a fix and recorded nothing a later run can read"
    )
    if row is not None:
        assert row["resolved_at"], "resolved_at is what makes a recurrence a regression"


async def test_a_resumed_run_does_not_repeat_the_agents_that_succeeded(
    cfg, tmp_path, fake_agents
):
    """The change that makes a failed run cheap to finish.

    `--run-id` re-dispatched every discovery agent, so a run that died at the
    twelfth of thirteen cost all thirteen to retry — resume was useless exactly
    when it was needed. Three separate failures (a misconfigured tracker, a git
    bug, an account rate limit) each paid full price and none of them produced a
    score.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    first = {name for name, _ in calls}
    assert "API" in first and "ARCHITECT" in first

    calls.clear()
    await make_conductor(cfg, tmp_path).run("pr-check", run_id=report.run_id)
    second = {name for name, _ in calls}

    assert "API" not in second, "a successful agent was asked to run again"
    assert "ARCHITECT" not in second
    # MAPPER reads the whole repository and is the most expensive agent in the
    # roster; the map it publishes is versioned and pinned, so re-running it on
    # resume buys an identical artifact at full price.
    assert "MAPPER" not in second, "a resumed run re-mapped the repository"

    store = RunStore(report.run_id, tmp_path, create=False)
    assert any(
        "already completed in this run" in str(e.detail.get("reason", ""))
        for e in store.ledger("skipped")
    ), "the skip was silent"


async def test_an_agent_that_errored_is_retried_on_resume(cfg, tmp_path, fake_agents):
    """Only *successful* finishes are skipped.

    A rate-limited agent produced nothing, and that is the case this whole
    change exists for — skipping it would make resume useless in the one
    situation it was built to survive.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"subtype": "failure", "error": "session limit"}

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    calls.clear()
    behaviour["API"] = {}
    await make_conductor(cfg, tmp_path).run("pr-check", run_id=report.run_id)

    assert "API" in {name for name, _ in calls}, "the failed agent was not retried"


# -- the provider stops serving ---------------------------------------------
#
# Measured, not hypothetical. `run-20260919T152757-4c8c37` on the demo app: 8
# agents, 2h45m, $56.58, 34 findings, discovery complete -- and then TRIAGE and
# ten REPRODUCER invocations all died within seconds of each other on "You've
# hit your session limit · resets 4:20pm". Eleven escalations, zero tickets. The
# findings survived on disk and were filed by hand, hours later. These pin the
# three things that were missing: recognising it, not repeating it, and saying
# that the run can be picked back up.

QUOTA_ERROR = (
    "ResultError: Claude Code returned an error result: "
    "You've hit your session limit · resets 4:20pm"
)


def test_a_quota_refusal_is_told_apart_from_a_failure():
    from qaas.router import is_quota_error

    assert is_quota_error(QUOTA_ERROR)
    assert is_quota_error("HTTP 429: Too Many Requests")
    assert is_quota_error("usage limit reached")
    assert not is_quota_error(None)
    assert not is_quota_error("AssertionError: expected 200, got 500")


def test_the_runs_own_timeout_is_not_mistaken_for_a_quota():
    """`_dispatch` writes `exceeded the run's remaining wall clock (429s)`.

    A bare "429" in the marker list made that text quota-shaped, so a run could
    stop itself on "the provider is refusing" because of how many seconds were
    left on its own clock.
    """
    from qaas.router import is_quota_error

    assert not is_quota_error("exceeded the run's remaining wall clock (429s)")


async def test_quota_stops_the_run_before_the_next_phase(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"subtype": "failure", "error": QUOTA_ERROR}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    names = [n for n, _ in calls]

    assert names == ["MAPPER"], "the rest of the roster walked into the same wall"
    assert report.quota_exhausted
    assert report.stopped_early and "quota" in report.stopped_early


async def test_only_one_line_says_it_not_one_per_agent(cfg, tmp_path, fake_agents):
    """Eleven escalations saying the same thing is a fact recorded and not acted on."""
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 3}
    behaviour["REPRODUCER"] = {"subtype": "failure", "error": QUOTA_ERROR}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    names = [n for n, _ in calls]
    store = RunStore(report.run_id, tmp_path, create=False)

    assert names.count("REPRODUCER") == 1, "the second finding bought the same answer again"
    assert "TRIAGE" not in names, "filing was dispatched into the limit that stopped reproduction"
    assert "REPORTER" not in names
    quota = list(store.ledger("quota_exhausted"))
    assert len(quota) == 1
    assert quota[0].detail["unfiled_findings"] == 3
    # Not an escalation: an escalation means a human must look at a *finding*.
    assert not [
        e for e in store.ledger("escalation")
        if "session limit" in str(e.detail.get("reason", ""))
    ]


async def test_a_quota_stop_still_closes_the_ledger(cfg, tmp_path, fake_agents):
    """A run with `run_started` and no `run_finished` reads as "still running", forever."""
    _, behaviour = fake_agents
    behaviour["MAPPER"] = {"subtype": "failure", "error": QUOTA_ERROR}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    store = RunStore(report.run_id, tmp_path, create=False)

    finished = list(store.ledger("run_finished"))
    assert len(finished) == 1
    assert finished[0].detail["quota_exhausted"] is True
    assert finished[0].detail["resume"] == report.resume_command


async def test_the_summary_names_the_command_that_picks_it_back_up(cfg, tmp_path, fake_agents):
    _, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2}
    behaviour["REPRODUCER"] = {"subtype": "failure", "error": QUOTA_ERROR}

    report = await make_conductor(cfg, tmp_path).run("nightly")

    assert report.resume_command == f"qaas run --mode nightly --run-id {report.run_id}"
    assert "2 finding(s)" in report.stopped_early
    assert report.summary()["resume"] == report.resume_command


async def test_resuming_a_quota_stopped_run_reaches_the_filing_phase(cfg, tmp_path, fake_agents):
    """The property worth all of this: the findings become tickets on the retry.

    No second resume mechanism -- `_succeeded_agents` already skips what
    finished and `_phase_file` already picks up envelopes carrying no ticket.
    This asserts the two halves meet.
    """
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2}
    behaviour["REPRODUCER"] = {"subtype": "failure", "error": QUOTA_ERROR}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    assert report.quota_exhausted

    calls.clear()
    behaviour["REPRODUCER"] = {}
    resumed = await make_conductor(cfg, tmp_path).run("nightly", run_id=report.run_id)
    names = [n for n, _ in calls]

    assert "MAPPER" not in names, "a resumed run paid to re-map"
    assert "API" not in names, "discovery that succeeded was asked again"
    assert "TRIAGE" in names, "the filing phase never ran; the findings stay on disk"
    assert not resumed.quota_exhausted


async def test_each_ticket_gets_its_own_diff_budget(cfg, tmp_path, fake_agents):
    """§8.2 bounds a diff, not a run.

    The budget lived on the per-run store keyed by agent alone, so every ticket
    in a fix cycle drew from one pool of five files: ticket A's fix touched four,
    ticket B's FIXER got one edit and was refused, and every later ticket
    escalated as "wider than the envelope". `fix-cycle --from-board` exists to
    work ten tickets.
    """
    from qaas.guardrails import Guardrail

    calls, behaviour = fake_agents
    store = RunStore.new(tmp_path)
    _filed(store, key="CORVID-1")
    _filed(store, key="CORVID-2")

    def ticket_of_this_call() -> str:
        return "CORVID-2" if "CORVID-2" in calls[-1][1] else "CORVID-1"

    seen: dict[str, int] = {}

    def verifier(ctx, spec):
        ticket = ticket_of_this_call()
        seen[ticket] = seen.get(ticket, 0) + 1
        verdict = "NOT_FIXED" if seen[ticket] == 1 else "VERIFIED"
        ctx.store.log("verdict", agent="VERIFIER", ticket_key=ticket, verdict=verdict, observed="-")

    allowed: dict[str, list[bool]] = {}

    def fixer(ctx, spec):
        ticket = ticket_of_this_call()
        guard = Guardrail(ctx)
        root = ctx.agent.policy.write_paths[0]
        allowed[ticket] = [
            guard.check("Edit", {"file_path": f"{root}/{ticket.lower()}_{i}.py"}).allowed
            for i in range(4)
        ]

    def reviewer(ctx, spec):
        ctx.store.log("review", agent="REVIEWER", ticket_key=ticket_of_this_call(), decision="APPROVE")

    behaviour["VERIFIER"] = {"hook": verifier}
    behaviour["FIXER"] = {"hook": fixer}
    behaviour["REVIEWER"] = {"hook": reviewer}

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert allowed == {"CORVID-1": [True] * 4, "CORVID-2": [True] * 4}, allowed
    assert not report.escalations, report.escalations


def test_a_resumed_run_keeps_the_base_it_started_on(tmp_path):
    """The base is the first `run_started`, not the branch a resume finds checked out."""
    store = RunStore.new(tmp_path)
    store.log("run_started", mode="fix-cycle", target_branch="develop", target_sha="a" * 40)
    store.log("run_started", mode="fix-cycle", target_branch="fix/CORVID-1", target_sha="b" * 40)
    assert Router._run_base(store) == "develop"


def test_a_detached_start_uses_the_sha(tmp_path):
    store = RunStore.new(tmp_path)
    store.log("run_started", mode="nightly", target_branch="HEAD", target_sha="c" * 40)
    assert Router._run_base(store) == "c" * 40


# -- every run ends, and the reserve is really held back ----------------------


def _kinds(store) -> list[str]:
    return [e.kind for e in store.ledger()]


async def test_an_exception_during_discovery_still_closes_the_run(cfg, tmp_path, fake_agents, monkeypatch):
    """The finding phases caught only Budget/Quota, so the case the second half's
    comment describes -- a task builder raising -- escaped from the first half and
    left `run_started` with no `run_finished`."""
    def boom(*args, **kwargs):
        raise ValueError("no target profile loaded")

    monkeypatch.setattr(conductor_mod.tasks, "mapper", boom)
    report = await make_conductor(cfg, tmp_path).run("pr-check")
    store = RunStore(report.run_id, tmp_path)
    assert _kinds(store)[-1] == "run_finished"
    assert "ValueError" in (report.stopped_early or "")


async def test_an_interrupt_closes_the_run_and_still_interrupts(cfg, tmp_path, fake_agents):
    """Ctrl-C arrives as CancelledError, a BaseException neither clause caught.
    The dashboard then showed the run live forever."""
    import asyncio

    calls, behaviour = fake_agents

    def cancel(ctx, spec):
        raise asyncio.CancelledError()

    behaviour["MAPPER"] = {"hook": cancel}
    router = make_conductor(cfg, tmp_path)
    with pytest.raises(asyncio.CancelledError):
        await router.run("pr-check")
    run_id = next((tmp_path / "runs").iterdir()).name
    finished = list(RunStore(run_id, tmp_path).ledger("run_finished"))
    assert finished and finished[-1].detail.get("interrupted") is True
    assert "--run-id" in (finished[-1].detail.get("resume") or "")


async def test_a_finding_phase_agent_is_bounded_by_the_reserved_clock(cfg, tmp_path, fake_agents, monkeypatch):
    """Bounded by the full cap, an agent in flight when the reserve began ran on
    through it, and TRIAGE then found the clock gone."""
    import asyncio

    seen: dict[str, float] = {}
    real_wait_for = asyncio.wait_for

    async def spy(awaitable, timeout):
        seen.setdefault("first", timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(conductor_mod.asyncio, "wait_for", spy)
    mode = cfg.run_modes["pr-check"]
    await make_conductor(cfg, tmp_path).run("pr-check")
    reserved = mode.max_wall_clock_s * (1 - mode.reserve_fraction)
    assert seen["first"] <= reserved + 1, seen


async def test_reproducing_inside_verify_uses_the_full_clock(cfg, tmp_path, fake_agents):
    """`_reproduce_for` ran under `_gather`'s reserved default, so past 85% of a
    run the first ticket needing a test abandoned verify and report."""
    from qaas.router import Budget

    calls, behaviour = fake_agents
    store = RunStore.new(tmp_path)
    env = _filed(store)
    fresh = env.model_copy(update={"reproduction": env.reproduction.model_copy(
        update={"status": ReproStatus.UNATTEMPTED})})
    store.put_envelope(fresh)
    budget = Budget(None, 1000, reserve_fraction=0.15)
    budget.started -= 900  # inside the reserve, well before the cap
    router = make_conductor(cfg, tmp_path)
    specs = {s.name: s for s in cfg.enabled_agents("full-loop")}
    from qaas.router import RunReport
    await router._reproduce_for(fresh, specs, store, budget, RunReport(store.run_id, "full-loop"), None)
    assert [n for n, _ in calls] == ["REPRODUCER"]


def test_the_reserved_clock_is_what_is_left_before_the_reserve():
    budget = Budget(None, 1000, reserve_fraction=0.2)
    assert 790 < budget.seconds_left(reserve=True) <= 800
    assert 990 < budget.seconds_left() <= 1000


async def test_a_verified_ticket_resolves_this_targets_memory(cfg, tmp_path, verdicts, monkeypatch):
    """Memory is keyed (target, fingerprint); an unscoped resolve marked only the
    pre-target partition, so a regression could never be reported for a named target."""
    calls, behaviour, script = verdicts
    script("VERIFIED")
    seen: list[dict] = []
    monkeypatch.setattr(
        conductor_mod.defect_memory, "resolve",
        lambda *args, **kwargs: seen.append(kwargs),
    )
    store = RunStore.new(tmp_path)
    _filed(store)
    await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert seen and seen[0].get("target") == str(cfg.target or "")


@pytest.fixture
def fixed_target(tmp_path):
    import subprocess

    root = tmp_path / "target"
    root.mkdir()
    for args in (["init", "-q", "-b", "master"], ["commit", "-q", "--allow-empty", "-m", "init"],
                 ["branch", "fix/QAAS-54-dup"]):
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)
    return root


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([("review", "APPROVE")], "fix/QAAS-54-dup"),
        ([("review", "ESCALATE_TO_HUMAN"), ("human_decision", "proceed")], "fix/QAAS-54-dup"),
        ([("review", "REQUEST_CHANGES")], None),
        ([("human_decision", "proceed"), ("review", "REQUEST_CHANGES")], None),
        ([("review", "APPROVE"), ("human_decision", "hold")], None),
    ],
)
def test_a_resumed_cycle_verifies_an_approved_fix_not_the_repro_branch(cfg, tmp_path, fixed_target, events, expected):
    """Starting on the reproduction branch -- which carries no fix by
    construction -- meant an approved fix could never simply be re-verified."""
    store = RunStore.new(tmp_path / "state")
    store.log("vcs", agent="FIXER", action="commit", branch="fix/QAAS-54-dup", scope="QAAS-54")
    for kind, decision in events:
        store.log(kind, agent="REVIEWER", ticket_key="QAAS-54", decision=decision)
    router = Router(cfg, target_root=fixed_target, root=tmp_path / "state")
    assert router._prior_fix_branch(store, "QAAS-54") == expected
