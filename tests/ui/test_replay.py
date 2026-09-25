"""Playing a finished run back as if it were live.

For a demo, and for anyone reading a run afterwards: the finished view shows the
end state all at once, and the story -- which agent ran when, where the finding
appeared, when the ticket was filed and verified -- is only visible while it
happens. A replay walks the ledger through the same `apply` and the same events
a live run sends, compressed by `speed`, with every idle stretch capped.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qaas.store import RunStore
from qaas.ui import server
from qaas.ui.server import REPLAY_MAX_GAP_S, Dashboard, build_app

from .conftest import SPECS, build_run, local_client

RUN = "run-20260101T000000-aaaaaa"


def _events(client, url: str) -> list[tuple[str, dict]]:
    out, event = [], None
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                out.append((event, json.loads(line.split(":", 1)[1])))
    return out


@pytest.fixture
def paced(monkeypatch):
    pauses: list[float] = []

    async def record(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(server, "_pause", record)
    return pauses


def test_a_finished_run_plays_back_from_an_empty_page(tmp_path: Path, paced) -> None:
    build_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    events = _events(client, f"/api/runs/{RUN}/replay?speed=600")

    kinds = [kind for kind, _ in events]
    first = events[0][1]
    assert kinds[0] == "snapshot" and not first["findings"] and first["replay"] == 600
    assert "finding" in kinds and "ticket" in kinds, "the story arrived all at once, or not at all"
    assert kinds.index("finding") < kinds.index("ticket"), "a ticket before the finding it files"
    assert kinds[-1] == "done" and events[-1][1]["replay"] is True
    patches = [data for kind, data in events if kind == "patch"]
    assert all(p["replay"] == 600 for p in patches)
    assert patches[-1]["completed"] is True


def _timed_run(root: Path) -> datetime:
    """A run whose lines are hours apart, as a real one's are."""
    store = RunStore(RUN, root=root, create=True)
    t0 = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
    lines = [
        (0, "run_started", None, {"mode": "full-loop", "agents": ["API"], "wall_clock_s": 28800}),
        (60, "agent_started", "API", {"model": "claude-opus-5"}),
        (3 * 3600, "agent_finished", "API", {"cost_usd": 1.0, "num_turns": 9}),
        (3 * 3600 + 1, "run_finished", None, {}),
    ]
    with store.ledger_path.open("w", encoding="utf-8") as fh:
        for offset, kind, agent, detail in lines:
            at = (t0 + timedelta(seconds=offset)).isoformat()
            fh.write(json.dumps({"at": at, "kind": kind, "agent": agent, "detail": detail}) + "\n")
    return t0


def test_pauses_follow_the_run_and_never_stall(tmp_path: Path, paced) -> None:
    _timed_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    _events(client, f"/api/runs/{RUN}/replay?speed=60")
    # 60s at x60 is one second; three hours at x60 would be three minutes of a
    # frozen screen, and is capped.
    assert paced[0] == pytest.approx(1.0)
    assert max(paced) == REPLAY_MAX_GAP_S


def test_the_replayed_clock_is_the_runs_not_the_walls(tmp_path: Path, paced) -> None:
    """The run was hours ago. Measured to now, every card read "running for days"."""
    _timed_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    events = _events(client, f"/api/runs/{RUN}/replay?speed=60")
    mid = next(data for kind, data in events if kind == "patch" and data["agents"].get("API", {}).get("status") == "running")
    assert mid["elapsed_s"] == pytest.approx(60, abs=1)
    final = [data for kind, data in events if kind == "patch"][-1]
    assert final["elapsed_s"] == pytest.approx(3 * 3600 + 1, abs=1)


def test_an_unknown_run_is_a_404(tmp_path: Path) -> None:
    build_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    assert client.get("/api/runs/run-nope/replay").status_code == 404


def test_the_page_offers_it(tmp_path: Path) -> None:
    build_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    page = client.get("/").text
    assert 'id="replay-btn"' in page and 'id="replay-speed"' in page


# -- what the stream says, live or replayed -------------------------------------


def test_a_revised_finding_is_sent_as_itself_not_as_the_newest(tmp_path: Path, paced) -> None:
    """An envelope is re-logged when it is revised. The stream sent the newest
    finding for every such line, and the page listed one defect three times."""
    store = build_run(tmp_path, finish=False)
    first = next(iter(store.envelopes()))
    from .conftest import make_envelope

    second = make_envelope(RUN)
    store.put_envelope(second)                      # a newer finding
    first.jira.key = "QAAS-9"
    store.put_envelope(first)                       # the older one, revised
    store.log("run_finished")
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    sent = [data["id"] for kind, data in _events(client, f"/api/runs/{RUN}/replay?speed=1000") if kind == "finding"]
    assert sent[-1] == first.id, "the revision was announced as the newest finding"
    assert set(sent) == {first.id, second.id}


def test_denials_and_the_run_header_reach_the_page(tmp_path: Path, paced) -> None:
    build_run(tmp_path)
    client = local_client(build_app(Dashboard(tmp_path, specs=SPECS)))
    events = _events(client, f"/api/runs/{RUN}/replay?speed=1000")
    denials = [data for kind, data in events if kind == "denial"]
    assert denials and "allowlist" in denials[0]["reason"]
    last = [data for kind, data in events if kind == "patch"][-1]
    assert last["started"] and last["mode"] == "pr-check" and last["target_name"] == "corvid"
