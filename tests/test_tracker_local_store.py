"""LocalTracker across processes: key claims, atomic writes, tolerant reads.

Four processes filing forty tickets each left 19 files for 160 calls, and ~110
of the calls died on `ValidationError` reading a file another process was half
way through writing. Keys were "highest number on disk, plus one", written with
a plain `write_text`; and one truncated file made every later `issues()` raise,
which took `search` and all further filing down with it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from qaas.adapters.tracker import LocalTracker, TrackerError

_WORKER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from qaas.adapters.tracker import LocalTracker

    root, go, worker, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    while not go.exists():          # start together, so the race is a race
        time.sleep(0.001)
    tracker = LocalTracker(root)
    failures = 0
    for i in range(count):
        try:
            print(tracker.create_issue(project="CORVID", title=f"w{worker} #{i}", body="b").key)
            tracker.search(text="no such ticket")   # a reader racing the writers
        except Exception as exc:
            failures += 1
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1 if failures else 0)
    """
)


def test_four_processes_filing_forty_tickets_each_keep_all_160(tmp_path):
    go = tmp_path / "go"
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(tmp_path), str(go), str(n), "40"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for n in range(4)
    ]
    go.touch()
    outputs = [w.communicate(timeout=120) for w in workers]

    for worker, (_, stderr) in zip(workers, outputs):
        assert worker.returncode == 0, stderr
    keys = [line for stdout, _ in outputs for line in stdout.split()]
    assert len(keys) == 160
    assert len(set(keys)) == 160, "two processes were handed the same key"

    tracker = LocalTracker(tmp_path)
    assert len(tracker.issues()) == 160
    assert tracker.unreadable_files == 0
    assert list((tmp_path / "tickets").glob("*.tmp")) == [], "no temp file left behind"


def _truncate(tracker: LocalTracker, key: str) -> None:
    path = tracker.dir / f"{key}.json"
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")


def test_a_truncated_ticket_is_skipped_and_counted_not_fatal(tmp_path):
    tracker = LocalTracker(tmp_path)
    first = tracker.create_issue(project="CORVID", title="Refund authz", body="b").key
    tracker.create_issue(project="CORVID", title="Slow orders list", body="b")
    _truncate(tracker, first)

    assert [i.title for i in tracker.issues()] == ["Slow orders list"]
    assert tracker.unreadable_files == 1
    assert [i.title for i in tracker.search(text="orders")] == ["Slow orders list"]

    # Filing still works, and never reuses the unreadable ticket's number.
    third = tracker.create_issue(project="CORVID", title="Third", body="b")
    assert third.key == "CORVID-3"


def test_reading_a_truncated_ticket_by_key_is_a_tracker_error(tmp_path):
    """`ValidationError` escaped here, past every caller's `except TrackerError`."""
    tracker = LocalTracker(tmp_path)
    key = tracker.create_issue(project="CORVID", title="Refund authz", body="b").key
    _truncate(tracker, key)

    with pytest.raises(TrackerError, match="could not be read"):
        tracker.get(key)
    with pytest.raises(TrackerError):
        tracker.transition(key, "in_progress")


def test_a_claimed_but_unwritten_key_is_never_handed_out_again(tmp_path):
    """Another process's claim is an empty file until it is written. Its number is
    taken all the same, and the empty file does not break anyone's scan."""
    tracker = LocalTracker(tmp_path)
    (tracker.dir / "CORVID-1.json").touch()

    issue = tracker.create_issue(project="CORVID", title="Mine", body="b")

    assert issue.key == "CORVID-2"
    assert [i.key for i in tracker.issues()] == ["CORVID-2"]
    assert tracker.unreadable_files == 1


def test_a_failed_create_releases_its_claim(tmp_path, monkeypatch):
    tracker = LocalTracker(tmp_path)

    def refuse(self, issue):
        raise OSError("disk full")

    monkeypatch.setattr(LocalTracker, "_write", refuse)
    with pytest.raises(OSError):
        tracker.create_issue(project="CORVID", title="Lost", body="b")

    assert list(tracker.dir.iterdir()) == []


async def test_the_search_tool_says_when_it_skipped_a_ticket(tmp_path):
    """A dedupe that silently read less than the whole backlog would report "no
    duplicate" with confidence."""
    from support import CONFIG_SEARCH

    from qaas.config import load_config
    from qaas.mcp.context import ToolContext, handlers
    from qaas.mcp.tracker import build_tools
    from qaas.store import RunStore, SystemMapStore

    config = load_config(search=CONFIG_SEARCH)
    root = tmp_path / ".qaas"
    ctx = ToolContext(
        store=RunStore.new(root=root), maps=SystemMapStore(root=root), config=config,
        agent=config.agents["TRIAGE"], target_root=Path(tmp_path),
    )
    tracker = LocalTracker(root)
    key = tracker.create_issue(project="CORVID", title="Refund authz", body="b").key
    tracker.create_issue(project="CORVID", title="Slow orders list", body="b")
    _truncate(tracker, key)

    result = await handlers(build_tools(ctx))["search"]({})

    assert not result.get("is_error"), result
    assert result["structuredContent"]["count"] == 1
    assert result["structuredContent"]["unreadable"] == 1
    assert "could not be read" in result["content"][0]["text"]
