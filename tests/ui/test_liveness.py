"""Which runs are live, and the live view's handoff from replay to tail.

A killed run never writes `run_finished`, so "started more often than finished"
called it live forever. These pin the file-age fallback, the replay's byte
offset, the run rail's raw scan and the SSE snapshot handoff.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from qaas import trace
from qaas.store import LedgerEntry, RunStore
from qaas.ui import state
from qaas.ui.server import Dashboard, RunWatcher, build_app

from .conftest import SPECS, build_run, local_client
from .test_stream import EVENT_TIMEOUT, FAST, next_event


def age(store: RunStore, seconds: float) -> None:
    """Make the ledger look as if nothing has written it for `seconds`."""
    then = time.time() - seconds
    os.utime(store.ledger_path, (then, then))


def fresh(root: Path, run_id: str) -> RunStore:
    return RunStore(run_id, root=root, create=False)


# -- is_live ----------------------------------------------------------------


def test_a_killed_run_stops_being_live_once_its_ledger_outlives_its_cap(tmp_path: Path) -> None:
    """build_run records `wall_clock_s=900`. No run can write for longer than
    that, so silence past it plus the margin means nothing is writing."""
    store = build_run(tmp_path, "run-killed", finish=False)
    assert state.is_live(fresh(tmp_path, "run-killed")) is True

    age(store, 900 + trace.STALE_MARGIN_S - 60)
    assert state.is_live(fresh(tmp_path, "run-killed")) is True, "still inside the cap"

    age(store, 900 + trace.STALE_MARGIN_S + 60)
    assert state.is_live(fresh(tmp_path, "run-killed")) is False


def test_a_run_that_recorded_no_cap_is_given_the_longest_shipped_one(tmp_path: Path) -> None:
    store = RunStore("run-old", root=tmp_path, create=True)
    store.log("run_started", mode="nightly", agents=["API"])      # no wall_clock_s
    store.log("agent_started", agent="API", model="m")

    age(store, 3 * 3600)
    assert state.is_live(fresh(tmp_path, "run-old")) is True
    age(store, trace.STALE_WITHOUT_CAP_S + 60)
    assert state.is_live(fresh(tmp_path, "run-old")) is False


def test_a_resume_of_a_stale_run_is_live_again(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-back", finish=False)
    age(store, 86400)
    assert state.is_live(fresh(tmp_path, "run-back")) is False
    store.log("run_started", mode="full-loop", agents=["REPRODUCER"], wall_clock_s=1800)
    assert state.is_live(fresh(tmp_path, "run-back")) is True


def test_a_nested_run_finished_does_not_count_as_the_run_ending(tmp_path: Path) -> None:
    """The scan is over raw bytes, so it must only count *top-level* kinds."""
    store = build_run(tmp_path, "run-nested", finish=False)
    store.log("tool_call", agent="API", tool="x", args={"a": "b", "kind": "run_finished"})
    store.log("tool_call", agent="API", tool="x", args={"kind": "run_finished"})
    assert state.is_live(fresh(tmp_path, "run-nested")) is True


def test_pick_run_prefers_a_genuinely_live_run_over_a_stale_one(tmp_path: Path) -> None:
    """The newer ledger claims to be open and has been silent for a day; the
    older one is actually running. The dashboard must open the running one."""
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=False)
    stale = build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=False)
    age(stale, 86400)
    assert state.pick_run(tmp_path) == "run-20260101T000000-aaaaaa"


def test_pick_run_with_only_stale_runs_opens_the_newest(tmp_path: Path) -> None:
    for run_id in ("run-20260101T000000-aaaaaa", "run-20260102T000000-bbbbbb"):
        age(build_run(tmp_path, run_id, finish=False), 86400)
    assert state.pick_run(tmp_path) == "run-20260102T000000-bbbbbb"


# -- the view ----------------------------------------------------------------


def test_a_stale_view_is_interrupted_and_its_clock_stops(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-dead", finish=False)
    age(store, 86400)
    view = state.load(fresh(tmp_path, "run-dead"), SPECS)
    payload = view.to_json()
    assert payload["interrupted"] is True
    assert payload["live"] is False
    assert payload["completed"] is False
    # Measured to the last entry, not to now: a dead run is not a day long.
    assert view.elapsed_s < 60


def test_the_rail_and_the_live_route_agree_about_a_stale_run(tmp_path: Path) -> None:
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=False)
    age(build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=False), 86400)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    rows = {r["run_id"]: r for r in client.get("/api/runs").json()}
    assert rows["run-20260102T000000-bbbbbb"]["live"] is False
    assert rows["run-20260101T000000-aaaaaa"]["live"] is True
    assert client.get("/api/runs/live").json()["run_id"] == "run-20260101T000000-aaaaaa"
    body = client.get("/api/runs/run-20260102T000000-bbbbbb").json()
    assert body["interrupted"] is True


async def test_a_stale_run_starts_no_tail_thread(tmp_path: Path) -> None:
    age(build_run(tmp_path, "run-stale", finish=False), 86400)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-stale")
    assert watcher.done is True
    assert watcher._thread is None


async def test_a_watched_run_that_goes_silent_ends_its_stream_as_interrupted(
    tmp_path: Path,
) -> None:
    store = build_run(tmp_path, "run-dies", finish=False)
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-dies")
    client = watcher.attach()
    assert watcher._thread is not None
    age(store, 86400)                       # the process writing it was killed
    done = await next_event(client, "done")
    assert done["completed"] is False
    assert done["interrupted"] is True
    assert watcher.view.interrupted is True


# -- trace.tail --------------------------------------------------------------


def test_follow_stops_on_a_ledger_that_has_gone_stale(tmp_path: Path) -> None:
    """`qaas trace --follow` on a ^C'd run polled it until someone pressed ^C."""
    store = build_run(tmp_path, "run-follow", finish=False)
    age(store, 86400)
    began = time.monotonic()
    entries = list(trace.tail(store, poll=0.0, timeout_s=10))
    assert time.monotonic() - began < 5, "followed a dead ledger until the timeout"
    assert entries and entries[0].kind == "run_started"


def test_follow_from_an_offset_reads_the_cap_it_seeked_past(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-offset-stale", finish=False)
    size = store.ledger_path.stat().st_size
    age(store, 900 + trace.STALE_MARGIN_S + 60)
    began = time.monotonic()
    assert list(trace.tail(store, poll=0.0, start_offset=size, timeout_s=10)) == []
    assert time.monotonic() - began < 5


def test_follow_keeps_following_a_quiet_ledger_inside_its_cap(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-quiet", finish=False)
    age(store, 600)                        # quiet, but well inside 900s + margin
    seen = list(trace.tail(store, from_start=False, poll=0.0, timeout_s=0.2))
    assert seen == []                      # ended by the timeout, not by staleness


def test_follow_does_not_mangle_a_character_split_across_polls(tmp_path: Path) -> None:
    """The tail reads bytes; a text-mode read decoded half a character as U+FFFD."""
    store = RunStore("run-utf8", tmp_path)
    store.log("run_started", mode="nightly", wall_clock_s=900)
    line = LedgerEntry(kind="escalation", detail={"reason": "naïve — café"}).model_dump_json()
    raw = (line + "\n").encode("utf-8")
    cut = raw.index("ï".encode()) + 1                      # inside the two-byte ï
    with store.ledger_path.open("ab") as handle:
        handle.write(raw[:cut])
    stream = trace.tail(store, poll=0.0, timeout_s=10)
    assert next(stream).kind == "run_started"     # the half character is now read
    with store.ledger_path.open("ab") as handle:
        handle.write(raw[cut:])
    entry = next(stream)
    stream.close()
    assert entry.detail["reason"] == "naïve — café"


# -- the replay's offset (issue: lost entries) --------------------------------


def test_replay_stops_at_the_last_newline(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-half", finish=False)
    whole = store.ledger_path.stat().st_size
    with store.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"at":"2026-01-01T00:00:00Z","kind":"agent_sta')
    entries, offset = state.replay(fresh(tmp_path, "run-half"))
    assert offset == whole, "the tail must start at the half-written line, not past it"
    assert entries[-1].kind != "agent_started"


async def test_a_line_half_written_during_the_replay_reaches_the_live_view(
    tmp_path: Path,
) -> None:
    """The tail started at `stat().st_size`, past the half line the replay had
    skipped -- so the entry was in neither half, permanently."""
    store = build_run(tmp_path, "run-torn", finish=False)
    line = LedgerEntry(kind="agent_started", agent="REPRODUCER",
                       detail={"model": "m"}).model_dump_json() + "\n"
    with store.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line[:20])
    watcher = Dashboard(tmp_path, specs=SPECS, poll=FAST).watcher("run-torn")
    client = watcher.attach()
    with store.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line[20:])
    data = await next_event(client, "ledger")
    assert (data["kind"], data["agent"]) == ("agent_started", "REPRODUCER")
    assert watcher.view.agents["REPRODUCER"].status == "running"


async def test_a_line_appended_between_replay_and_start_is_not_lost(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-gap", finish=False)
    view = state.load(fresh(tmp_path, "run-gap"), SPECS)
    store.log("agent_started", agent="REPRODUCER", model="m")   # after the read
    watcher = RunWatcher(view, poll=FAST)
    watcher.start()
    client = watcher.attach()
    data = await next_event(client, "ledger")
    assert data["agent"] == "REPRODUCER"


# -- the run rail (issue: /api/runs took 3.2s on the event loop) --------------


def test_the_run_rail_does_not_parse_every_ledger_line(tmp_path: Path, monkeypatch) -> None:
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=False)
    build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=True)

    def refuse(*args, **kwargs):
        raise AssertionError("the run rail validated ledger lines one by one")

    monkeypatch.setattr(state, "read_ledger", refuse)
    monkeypatch.setattr(LedgerEntry, "model_validate_json", classmethod(refuse))
    rows = {r["run_id"]: r for r in state.list_runs_summary(tmp_path)}
    assert rows["run-20260101T000000-aaaaaa"]["live"] is True
    assert rows["run-20260102T000000-bbbbbb"]["live"] is False
    assert rows["run-20260102T000000-bbbbbb"]["mode"] == "pr-check"


def test_the_run_rail_matches_the_parsed_view(tmp_path: Path) -> None:
    store = build_run(tmp_path, "run-match", finish=False)
    row = state.list_runs_summary(tmp_path)[0]
    view = state.load(fresh(tmp_path, "run-match"), SPECS)
    assert row["started"] == view.to_json()["started"]
    assert row["mode"] == view.mode

    # Appended to after the first scan: picked up from where it stopped.
    store.log("run_finished", run_id="run-match")
    store.log("run_started", mode="nightly", agents=["API"], wall_clock_s=7200)
    row = state.list_runs_summary(tmp_path)[0]
    assert row["mode"] == "nightly"
    assert row["live"] is True


def test_the_run_rail_rescans_a_ledger_that_was_replaced(tmp_path: Path) -> None:
    """The scan resumes from its last offset because the ledger is append-only.
    A file that was replaced -- shorter, or longer with different bytes where
    the last scan stopped -- is read again from the start, not trusted."""
    store = build_run(tmp_path, "run-replaced", finish=False)
    assert state.list_runs_summary(tmp_path)[0]["live"] is True

    store.ledger_path.unlink()
    for _ in range(40):                    # longer than before, different bytes
        store.log("run_started", mode="nightly", agents=["API"], wall_clock_s=7200)
        store.log("run_finished", run_id="run-replaced")
    row = state.list_runs_summary(tmp_path)[0]
    assert row["mode"] == "nightly"
    assert row["live"] is False

    store.ledger_path.write_text(
        LedgerEntry(kind="run_started", detail={"mode": "pr-check"}).model_dump_json() + "\n"
    )                                      # shorter than the last scan's offset
    row = state.list_runs_summary(tmp_path)[0]
    assert row["mode"] == "pr-check"
    assert row["live"] is True


def test_the_runs_route_scans_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    build_run(tmp_path)
    where: list[bool] = []
    real = state.list_runs_summary

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            where.append(True)
        except RuntimeError:
            where.append(False)
        return real(*args, **kwargs)

    monkeypatch.setattr(state, "list_runs_summary", spy)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    assert client.get("/api/runs").status_code == 200
    assert where == [False], "the scan ran on the event loop and froze every stream"


# -- the SSE handoff (issue: entries delivered twice) -------------------------


async def test_the_snapshot_is_taken_as_the_client_attaches(tmp_path: Path) -> None:
    """The snapshot was serialised inside the generator, after the response
    started, while `_pump` kept applying entries *and* queueing them for the
    new client -- so an entry in that gap arrived in the snapshot and again as
    a delta."""
    from starlette.requests import Request

    from qaas.ui import server

    store = build_run(tmp_path, "run-dup", finish=False)
    dash = Dashboard(tmp_path, specs=SPECS, poll=FAST)
    app = build_app(dash)
    request = Request({
        "type": "http", "app": app, "method": "GET", "path": "/", "headers": [],
        "query_string": b"", "path_params": {"run_id": "run-dup"},
    })
    response = await server._stream(request)
    watcher = dash.watchers["run-dup"]

    store.log("agent_started", agent="REPRODUCER", model="m")
    async with asyncio.timeout(EVENT_TIMEOUT):
        while "REPRODUCER" not in watcher.view.agents:
            await asyncio.sleep(0.01)

    events = response.body_iterator
    try:
        first = await events.__anext__()
        assert first["event"] == "snapshot"
        snapshot = json.loads(first["data"])
        second = await events.__anext__()
    finally:
        await events.aclose()
    delivered_twice = "REPRODUCER" in snapshot["agents"] and second["event"] == "ledger"
    assert not delivered_twice, "the entry arrived in the snapshot and again as a delta"
    assert "REPRODUCER" not in snapshot["agents"]
    assert json.loads(second["data"])["agent"] == "REPRODUCER"
