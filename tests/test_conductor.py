"""M3/M6 verification: the run state machine, without spending a cent.

`run_agent` is replaced by a fake, so these tests exercise phase ordering, the
budget governor and the loop breakers — the parts that must work correctly
precisely when a real run is going wrong.
"""

from pathlib import Path

import pytest

from qaas import conductor as conductor_mod
from support import CONFIG_SEARCH

from qaas.conductor import Budget, BudgetExceeded, Conductor
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
                        severity=Severity.MAJOR,
                        confidence=0.9,
                        evidence=[{"type": "log", "uri": "artifact://a/b"}],
                    )
                )
        if cfg.get("publish_map"):
            ctx.maps.put({"services": ["orders-api"], "routes": []})
        if cfg.get("review"):
            # ARBITER's decision reaches the conductor through the ledger, the
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


def make_conductor(cfg, tmp_path) -> Conductor:
    return Conductor(cfg, target_root=REPO, root=tmp_path)


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
    forge = cfg.agents["FORGE"]          # its own cap is $4
    assert b.allowance(forge) == pytest.approx(1.0), "the run's remainder wins"
    b.spend(0.7)
    assert b.allowance(forge) == pytest.approx(0.3)


def test_allowance_never_goes_to_zero_or_negative(cfg):
    b = Budget(max_usd=1.0, max_seconds=3600)
    b.spend(5.0)
    assert b.allowance(cfg.agents["CONDUIT"]) > 0


# -- phase ordering ---------------------------------------------------------


async def test_phases_run_in_dependency_order(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}
    behaviour["CONDUIT"] = {"emit": 2}
    behaviour["SURFACE"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    order = [name for name, _ in calls]

    assert order[0] == "CARTOGRAPHER", "the map must exist before anything reads it"
    assert set(order[1:3]) == {"CONDUIT", "SURFACE"}
    assert order.count("FORGE") == 3, "one FORGE invocation per finding"
    assert order[-1] == "CLERK", "filing comes last"
    assert report.cost_usd > 0


async def test_forge_is_skipped_when_discovery_found_nothing(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}

    store_root = tmp_path
    await make_conductor(cfg, store_root).run("nightly")
    assert "FORGE" not in [n for n, _ in calls]
    assert "CLERK" not in [n for n, _ in calls]


async def test_incident_mode_discovers_but_never_files(cfg, tmp_path, fake_agents):
    """§9: incident runs are diagnostic. Nothing reaches a ticket."""
    calls, behaviour = fake_agents
    behaviour["CONDUIT"] = {"emit": 2}

    await make_conductor(cfg, tmp_path).run("incident")
    names = [n for n, _ in calls]
    assert "CONDUIT" in names
    assert "CLERK" not in names


async def test_each_forge_invocation_gets_its_own_finding(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}
    behaviour["CONDUIT"] = {"emit": 3}

    await make_conductor(cfg, tmp_path).run("pr-check")
    forge_tasks = [task for name, task in calls if name == "FORGE"]
    assert len(forge_tasks) == 3
    assert len({t for t in forge_tasks}) == 3, "each task names a different finding"


# -- loop breakers ----------------------------------------------------------


async def test_a_spend_blowout_stops_the_run_and_escalates(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True, "cost": 99.0}

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    assert report.stopped_early and "spend cap" in report.stopped_early
    assert "CLERK" not in [n for n, _ in calls]
    assert report.escalations


async def test_more_findings_than_the_cap_escalates_and_triages_the_worst(cfg, tmp_path, fake_agents):
    """§8.3: past K findings, pause and escalate rather than grind through."""
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}
    behaviour["CONDUIT"] = {"emit": 40}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    forge_calls = sum(1 for n, _ in calls if n == "FORGE")
    assert forge_calls == cfg.thresholds.max_findings_per_agent_run
    assert any("exceed the per-run cap" in e for e in report.escalations)


async def test_a_failing_agent_escalates_without_killing_the_run(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}
    behaviour["CONDUIT"] = {"subtype": "failure", "error": "browser timeout"}
    behaviour["SURFACE"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("nightly")
    assert "CONDUIT" in report.failed
    assert "FORGE" in [n for n, _ in calls], "SURFACE's finding is still triaged"
    assert any("CONDUIT failed" in e for e in report.escalations)


async def test_a_missing_map_is_escalated_not_ignored(cfg, tmp_path, fake_agents):
    """Everything downstream trusts the map. Running without one must be loud."""
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {}  # runs, publishes nothing

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    assert any("published no map" in e for e in report.escalations)


# -- the ledger -------------------------------------------------------------


async def test_the_run_is_fully_recorded_in_the_ledger(cfg, tmp_path, fake_agents):
    calls, behaviour = fake_agents
    behaviour["CARTOGRAPHER"] = {"publish_map": True}
    behaviour["CONDUIT"] = {"emit": 1}

    report = await make_conductor(cfg, tmp_path).run("pr-check")
    store = RunStore(report.run_id, tmp_path)
    kinds = [e.kind for e in store.ledger()]

    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert "envelope" in kinds
    assert store.total_cost_usd() == pytest.approx(report.cost_usd)


# -- the remediation loop (§8.3) --------------------------------------------


def _filed(store, spec_name="CONDUIT", key="CORVID-1", branch="fix/x"):
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
    """Script PROOF's successive verdicts; each dispatch pops the next one."""
    calls, behaviour = fake_agents
    queue: list[str] = []

    def script(*sequence: str) -> None:
        queue.extend(sequence)

    def proof_behaviour(ctx, spec):
        if queue:
            ctx.store.log("verdict", agent="PROOF", ticket_key="CORVID-1",
                          verdict=queue.pop(0), observed="...")

    behaviour["PROOF"] = {"hook": proof_behaviour}
    return calls, behaviour, script


async def test_a_verified_ticket_ends_the_loop(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("VERIFIED")
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert [n for n, _ in calls].count("PROOF") == 1
    assert not report.escalations


async def test_not_fixed_without_a_mender_escalates_immediately(cfg, tmp_path, verdicts):
    """A roster with no fixer must not cycle: re-running PROOF on unchanged code
    proves nothing and spends the budget discovering that."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    store = RunStore.new(tmp_path)
    _filed(store)

    # A verify-only roster, which is what every Phase 1 mode still is.
    mode = cfg.run_modes["fix-cycle"].model_copy(update={"agents": ["PROOF"]})
    verify_only = cfg.model_copy(update={"run_modes": {**cfg.run_modes, "fix-cycle": mode}})

    report = await Conductor(verify_only, target_root=REPO, root=tmp_path).run(
        "fix-cycle", run_id=store.run_id
    )
    assert [n for n, _ in calls].count("PROOF") == 1, "must not cycle without a fixer"
    assert any("no MENDER" in e for e in report.escalations)


async def test_not_fixed_with_a_mender_runs_the_fix_loop(cfg, tmp_path, verdicts):
    """The Phase 3 path: NOT_FIXED sends the ticket to MENDER, then ARBITER, then
    back to PROOF — bounded by max_proof_reopens."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["ARBITER"] = {"review": "APPROVE"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    assert order == ["PROOF", "MENDER", "ARBITER", "PROOF"], order
    assert not report.escalations


def _mender_pushes(branch: str):
    """MENDER's hook: log the vcs write a real fix round leaves in the ledger."""
    def hook(ctx, spec):
        ctx.store.log("vcs", agent="MENDER", action="create_branch", branch=branch)
        ctx.store.log("vcs", agent="MENDER", action="push", branch=branch)
    return hook


async def test_proof_reverifies_on_menders_branch_not_the_repro_branch(cfg, tmp_path, verdicts):
    """The bug this pins: the envelope names the *repro* branch, which carries
    the failing test and no fix. Re-sending PROOF there after a remediation
    round made VERIFIED unreachable -- PROOF re-verified the unfixed branch it
    had just failed on, burned a reopen and escalated. A live run recorded
    exactly that."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["ARBITER"] = {"review": "APPROVE"}
    behaviour["MENDER"] = {"hook": _mender_pushes("fix/CORVID-1-limit")}
    store = RunStore.new(tmp_path)
    _filed(store, branch="qa/repro/61297327-orders-limit-ignored")

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    proof_tasks = [task for name, task in calls if name == "PROOF"]
    assert len(proof_tasks) == 2, [n for n, _ in calls]
    assert "qa/repro/61297327-orders-limit-ignored" in proof_tasks[0]
    assert "fix/CORVID-1-limit" in proof_tasks[1]
    assert "qa/repro/61297327-orders-limit-ignored" not in proof_tasks[1].splitlines()[0]
    assert not report.escalations


async def test_a_remediation_round_that_writes_nothing_keeps_the_repro_branch(cfg, tmp_path, verdicts):
    """No vcs write means no fix branch to find. PROOF stays where it was rather
    than being sent to some other ticket's branch picked up from the ledger."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED", "VERIFIED")
    behaviour["ARBITER"] = {"review": "APPROVE"}
    behaviour["MENDER"] = {}
    store = RunStore.new(tmp_path)
    # A stale entry from an earlier ticket in the same run: run-wide scanning
    # would hand PROOF this branch, which has nothing to do with CORVID-1.
    store.log("vcs", agent="MENDER", action="push", branch="fix/CORVID-99-unrelated")
    _filed(store, branch="qa/repro/orders-limit")

    await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    proof_tasks = [task for name, task in calls if name == "PROOF"]
    assert len(proof_tasks) == 2
    assert "qa/repro/orders-limit" in proof_tasks[1]
    assert "CORVID-99" not in proof_tasks[1]


async def test_arbiter_requesting_changes_sends_it_back_to_mender(cfg, tmp_path, verdicts):
    """§8.3: bounded round trips. Two REQUEST_CHANGES exhausts the limit and
    escalates rather than looping until the budget is gone."""
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    behaviour["ARBITER"] = {"review": "REQUEST_CHANGES"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    trips = cfg.thresholds.max_mender_arbiter_round_trips
    assert order.count("MENDER") == trips
    assert order.count("ARBITER") == trips
    assert any("round trips without approval" in e for e in report.escalations)


async def test_arbiter_escalation_stops_the_loop_immediately(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("NOT_FIXED")
    behaviour["ARBITER"] = {"review": "ESCALATE_TO_HUMAN"}
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    order = [n for n, _ in calls]
    assert order.count("MENDER") == 1, "one attempt, then a human takes it"
    assert any("escalated" in e.lower() for e in report.escalations)


async def test_a_regression_blocks_rather_than_retrying(cfg, tmp_path, verdicts):
    calls, behaviour, script = verdicts
    script("REGRESSED")
    store = RunStore.new(tmp_path)
    _filed(store)

    report = await make_conductor(cfg, tmp_path).run("fix-cycle", run_id=store.run_id)
    assert [n for n, _ in calls].count("PROOF") == 1
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
    behaviour["MENDER"] = {}
    store = RunStore.new(tmp_path)
    _filed(store)

    cfg2 = cfg.model_copy(deep=True)
    cfg2.agents["MENDER"] = cfg.agents["FORGE"].model_copy(
        update={"name": "MENDER", "layer": "remediation", "must_call": []}
    )
    cfg2.run_modes["fix-cycle"].agents = ["PROOF", "MENDER"]

    report = await make_conductor(cfg2, tmp_path).run("fix-cycle", run_id=store.run_id)
    names = [n for n, _ in calls]
    assert names.count("PROOF") == cfg.thresholds.max_proof_reopens + 1
    assert names.count("MENDER") == cfg.thresholds.max_proof_reopens
    assert any("the limit" in e for e in report.escalations)


async def test_a_discovery_agent_cannot_certify_its_own_finding(cfg, tmp_path):
    """§2: the finder never grades its own homework.

    A discovery agent claiming `reproduced` in its own envelope would skip FORGE
    entirely — which is not hypothetical: it happened on the first full pipeline
    run and the triage phase was silently skipped for every finding.
    """
    from qaas.mcp import envelope_server
    from qaas.mcp.context import ToolContext, handlers
    from qaas.store import RunStore, SystemMapStore

    store = RunStore.new(root=tmp_path)
    ctx = ToolContext(
        store=store, maps=SystemMapStore(tmp_path), config=cfg,
        agent=cfg.agents["CONDUIT"], target_root=REPO,
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
            "verified_by": "CONDUIT",
        },
    })

    envelope = store.envelopes()[0]
    assert envelope.reproduction.status.value == "unattempted"
    assert envelope.reproduction.failing_test is None
    assert envelope.reproduction.verified_by is None
    assert envelope.reproduction.steps == ["call the endpoint"], "the steps are kept for FORGE"


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
        run_id=store.run_id, discovered_by="SURFACE", domain=Domain.FRONTEND,
        **{"class": "bug"}, title="Neighbour's finding", summary="Emitted by SURFACE.",
        severity=Severity.MINOR, confidence=0.8,
        evidence=[{"type": "log", "uri": "artifact://a/b"}],
    ))

    async def fake_query(**kwargs):
        store.put_envelope(DefectEnvelope(
            run_id=store.run_id, discovered_by="CONDUIT", domain=Domain.API,
            **{"class": "bug"}, title="Mine", summary="Emitted by CONDUIT.",
            severity=Severity.MAJOR, confidence=0.9,
            evidence=[{"type": "log", "uri": "artifact://a/b"}],
        ))
        return
        yield  # pragma: no cover

    import qaas.runner as runner_mod
    original = runner_mod.query
    runner_mod.query = fake_query
    try:
        outcome = await run_agent(cfg.agents["CONDUIT"], ctx_for("CONDUIT"), "task")
    finally:
        runner_mod.query = original

    assert len(outcome.result.envelope_ids) == 1, "only its own finding"
    assert store.get_envelope(outcome.result.envelope_ids[0]).discovered_by == "CONDUIT"


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
    mode = cfg.run_modes["pr-check"]
    store.put_result(AgentResult(agent="CONDUIT", subtype="success",
                                 cost_usd=mode.max_budget_usd - 0.05, num_turns=1))
    assert store.total_cost_usd() == pytest.approx(mode.max_budget_usd - 0.05)

    behaviour["CARTOGRAPHER"] = {"cost": 1.0, "publish_map": True}
    report = await make_conductor(cfg, tmp_path).run("pr-check", run_id=store.run_id)

    assert report.stopped_early, "the resumed run ignored what the run had already spent"
    assert "spend cap" in report.stopped_early
