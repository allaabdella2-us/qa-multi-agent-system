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
from qaas.envelope import DefectEnvelope, Domain, Severity
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
    await tools["emit_envelope"]({
        "domain": "api", "class": "bug",
        "title": "Something is broken",
        "summary": "And I have already reproduced it, honestly.",
        "severity": "major", "confidence": 0.95,
        "evidence": [{"type": "log", "uri": "artifact://a/b"}],
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
    behaviour["API"] = {"emit": 1}

    await make_conductor(cfg, tmp_path).run("full-loop")
    dispatched = {name for name, _ in calls}

    # Remediation agents only run on a NOT_FIXED verdict, which this run has no
    # way to produce; they are covered by the fix-loop tests instead.
    expected = {s.name for s in cfg.enabled_agents("full-loop")} - {"FIXER", "REVIEWER", "VERIFIER"}
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
