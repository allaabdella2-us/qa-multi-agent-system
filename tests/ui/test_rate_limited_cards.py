"""What a card says about an agent the provider stopped, and one a process left.

From the first run of the whole roster: REPRODUCER hit the account's session
limit on its third finding and was drawn as a red "failed" box with the raw
`ResultError` under it -- although the resume then ran it to completion. And a
SYNTHESIZER killed mid-dispatch stayed "running" beside a finished run.
"""

from __future__ import annotations

from support import CONFIG_SEARCH

from qaas.config import load_config
from qaas.store import RunStore
from qaas.ui import state

LIMIT = "ResultError: Claude Code returned an error result: You've hit your session limit · resets 3:50pm"


def _view(tmp_path, lines):
    store = RunStore.new(tmp_path)
    for kind, agent, detail in lines:
        store.log(kind, agent=agent, **detail)
    return state.load(store, load_config(search=CONFIG_SEARCH).agents)


def test_a_provider_limit_is_not_a_failure(tmp_path):
    view = _view(tmp_path, [
        ("run_started", None, {"mode": "full-loop", "agents": ["REPRODUCER"]}),
        ("agent_started", "REPRODUCER", {}),
        ("agent_error", "REPRODUCER", {"error": LIMIT}),
        ("agent_finished", "REPRODUCER", {"error": LIMIT, "cost_usd": 0.15}),
        ("quota_exhausted", None, {"reason": f"REPRODUCER: {LIMIT}", "resume": "qaas run --run-id r"}),
        ("run_finished", None, {}),
    ])
    card = view.to_json()["agents"]["REPRODUCER"]
    assert card["status"] == "rate_limited"
    assert card["error"] is None, "the raw provider error was drawn in red"
    assert "qaas run --run-id r" in card["reason"]


def test_a_later_success_clears_the_earlier_error(tmp_path):
    view = _view(tmp_path, [
        ("run_started", None, {"mode": "full-loop", "agents": ["REPRODUCER"]}),
        ("agent_started", "REPRODUCER", {}),
        ("agent_finished", "REPRODUCER", {"error": "OSError: disk full"}),
        ("agent_started", "REPRODUCER", {}),
        ("agent_finished", "REPRODUCER", {"cost_usd": 2.1}),
        ("run_finished", None, {}),
    ])
    card = view.to_json()["agents"]["REPRODUCER"]
    assert card["status"] == "done" and card["error"] is None


def test_waiting_for_a_reset_says_when(tmp_path):
    view = _view(tmp_path, [
        ("run_started", None, {"mode": "full-loop", "agents": ["REPRODUCER"]}),
        ("agent_started", "REPRODUCER", {}),
        ("agent_finished", "REPRODUCER", {"error": LIMIT}),
        ("quota_wait", "REPRODUCER", {"until": "2026-09-24T19:51:00+00:00", "seconds": 3000}),
    ])
    body = view.to_json()
    card = body["agents"]["REPRODUCER"]
    assert card["status"] == "waiting" and card["waiting_until"].startswith("2026-09-24T19:51")
    assert body["phase_status"]["reproduce"] == "active", "a wait is still work in progress"


def test_an_agent_a_dead_process_left_running_settles_on_resume(tmp_path):
    view = _view(tmp_path, [
        ("run_started", None, {"mode": "full-loop", "agents": ["SYNTHESIZER"]}),
        ("agent_started", "SYNTHESIZER", {}),
        ("agent_finished", "SYNTHESIZER", {"cost_usd": 4.18}),
        ("run_finished", None, {}),
        ("run_started", None, {"mode": "full-loop", "agents": ["SYNTHESIZER"]}),
        ("agent_started", "SYNTHESIZER", {}),  # killed here; no finish
        ("run_started", None, {"mode": "full-loop", "agents": ["SYNTHESIZER"]}),
        ("run_finished", None, {}),
    ])
    assert view.to_json()["agents"]["SYNTHESIZER"]["status"] == "done"


def test_one_killed_before_it_ever_finished_reads_as_interrupted(tmp_path):
    view = _view(tmp_path, [
        ("run_started", None, {"mode": "full-loop", "agents": ["API"]}),
        ("agent_started", "API", {}),
        ("run_finished", None, {"interrupted": True}),
    ])
    assert view.to_json()["agents"]["API"]["status"] == "interrupted"


def test_the_page_has_a_band_for_every_shipped_layer():
    """SYNTHESIZER's layer was missing from the page's list and it was drawn
    under "not in this roster"."""
    from pathlib import Path

    import qaas.ui

    script = (Path(qaas.ui.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    for spec in load_config(search=CONFIG_SEARCH).agents.values():
        assert f'["{spec.layer}",' in script, f"{spec.name}'s layer {spec.layer!r} has no band"
