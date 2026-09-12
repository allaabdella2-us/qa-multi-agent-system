"""The read model: one ledger folded into one view.

The load-bearing property is that `apply()` is the *only* way state changes, so
replaying a file and following a live run cannot produce different answers.
`test_incremental_matches_one_shot` is that property, asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qaas.store import AgentResult, RunStore
from qaas.ui import state

from .conftest import SPECS, build_run, make_envelope


def load(root: Path, run_id: str = "run-20260101T000000-aaaaaa") -> state.RunView:
    return state.load(RunStore(run_id, root=root, create=False), SPECS)


# -- header ---------------------------------------------------------------


def test_header_comes_from_run_started(run_root: Path) -> None:
    view = load(run_root)
    assert view.mode == "pr-check"
    assert view.wall_clock_s == 900
    assert view.budget_usd is None
    assert view.target_sha == "bf40c35766cc"
    assert view.target_dirty is True
    assert view.target_branch == "main"
    assert view.completed is True


def test_target_name_is_the_runs_own_target_not_todays_config(run_root: Path) -> None:
    # `run_started` records a path, never the profile name. Naming the target
    # from today's `cfg.target` would relabel every historical run the day
    # someone repoints system.yaml.
    assert load(run_root).target_name == "corvid"


def test_elapsed_runs_to_now_while_a_run_is_live(tmp_path: Path) -> None:
    build_run(tmp_path, "run-live", finish=False)
    view = load(tmp_path, "run-live")
    assert view.completed is False
    # RunSummary.duration_s is None here (no `finished`); the header still needs
    # a clock, so elapsed measures against now.
    assert view.elapsed_s > 0


# -- agents ---------------------------------------------------------------


def test_agent_states(run_root: Path) -> None:
    agents = load(run_root).agents
    assert agents["MAPPER"].status == "done"
    assert agents["API"].status == "done"
    assert agents["BROWSER"].status == "skipped"
    assert agents["BROWSER"].reason == "target has no live_ui"


def test_agent_counters(run_root: Path) -> None:
    api = load(run_root).agents["API"]
    assert api.tool_calls == 2
    assert api.denials == 1
    assert api.findings == 1
    assert api.turns == 21
    assert api.last_tool == "Bash"
    assert api.model == "claude-opus-5"


def test_cost_accumulates_across_repeated_invocations(tmp_path: Path) -> None:
    # REPRODUCER runs once per finding and FIXER once per review round trip, so
    # a view keyed by agent name must add dispatches rather than replace them.
    store = build_run(tmp_path, "run-multi", finish=False)
    for _ in range(3):
        store.log("agent_started", agent="REPRODUCER", model="claude-opus-5")
        store.put_result(AgentResult(agent="REPRODUCER", cost_usd=1.0, num_turns=5))
    repro = load(tmp_path, "run-multi").agents["REPRODUCER"]
    assert repro.invocations == 3
    assert repro.cost_usd == pytest.approx(3.0)
    assert repro.turns == 15


def test_an_agent_the_config_has_never_heard_of_is_rendered_not_dropped(
    tmp_path: Path,
) -> None:
    # Every ledger written before the roster was renamed names agents like
    # CARTOGRAPHER and FORGE. Dropping or raising on them would shut the
    # dashboard out of the runs most worth looking at.
    store = RunStore("run-retired", root=tmp_path, create=True)
    store.log("run_started", mode="nightly", agents=["FORGE"], wall_clock_s=900)
    store.log("agent_started", agent="FORGE", model="claude-opus-5")
    store.log("tool_call", agent="FORGE", tool="Read", tool_use_id="x", allowed=True)
    view = load(tmp_path, "run-retired")
    assert view.agents["FORGE"].layer is None
    assert view.agents["FORGE"].status == "running"
    assert view.agents["FORGE"].phase is None


# -- phases ---------------------------------------------------------------


def test_phase_derives_from_layer(run_root: Path) -> None:
    view = load(run_root)
    assert view.agents["MAPPER"].phase == "map"
    assert view.agents["API"].phase == "discover"


def test_the_triage_layer_splits_by_name(run_root: Path) -> None:
    # `triage` is the one layer holding two phases. The router dispatches both
    # by name too (router.py:344,372), so splitting them by name is the same
    # rule, not a special case.
    assert state.phase_of("REPRODUCER", "triage") == "reproduce"
    assert state.phase_of("TRIAGE", "triage") == "file"


def test_a_phase_no_agent_fills_is_absent_not_pending(run_root: Path) -> None:
    # pr-check carries no VERIFIER and no REPORTER. Showing those phases as
    # forever-pending reads as a stalled run.
    status = load(run_root).phase_status
    assert status["verify"] == "absent"
    assert status["report"] == "absent"
    assert status["discover"] == "done"


def test_phase_is_unknown_when_no_agent_can_be_placed(tmp_path: Path) -> None:
    store = RunStore("run-retired2", root=tmp_path, create=True)
    store.log("run_started", mode="nightly", agents=["FORGE"], wall_clock_s=900)
    store.log("agent_started", agent="FORGE", model="claude-opus-5")
    assert load(tmp_path, "run-retired2").phase == "unknown"


def test_an_active_phase_wins_over_a_finished_one(tmp_path: Path) -> None:
    store = RunStore("run-phases", root=tmp_path, create=True)
    store.log("run_started", mode="pr-check", agents=["MAPPER", "API"], wall_clock_s=900)
    store.log("agent_started", agent="MAPPER", model="m")
    store.put_result(AgentResult(agent="MAPPER", cost_usd=0.1))
    store.log("agent_started", agent="API", model="m")
    view = load(tmp_path, "run-phases")
    assert view.phase_status["map"] == "done"
    assert view.phase == "discover"


# -- findings, denials, tickets -------------------------------------------


def test_findings_carry_the_fileable_verdict_and_its_reason(run_root: Path) -> None:
    finding = load(run_root).findings[0]
    assert finding.severity == "blocker"
    assert finding.fileable is True
    assert finding.held_reason == ""
    assert finding.evidence_uris and finding.evidence_uris[0].startswith("artifact://")


def test_a_held_finding_reports_the_gate_it_failed(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-held", finish=False)
    store.put_envelope(make_envelope("run-held", evidence=False, title="No evidence"))
    held = [f for f in load(tmp_path, "run-held").findings if f.title == "No evidence"]
    assert held and held[0].fileable is False
    assert "no evidence" in held[0].held_reason


def test_findings_sort_most_severe_first(tmp_path: Path) -> None:
    from qaas.envelope import Severity

    store = build_run(tmp_path, "run-sorted", finish=False)
    store.put_envelope(
        make_envelope("run-sorted", severity=Severity.TRIVIAL, title="Trivial one")
    )
    titles = [f["title"] for f in load(tmp_path, "run-sorted").to_json()["findings"]]
    assert titles.index("Cross-tenant order read") < titles.index("Trivial one")


def test_denials_keep_the_reason_and_the_arguments(run_root: Path) -> None:
    denial = load(run_root).denials[0]
    assert denial.agent == "API"
    assert denial.tool == "Bash"
    assert "not in API's tool allowlist" in denial.reason
    assert denial.via == "hook"
    assert denial.args["command"] == "ls -la"


def test_tickets_track_creation_and_verdict(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-ticket", finish=False)
    store.log("verdict", agent="VERIFIER", ticket_key="QAAS-1", verdict="NOT_FIXED")
    store.log("reopened", ticket_key="QAAS-1", attempt=1)
    store.log("verified", ticket_key="QAAS-1", reopens=1)
    ticket = load(tmp_path, "run-ticket").tickets["QAAS-1"]
    assert ticket.project == "QAAS"
    assert ticket.severity == "blocker"
    assert ticket.verdict == "VERIFIED"
    assert ticket.reopens == 1


# -- the property that makes one code path safe ---------------------------


def test_incremental_matches_one_shot(run_root: Path) -> None:
    """Following a live run must land where replaying the file lands.

    This is the whole reason `apply()` exists rather than a batch builder: the
    live path and the historical path are the same statements in the same order.
    """
    store = RunStore("run-20260101T000000-aaaaaa", root=run_root, create=False)
    entries = state.read_ledger(store)

    one_shot = state.load(store, SPECS, entries=entries)
    incremental = state.RunView(run_id=store.run_id, store=store, specs=SPECS)
    for entry in entries:
        incremental.apply(entry)

    assert incremental.to_json() == one_shot.to_json()


# -- reading a file that is still being written ---------------------------


def test_a_half_written_last_line_is_skipped_not_fatal(run_root: Path) -> None:
    # A live ledger can be mid-`write` when the dashboard reads it. `store.ledger()`
    # validates strictly and raises; this reader applies `trace.tail`'s tolerance
    # (trace.py:116-122) so a partial line costs one poll, not the view.
    store = RunStore("run-20260101T000000-aaaaaa", root=run_root, create=False)
    whole = len(state.read_ledger(store))
    with store.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write('{"at":"2026-01-01T00:00:00Z","kind":"tool_ca')
    assert len(state.read_ledger(store)) == whole
    assert state.load(store, SPECS).completed is True


def test_a_kind_this_reader_does_not_know_is_skipped(run_root: Path) -> None:
    store = RunStore("run-20260101T000000-aaaaaa", root=run_root, create=False)
    whole = len(state.read_ledger(store))
    with store.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write('{"at":"2026-01-01T00:00:00Z","kind":"from_the_future","detail":{}}\n')
    assert len(state.read_ledger(store)) == whole


# -- run discovery --------------------------------------------------------


def test_is_live_is_the_absence_of_run_finished(tmp_path: Path) -> None:
    build_run(tmp_path, "run-done", finish=True)
    build_run(tmp_path, "run-open", finish=False)
    assert state.is_live(RunStore("run-open", root=tmp_path, create=False)) is True
    assert state.is_live(RunStore("run-done", root=tmp_path, create=False)) is False


def test_pick_run_prefers_a_live_run_over_a_newer_finished_one(tmp_path: Path) -> None:
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=False)
    build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=True)
    assert state.pick_run(tmp_path) == "run-20260101T000000-aaaaaa"
    assert state.pick_run(tmp_path, "run-20260102T000000-bbbbbb") == (
        "run-20260102T000000-bbbbbb"
    )


def test_pick_run_falls_back_to_newest_when_nothing_is_live(tmp_path: Path) -> None:
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=True)
    build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=True)
    assert state.pick_run(tmp_path) == "run-20260102T000000-bbbbbb"


def test_pick_run_on_an_empty_root_is_none(tmp_path: Path) -> None:
    assert state.pick_run(tmp_path) is None


def test_run_rail_reports_the_cost_qaas_runs_omits(run_root: Path) -> None:
    row = state.list_runs_summary(run_root)[0]
    assert row["run_id"] == "run-20260101T000000-aaaaaa"
    assert row["mode"] == "pr-check"
    assert row["findings"] == 1
    assert row["cost_usd"] == pytest.approx(2.0)
    assert row["live"] is False


def test_a_resumed_run_is_read_under_the_mode_it_resumed_as(tmp_path: Path) -> None:
    """`qaas run --run-id` appends a second `run_started` to the same ledger.

    Both the header and the run rail must read the same one, and it must be the
    later one: the router restarts the wall clock across a resume rather than
    carrying it over (router.py:120-124), so the run is being held to the second
    cap, not the first.
    """
    store = build_run(tmp_path, "run-resumed", finish=False)
    store.log(
        "run_started",
        mode="nightly",
        agents=["MAPPER", "API", "REPORTER"],
        wall_clock_s=7200,
        target_root="/tmp/corvid",
    )
    view = load(tmp_path, "run-resumed")
    assert view.mode == "nightly"
    assert view.wall_clock_s == 7200
    assert state.list_runs_summary(tmp_path)[0]["mode"] == view.mode


def test_an_agent_the_run_never_reached_does_not_stay_queued(tmp_path: Path) -> None:
    # A roster names REPORTER; the run ends without dispatching it. "queued" on
    # a finished run is a card waiting for something that already happened.
    store = RunStore("run-unreached", root=tmp_path, create=True)
    store.log("run_started", mode="nightly", agents=["API", "REPORTER"], wall_clock_s=900)
    store.log("agent_started", agent="API", model="m")
    store.put_result(AgentResult(agent="API", cost_usd=0.4))
    store.log("run_finished", run_id="run-unreached", stopped_early="wall-clock cap")
    view = load(tmp_path, "run-unreached")
    assert view.agents["REPORTER"].status == "never_ran"
    assert view.agents["API"].status == "done"
    assert view.stopped_early == "wall-clock cap"
