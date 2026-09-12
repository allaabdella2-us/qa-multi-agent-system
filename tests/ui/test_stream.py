"""The live path: a tail thread feeding connected clients.

`trace.tail` is synchronous and sleeps between polls (trace.py:128). These tests
drive the bridge directly rather than through an SSE response, with the poll
interval turned down so a test waits milliseconds instead of half a second per
assertion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from qaas.store import AgentResult, RunStore
from qaas.ui.server import Dashboard, build_app

from .conftest import SPECS, build_run

FAST = 0.01


async def next_event(client: asyncio.Queue, event: str, timeout: float = 5.0) -> dict:
    """The next message of one kind, ignoring the rest."""
    async with asyncio.timeout(timeout):
        while True:
            message = await client.get()
            if message["event"] == event:
                return message["data"]


async def test_a_live_run_streams_its_entries(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-live", finish=False)
    dash = Dashboard(tmp_path, specs=SPECS, poll=FAST)
    watcher = dash.watcher("run-live")
    client = watcher.attach()

    # The view already holds everything written before the tail attached; the
    # tail starts at EOF so the two halves meet exactly once.
    assert watcher.view.agents["API"].findings == 1

    store.log("agent_started", agent="REPRODUCER", model="claude-opus-5")
    data = await next_event(client, "ledger")
    assert data["kind"] == "agent_started"
    assert data["agent"] == "REPRODUCER"
    assert watcher.view.agents["REPRODUCER"].status == "running"


async def test_a_denial_reaches_a_client_with_its_reason(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-deny", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-deny")
    client = watcher.attach()
    store.log(
        "denial",
        agent="FIXER",
        tool="Bash",
        reason="git push -f is refused for every agent, always",
        via="hook",
    )
    data = await next_event(client, "ledger")
    assert data["kind"] == "denial"
    assert "refused for every agent" in data["text"]


async def test_tool_calls_do_not_reach_the_stream_but_do_move_the_card(
    tmp_path: Path,
) -> None:
    # A real run logs ~2400 tool_call lines. They belong in the agent card's
    # counter, not in a feed a person is reading.
    store = build_run(tmp_path, "run-tools", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-tools")
    client = watcher.attach()
    before = watcher.view.agents["API"].tool_calls
    store.log("tool_call", agent="API", tool="Grep", tool_use_id="z", allowed=True)
    patch = await next_event(client, "patch")
    assert patch["agents"]["API"]["tool_calls"] == before + 1
    assert patch["agents"]["API"]["last_tool"] == "Grep"
    assert all(m["event"] != "ledger" for m in list(client._queue))


async def test_a_new_finding_is_its_own_event(tmp_path: Path) -> None:
    from .conftest import make_envelope

    store = build_run(tmp_path, "run-find", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-find")
    client = watcher.attach()
    store.put_envelope(make_envelope("run-find", title="Second finding"))
    data = await next_event(client, "finding")
    assert data["title"] == "Second finding"
    assert data["fileable"] is True


async def test_the_run_ending_closes_the_stream(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-end", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-end")
    client = watcher.attach()
    store.log("run_finished", run_id="run-end", mode="pr-check", cost_usd=2.0)
    assert (await next_event(client, "done"))["completed"] is True
    assert watcher.done is True
    assert watcher.view.completed is True


async def test_two_clients_share_one_watcher_and_one_tail(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-shared", finish=False)
    dash = Dashboard(tmp_path, specs=SPECS, poll=FAST)
    first, second = dash.watcher("run-shared"), dash.watcher("run-shared")
    assert first is second, "a second browser tab must not start a second tail"

    a, b = first.attach(), first.attach()
    store.log("agent_started", agent="TRIAGE", model="claude-sonnet-5")
    assert (await next_event(a, "ledger"))["agent"] == "TRIAGE"
    assert (await next_event(b, "ledger"))["agent"] == "TRIAGE"

    first.detach(a)
    assert a not in first.clients and b in first.clients


async def test_a_finished_run_starts_no_tail_thread(tmp_path: Path) -> None:
    # `tail(from_start=False)` seeks past a finished run's `run_finished`, so a
    # thread started here would poll a file that never changes again, forever.
    build_run(tmp_path, "run-over", finish=True)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-over")
    assert watcher.done is True
    assert watcher._thread is None


async def test_the_phase_rail_advances_while_an_agent_is_running(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-phase", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-phase")
    client = watcher.attach()
    store.log("agent_started", agent="REPRODUCER", model="claude-opus-5")
    patch = await next_event(client, "patch")
    # A running agent's phase outranks every finished one, so the rail follows
    # the work rather than the furthest point reached.
    assert patch["phase"] == "reproduce"
    assert patch["phase_status"]["reproduce"] == "active"


async def test_cost_rides_along_on_the_patch(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-cost", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-cost")
    client = watcher.attach()
    before = watcher.view.cost_usd
    store.log("agent_started", agent="REPRODUCER", model="claude-opus-5")
    # `put_result` writes the result file and logs `agent_finished` together,
    # which is why the header's cost only moves when an agent finishes.
    store.put_result(AgentResult(agent="REPRODUCER", cost_usd=1.5, num_turns=9))
    patch = None
    async with asyncio.timeout(5):
        while patch is None or patch["cost_usd"] <= before:
            message = await client.get()
            if message["event"] == "patch":
                patch = message["data"]
    assert patch["cost_usd"] == pytest.approx(before + 1.5)
    assert patch["agents"]["REPRODUCER"]["status"] == "done"


def test_the_sse_route_opens_with_a_snapshot(tmp_path: Path) -> None:
    build_run(tmp_path, "run-sse", finish=True)
    client = TestClient(build_app(Dashboard(tmp_path, specs=SPECS, poll=FAST)))
    with client.stream("GET", "/api/runs/run-sse/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = ""
        for chunk in response.iter_text():
            body += chunk
            if "event: done" in body:
                break
    assert "event: snapshot" in body
    assert '"run_id": "run-sse"' in body
