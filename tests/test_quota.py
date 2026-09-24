"""A provider session limit is a pause, not a failure.

The first run of the whole roster stopped on "You've hit your session limit ·
resets 3:50pm (America/New_York)" with QAAS-61 half done, and the dashboard
drew REPRODUCER as a red "failed" box -- for an agent that had done nothing
wrong, in a run that only needed to wait. A limit that says when it lifts is now
waited out and the same agent retried; one that does not, or lifts after the
run's own clock, still stops the run with a resume command.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from qaas import quota, router as conductor_mod
from qaas.router import QUOTA_MAX_WAITS, QUOTA_RESET_MARGIN_S, _quota_pause_for
from qaas.store import RunStore
from test_router import cfg, fake_agents, make_conductor  # noqa: F401 - fixtures

#: The conftest stubs the router's copy; this is the real parser.
reset_at = quota.reset_at

NOON_NY = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)  # 12:00 EDT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You've hit your session limit · resets 3:50pm (America/New_York)",
         datetime(2026, 9, 24, 19, 50, tzinfo=timezone.utc)),
        ("limit reached · resets 12am (UTC)", datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)),
        ("usage limit · resets Sep 26, 9:05am (Europe/London)",
         datetime(2026, 9, 26, 8, 5, tzinfo=timezone.utc)),
        ("429 Too Many Requests: retry after 30 seconds", NOON_NY + timedelta(seconds=30)),
        ("rate limited, try again in 5 minutes", NOON_NY + timedelta(minutes=5)),
    ],
)
def test_the_reset_time_is_read_from_the_message(text, expected):
    assert reset_at(text, NOON_NY) == expected


def test_a_time_already_past_is_tomorrow_unless_it_just_passed():
    assert reset_at("resets 11am (America/New_York)", NOON_NY) == datetime(
        2026, 9, 25, 15, 0, tzinfo=timezone.utc
    )
    assert reset_at("resets 11:55am (America/New_York)", NOON_NY) == datetime(
        2026, 9, 24, 15, 55, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "text",
    [None, "", "session limit", "resets 4", "resets 25:00", "resets Feb 30, 1pm (UTC)",
     "AssertionError: expected 200"],
)
def test_anything_unreadable_is_no_time_at_all(text):
    assert reset_at(text, NOON_NY) is None


def test_an_unknown_zone_is_this_machines():
    assert reset_at("resets 3:50pm (Mars/Olympus)", NOON_NY) is not None


# -- whether to wait -----------------------------------------------------------


def _pause(text, *, left=28800, max_wait=21600):
    return _quota_pause_for(text, now=NOON_NY, seconds_left=left, max_wait=max_wait)


def test_a_reset_inside_the_run_is_waited_for_with_a_margin(monkeypatch):
    monkeypatch.setattr(conductor_mod, "quota_reset_at", reset_at)
    until, seconds = _pause("resets 3:50pm (America/New_York)")
    assert seconds == pytest.approx(3 * 3600 + 50 * 60 + QUOTA_RESET_MARGIN_S)
    assert until == NOON_NY + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("left", "max_wait", "why"),
    [
        (3 * 3600, 21600, "the run's clock ends before the reset"),
        (28800, 3600, "longer than quota_wait_max_s"),
        (28800, 0, "waiting is switched off"),
    ],
)
def test_a_reset_the_run_cannot_use_stops_it(monkeypatch, left, max_wait, why):
    monkeypatch.setattr(conductor_mod, "quota_reset_at", reset_at)
    assert _pause("resets 3:50pm (America/New_York)", left=left, max_wait=max_wait) is None, why


# -- the router waits, then retries the same agent -------------------------------


def _waiting_router(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(conductor_mod, "quota_reset_at", lambda text, now=None: (now or NOON_NY) + timedelta(minutes=5))
    router = make_conductor(cfg, tmp_path)
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    router._sleep = sleep
    return router, slept


async def test_a_limit_that_lifts_is_waited_out_and_the_agent_retried(cfg, tmp_path, fake_agents, monkeypatch):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    behaviour["API"] = {"emit": 2}
    refusals = iter([True])

    def limited_once(ctx, spec):
        if next(refusals, False):
            behaviour["REPRODUCER"] = {}
    behaviour["REPRODUCER"] = {
        "subtype": "failure",
        "error": "You've hit your session limit · resets 3:50pm (America/New_York)",
        "hook": limited_once,
    }

    router, slept = _waiting_router(cfg, tmp_path, monkeypatch)
    report = await router.run("nightly")
    store = RunStore(report.run_id, tmp_path, create=False)

    assert not report.quota_exhausted and report.stopped_early is None
    assert slept and slept[0] == pytest.approx(5 * 60 + QUOTA_RESET_MARGIN_S, abs=2)
    names = [n for n, _ in calls]
    assert names.count("REPRODUCER") == 3, "the refused finding was not retried"
    assert "TRIAGE" in names and "REPORTER" in names
    waits = list(store.ledger("quota_wait"))
    assert len(waits) == 1 and waits[0].agent == "REPRODUCER" and waits[0].detail["until"]
    assert not list(store.ledger("quota_exhausted"))


async def test_a_limit_that_does_not_lift_stops_after_the_waits_run_out(cfg, tmp_path, fake_agents, monkeypatch):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"subtype": "failure", "error": "session limit · resets 3:50pm"}
    router, slept = _waiting_router(cfg, tmp_path, monkeypatch)
    report = await router.run("nightly")

    assert [n for n, _ in calls] == ["MAPPER"] * (QUOTA_MAX_WAITS + 1)
    assert len(slept) >= QUOTA_MAX_WAITS
    assert report.quota_exhausted and report.resume_command


async def test_queued_work_waits_for_the_reset_instead_of_walking_into_it(cfg, tmp_path, monkeypatch):
    from datetime import datetime as real_datetime

    router, slept = _waiting_router(cfg, tmp_path, monkeypatch)
    router._quota_until = real_datetime.now(timezone.utc) + timedelta(minutes=10)

    async def boom(*a, **k):
        raise RuntimeError("stop after the gate")

    monkeypatch.setattr(conductor_mod, "run_agent", boom)
    store = RunStore.new(tmp_path)
    with pytest.raises(RuntimeError):
        from qaas.router import Budget, RunReport

        await router._dispatch(
            cfg.agents["API"], store, Budget(None, 3600), RunReport(store.run_id, "nightly"),
            "task", None,
        )
    assert slept and 590 < slept[0] <= 600
