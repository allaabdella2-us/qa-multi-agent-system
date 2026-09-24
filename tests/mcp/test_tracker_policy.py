"""Tracker policy regressions: routing, project scope, per-agent statuses, the loop.

Each test here is a hole a reviewer found in `qaas.mcp.tracker` before 0.0.2:
routing that depended on an optional argument, a `project` taken as given, a
transition right that meant any status on any key, and blocking HTTP on the one
event loop every agent shares. Tested against the shipped agent specs, so a
policy loosened in YAML fails here rather than in production.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import is_error, make_envelope, text_of

from qaas.adapters.tracker import DEFAULT_PROJECT, SECURITY_PROJECT, LocalTracker
from qaas.config import AgentSpec, Policy
from qaas.mcp.context import handlers
from qaas.mcp.tracker import CREATE_SCHEMA, TRANSITION_STATUSES, build_tools

BODY = "Repro: POST /v1/refunds as user B. Evidence: artifact://x/log. AC: test_refund_authz passes."


def _tools(ctx) -> dict:
    return handlers(build_tools(ctx))


async def _file(ctx, tools, title: str = "A defect", **extra) -> dict:
    if "envelope_id" not in extra:
        extra["envelope_id"] = make_envelope(ctx, title=title).id
    return await tools["create_issue"]({"title": title, "body": BODY, **extra})


def _schema(ctx, name: str) -> dict:
    return next(t for t in build_tools(ctx) if t.name == name).input_schema


def _denials(ctx, tool: str) -> list:
    return [d for d in ctx.store.ledger("denial") if d.detail["tool"] == tool]


# -- 1. routing is decided from the envelope, and the envelope is required ---


def test_the_create_schema_requires_an_envelope(ctx):
    assert "envelope_id" in CREATE_SCHEMA["required"]
    assert "envelope_id" in _schema(ctx, "create_issue")["required"]


async def test_a_security_domain_finding_is_restricted_without_the_flag_or_the_class(ctx):
    """The domain alone is enough: a finding from the security surface that ticked
    neither `security_relevant` nor `class: vulnerability` is still a disclosure."""
    envelope = make_envelope(ctx, domain="security", impact={"security_relevant": False})
    assert envelope.defect_class.value == "bug"

    result = await _file(ctx, _tools(ctx), envelope_id=envelope.id)

    assert not is_error(result), text_of(result)
    assert result["structuredContent"]["project"] == SECURITY_PROJECT
    assert result["structuredContent"]["restricted"] is True


async def test_a_vulnerability_filed_without_an_envelope_never_reaches_the_public_project(ctx):
    """The reported hole: no `envelope_id`, so routing never looked at anything."""
    make_envelope(ctx, domain="security", **{"class": "vulnerability"})
    tools = _tools(ctx)

    result = await tools["create_issue"]({"title": "Refund authz bypass", "body": BODY})

    assert is_error(result)
    assert "`envelope_id` is required" in text_of(result)
    assert LocalTracker(ctx.store.root).issues() == []


async def test_an_envelope_that_already_carries_a_ticket_is_not_filed_again(ctx):
    """A retry after a create whose reply was lost, or a resumed run: the stamp is
    the record, and filing past it is a duplicate."""
    tools = _tools(ctx)
    envelope = make_envelope(ctx)
    first = await _file(ctx, tools, envelope_id=envelope.id)
    assert not is_error(first), text_of(first)

    again = await _file(ctx, tools, envelope_id=envelope.id)

    assert is_error(again)
    assert first["structuredContent"]["key"] in text_of(again)
    assert len(LocalTracker(ctx.store.root).issues()) == 1
    assert ctx.count("tickets") == 1
    assert _denials(ctx, "create_issue")


# -- 2. the project allowlist ------------------------------------------------


async def test_a_project_outside_the_configured_two_is_refused(ctx):
    result = await _file(ctx, _tools(ctx), project="SOMEONE-ELSES")

    assert is_error(result)
    assert "SOMEONE-ELSES" in text_of(result)
    assert DEFAULT_PROJECT in text_of(result) and SECURITY_PROJECT in text_of(result)
    assert LocalTracker(ctx.store.root).issues() == []
    assert ctx.count("tickets") == 0
    assert _denials(ctx, "create_issue")


async def test_an_ordinary_finding_may_be_filed_restricted_and_the_key_is_normalised(ctx):
    """Over-restricting costs a click; the allowlist is about *other* projects.
    A lower-cased key is ours, spelled the way the backend spells it."""
    tools = _tools(ctx)
    restricted = await _file(ctx, tools, project=SECURITY_PROJECT)
    lower = await _file(ctx, tools, title="Another", project=DEFAULT_PROJECT.lower())

    assert restricted["structuredContent"]["project"] == SECURITY_PROJECT
    assert lower["structuredContent"]["project"] == DEFAULT_PROJECT


def test_the_schemas_name_only_the_configured_projects(ctx):
    for name in ("create_issue", "search"):
        assert _schema(ctx, name)["properties"]["project"]["enum"] == [DEFAULT_PROJECT, SECURITY_PROJECT]


async def test_search_refuses_a_project_outside_the_configured_two(ctx):
    result = await _tools(ctx)["search"]({"project": "HR"})
    assert is_error(result)
    assert "HR" in text_of(result)


# -- 3. transition and link: which keys, which statuses, and an honest refusal -


@pytest.fixture
def filed(ctx):
    """A TRIAGE context with one ticket filed; returns (ctx, key)."""

    async def _make():
        result = await _file(ctx, _tools(ctx))
        return result["structuredContent"]["key"]

    return _make


async def test_fixer_may_start_and_hand_over_work_but_never_close_it(ctx, make_ctx, filed):
    key = await filed()
    fixer = make_ctx("FIXER", root=ctx.store.root)
    tools = _tools(fixer)

    for status in ("in_progress", "in_review"):
        moved = await tools["transition"]({"key": key, "status": status, "comment": "on fix/x"})
        assert not is_error(moved), text_of(moved)

    for status in ("resolved", "closed", "wont_fix", "open"):
        refused = await tools["transition"]({"key": key, "status": status})
        assert is_error(refused), status
        assert "in_progress, in_review" in text_of(refused)

    closing = await tools["transition"]({"key": key, "status": "closed"})
    assert "VERIFIER" in text_of(closing)
    assert LocalTracker(ctx.store.root).get(key).status == "in_review"
    assert len(_denials(fixer, "transition")) == 5


async def test_verifier_may_close_or_reopen_but_not_start_work(ctx, make_ctx, filed):
    key = await filed()
    verifier = make_ctx("VERIFIER", root=ctx.store.root)
    tools = _tools(verifier)

    assert not is_error(await tools["transition"]({"key": key, "status": "resolved"}))
    assert not is_error(await tools["transition"]({"key": key, "status": "open"}))
    refused = await tools["transition"]({"key": key, "status": "in_progress"})
    assert is_error(refused)
    assert "open, resolved, closed" in text_of(refused)


def test_the_status_table_matches_what_each_agent_is_told(ctx, make_ctx):
    """FIXER.md tells it `in_progress`; VERIFIER closes or reopens; TRIAGE is told
    to move nothing. The schema shows each agent only its own list."""
    assert _schema(make_ctx("FIXER"), "transition")["properties"]["status"]["enum"] == [
        "in_progress", "in_review",
    ]
    assert TRANSITION_STATUSES["FIXER"] == ("in_progress", "in_review")
    assert set(TRANSITION_STATUSES["VERIFIER"]) == {"open", "resolved", "closed"}
    assert "TRIAGE" not in TRANSITION_STATUSES
    triage_schema = _schema(ctx, "transition")  # TRIAGE: not in the table, full enum
    assert "closed" in triage_schema["properties"]["status"]["enum"]


async def test_the_refusal_names_who_actually_holds_the_transition_right(make_ctx):
    """It said "TRIAGE and VERIFIER only" while FIXER held the right and TRIAGE did not."""
    api = make_ctx("API")
    result = await _tools(api)["transition"]({"key": f"{DEFAULT_PROJECT}-1", "status": "closed"})

    assert is_error(result)
    text = text_of(result)
    assert "may not transition tickets" in text
    assert "FIXER" in text and "VERIFIER" in text
    assert "TRIAGE and VERIFIER only" not in text


async def test_an_agent_granted_the_right_but_told_no_status_is_refused_every_status(make_ctx, ctx, filed):
    key = await filed()
    custom = make_ctx("CUSTOM", root=ctx.store.root)
    custom.agent = AgentSpec(
        name="CUSTOM", layer="remediation", role="test double", prompt="CUSTOM.md",
        policy=Policy(may_transition_tickets=True),
    )
    result = await _tools(custom)["transition"]({"key": key, "status": "in_progress"})
    assert is_error(result)
    assert "TRANSITION_STATUSES" in text_of(result)


async def test_transition_and_link_refuse_keys_outside_the_configured_projects(ctx, make_ctx, filed):
    key = await filed()
    verifier = make_ctx("VERIFIER", root=ctx.store.root)
    tools = _tools(verifier)

    moved = await tools["transition"]({"key": "PAYROLL-7", "status": "closed"})
    assert is_error(moved)
    assert "PAYROLL" in text_of(moved)

    linked = await tools["link"]({"key": key, "to": "PAYROLL-7", "type": "relates"})
    assert is_error(linked)
    assert "PAYROLL" in text_of(linked)

    assert LocalTracker(ctx.store.root).get(key).links == []
    assert _denials(verifier, "transition") and _denials(verifier, "link")


# -- 7. the adapter runs off the event loop ---------------------------------


async def test_a_slow_tracker_does_not_freeze_the_event_loop(ctx, monkeypatch):
    """Every concurrent agent's tool calls share this loop. A blocking adapter call
    -- a 30s Jira timeout, a 429 backoff -- stopped all of them, not just its own."""

    def slow_search(self, **_kwargs):
        time.sleep(0.4)
        return []

    monkeypatch.setattr(LocalTracker, "search", slow_search)
    tools = _tools(ctx)
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    # Only ticks taken *while* the search is in flight count: inline, the search
    # holds the loop for its whole 0.4s and the ticker gets one tick either side.
    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    result = await tools["search"]({"text": "x"})
    done.set()
    await task

    assert not is_error(result)
    assert ticks >= 10, f"the loop ran {ticks} times in 0.4s while the tracker was busy"


async def test_an_unexpected_backend_exception_is_returned_not_raised(ctx, monkeypatch):
    """Handlers caught `TrackerError` only, so anything else escaped the tool."""

    def broken(self, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(LocalTracker, "create_issue", broken)
    result = await _file(ctx, _tools(ctx))

    assert is_error(result)
    assert "RuntimeError" in text_of(result) and "disk on fire" in text_of(result)
    assert ctx.count("tickets") == 0


async def test_parallel_creates_cannot_overrun_the_cap(make_ctx, ctx):
    """The cap check and the count are one step even with the backend on a thread."""
    capped = make_ctx(
        "TRIAGE",
        root=ctx.store.root,
        config=ctx.config.model_copy(
            update={"thresholds": ctx.config.thresholds.model_copy(update={"max_tickets_per_run": 2})}
        ),
    )
    tools = _tools(capped)
    envelopes = [make_envelope(capped, title=f"Defect {i}") for i in range(5)]

    results = await asyncio.gather(
        *(tools["create_issue"]({"title": e.title, "body": BODY, "envelope_id": e.id}) for e in envelopes)
    )

    assert sum(not is_error(r) for r in results) == 2
    assert len(LocalTracker(ctx.store.root).issues()) == 2
