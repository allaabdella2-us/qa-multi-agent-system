"""Tracker: the guardrails that must hold even when the prompt does not.

§8.1 write matrix, §4.12 per-run cap, §10 "security findings leak into public
tickets". Each is tested against the *shipped* agent specs from `config/`, so a
policy loosened in YAML fails here rather than in production.
"""

from __future__ import annotations

import pytest
from conftest import is_error, make_envelope, text_of

from qaas.adapters.tracker import (
    DEFAULT_PROJECT,
    SECURITY_PROJECT,
    JiraTracker,
    LocalTracker,
    TrackerConfigError,
    build_tracker,
)
from qaas.mcp.context import handlers
from qaas.mcp.tracker import build_tools

BODY = "Repro: POST /v1/refunds as user B. Evidence: artifact://x/log. AC: test_refund_authz passes."


@pytest.fixture
def triage(ctx):
    return ctx, handlers(build_tools(ctx))


def _file(tools, title: str = "Refund endpoint accepts any authenticated user", **extra):
    return tools["create_issue"]({"title": title, "body": BODY, **extra})


# -- §8.1 write permission matrix -----------------------------------------


async def test_a_read_only_agent_may_not_create_issues(make_ctx):
    """API is a discovery agent: it emits envelopes, TRIAGE files them."""
    ctx = make_ctx("API")
    assert ctx.agent.policy.read_only
    tools = handlers(build_tools(ctx))

    result = await _file(tools)
    assert is_error(result)
    assert "may not create tickets" in text_of(result)
    assert ctx.count("tickets") == 0

    denials = list(ctx.store.ledger("denial"))
    assert [d.detail["tool"] for d in denials] == ["create_issue"]
    assert denials[0].agent == "API"

    # And nothing reached the tracker.
    assert LocalTracker(ctx.store.root).issues() == []


async def test_a_read_only_agent_may_not_transition_issues(triage, make_ctx):
    ctx, tools = triage
    filed = await _file(tools)
    key = filed["structuredContent"]["key"]

    api = make_ctx("API", root=ctx.store.root)
    conduit_tools = handlers(build_tools(api))
    result = await conduit_tools["transition"]({"key": key, "status": "closed"})
    assert is_error(result)
    assert "may not transition tickets" in text_of(result)


async def test_proof_may_transition_but_not_create(triage, make_ctx):
    """§8.1: VERIFIER is the closing authority, never the filing one."""
    ctx, tools = triage
    key = (await _file(tools))["structuredContent"]["key"]

    verifier = make_ctx("VERIFIER", root=ctx.store.root)
    verifier_tools = handlers(build_tools(verifier))

    assert is_error(await _file(verifier_tools))

    moved = await verifier_tools["transition"](
        {"key": key, "status": "closed", "comment": "verified: test_refund_authz now passes"}
    )
    assert not is_error(moved)
    assert moved["structuredContent"]["status"] == "closed"

    logged = [e for e in verifier.store.ledger("ticket") if e.detail["action"] == "transitioned"]
    assert logged[0].detail["key"] == key
    assert logged[0].agent == "VERIFIER"


# -- §4.12 per-run rate limit ----------------------------------------------


async def test_clerk_files_up_to_the_cap_then_escalates(triage):
    ctx, tools = triage
    cap = ctx.agent.policy.max_tickets_per_run
    assert cap > 0

    for i in range(cap):
        result = await _file(tools, title=f"Defect number {i}")
        assert not is_error(result), text_of(result)
        assert result["structuredContent"]["tickets_filed"] == i + 1
    assert ctx.count("tickets") == cap

    over = await _file(tools, title="One too many")
    assert is_error(over)
    body = text_of(over)
    assert "cap" in body
    assert "escalation, not a filing problem" in body

    escalations = list(ctx.store.ledger("escalation"))
    assert len(escalations) == 1
    assert escalations[0].agent == "TRIAGE"
    assert escalations[0].detail["cap"] == cap
    assert escalations[0].detail["reason"] == "ticket cap reached"

    # The refusal is a refusal: the cap is not silently exceeded.
    assert ctx.count("tickets") == cap
    assert len(LocalTracker(ctx.store.root).issues()) == cap


# -- §10 security routing ---------------------------------------------------


async def test_a_security_relevant_envelope_routes_to_the_restricted_project(triage):
    ctx, tools = triage
    envelope = make_envelope(
        ctx,
        domain="security",
        title="Refund endpoint accepts any authenticated user",
        impact={"security_relevant": True, "user_facing": True},
    )

    result = await _file(tools, envelope_id=envelope.id)
    assert not is_error(result)
    body = result["structuredContent"]
    assert body["project"] == SECURITY_PROJECT
    assert body["project"] != DEFAULT_PROJECT
    assert body["restricted"] is True
    assert "security" in body["labels"]
    assert body["key"].startswith(SECURITY_PROJECT)


async def test_a_vulnerability_class_envelope_routes_to_the_restricted_project(triage):
    """The class alone is enough; the reporter need not also tick the flag."""
    ctx, tools = triage
    envelope = make_envelope(ctx, domain="security", **{"class": "vulnerability"})
    result = await _file(tools, envelope_id=envelope.id)
    assert result["structuredContent"]["project"] == SECURITY_PROJECT


async def test_asking_for_a_public_project_for_a_security_finding_is_refused(triage):
    ctx, tools = triage
    envelope = make_envelope(ctx, domain="security", impact={"security_relevant": True})

    result = await _file(tools, envelope_id=envelope.id, project=DEFAULT_PROJECT)
    assert is_error(result)
    assert DEFAULT_PROJECT in text_of(result)
    assert SECURITY_PROJECT in text_of(result)
    assert "never go to a public project" in text_of(result)

    assert LocalTracker(ctx.store.root).issues() == []
    assert ctx.count("tickets") == 0
    assert list(ctx.store.ledger("denial"))[0].detail["tool"] == "create_issue"


async def test_an_ordinary_envelope_goes_to_the_default_project(triage):
    ctx, tools = triage
    envelope = make_envelope(ctx)
    result = await _file(tools, envelope_id=envelope.id)
    body = result["structuredContent"]
    assert body["project"] == DEFAULT_PROJECT
    assert body["restricted"] is False
    assert body["severity"] == "critical"  # taken from the envelope
    assert body["envelope_id"] == envelope.id


# -- keys, logging, and the rest of the surface ----------------------------


async def test_issue_keys_are_monotonic_and_stable_across_store_instances(ctx, make_ctx):
    tools = handlers(build_tools(ctx))
    first = (await _file(tools, title="First"))["structuredContent"]["key"]
    second = (await _file(tools, title="Second"))["structuredContent"]["key"]
    assert (first, second) == (f"{DEFAULT_PROJECT}-1", f"{DEFAULT_PROJECT}-2")

    # A new run against the same state root continues the sequence rather than
    # restarting it — a reused key would overwrite a live ticket.
    later = make_ctx("TRIAGE", root=ctx.store.root)
    later_tools = handlers(build_tools(later))
    third = (await _file(later_tools, title="Third"))["structuredContent"]["key"]
    assert third == f"{DEFAULT_PROJECT}-3"

    # The sequence is shared with the restricted project, so keys stay unique.
    envelope = make_envelope(later, domain="security", impact={"security_relevant": True})
    fourth = (await _file(later_tools, envelope_id=envelope.id))["structuredContent"]["key"]
    assert fourth == f"{SECURITY_PROJECT}-4"

    assert [i.key for i in LocalTracker(ctx.store.root).issues()] == [first, second, third, fourth]


async def test_every_create_is_logged(triage):
    ctx, tools = triage
    key = (await _file(tools))["structuredContent"]["key"]
    created = [e for e in ctx.store.ledger("ticket") if e.detail["action"] == "created"]
    assert len(created) == 1
    assert created[0].agent == "TRIAGE"
    assert created[0].detail["key"] == key
    assert created[0].detail["project"] == DEFAULT_PROJECT


async def test_transition_refuses_an_unknown_key_without_dying(triage, make_ctx):
    ctx, _ = triage
    verifier = make_ctx("VERIFIER", root=ctx.store.root)
    tools = handlers(build_tools(verifier))
    result = await tools["transition"]({"key": "CORVID-999", "status": "closed"})
    assert is_error(result)
    assert "no issue 'CORVID-999'" in text_of(result)


async def test_transition_refuses_an_unknown_status_with_the_valid_list(triage, make_ctx):
    ctx, tools = triage
    key = (await _file(tools))["structuredContent"]["key"]
    verifier = make_ctx("VERIFIER", root=ctx.store.root)
    result = await handlers(build_tools(verifier))["transition"]({"key": key, "status": "donezo"})
    assert is_error(result)
    assert "in_review" in text_of(result)  # the message names the alternatives


async def test_link_relates_two_issues_and_refuses_dangling_targets(triage):
    ctx, tools = triage
    a = (await _file(tools, title="Original"))["structuredContent"]["key"]
    b = (await _file(tools, title="Recurrence"))["structuredContent"]["key"]

    linked = await tools["link"]({"key": b, "to": a, "type": "regression-of"})
    assert not is_error(linked)
    assert linked["structuredContent"]["links"] == [{"type": "regression-of", "to": a}]

    dangling = await tools["link"]({"key": b, "to": "CORVID-404", "type": "duplicates"})
    assert is_error(dangling)


async def test_link_is_refused_for_an_agent_with_no_tracker_write_access(make_ctx):
    ctx = make_ctx("API")
    result = await handlers(build_tools(ctx))["link"]({"key": "CORVID-1", "to": "CORVID-2"})
    assert is_error(result)
    assert "a link is a write" in text_of(result)


async def test_search_finds_filed_issues_and_is_open_to_read_only_agents(triage, make_ctx):
    ctx, tools = triage
    await _file(tools, title="Refund endpoint accepts any authenticated user")
    await tools["create_issue"](
        {"title": "Order list ignores the page size parameter", "body": "GET /v1/orders returns every row."}
    )

    api = make_ctx("API", root=ctx.store.root)
    conduit_tools = handlers(build_tools(api))

    hits = await conduit_tools["search"]({"text": "refund"})
    assert not is_error(hits)
    assert hits["structuredContent"]["count"] == 1

    everything = await conduit_tools["search"]({"project": DEFAULT_PROJECT})
    assert everything["structuredContent"]["count"] == 2

    nothing = await conduit_tools["search"]({"text": "websocket"})
    assert nothing["structuredContent"]["issues"] == []


# -- the adapter seam -------------------------------------------------------


def test_the_backend_is_chosen_by_config(tmp_path):
    assert isinstance(build_tracker("local", tmp_path), LocalTracker)
    with pytest.raises(ValueError, match="unknown tracker backend"):
        build_tracker("bugzilla", tmp_path)


def test_the_jira_adapter_refuses_to_start_unconfigured(monkeypatch):
    """A misconfigured tracker must fail at startup, not after twenty findings.

    The behaviour of the Jira adapter itself lives in
    tests/adapters/test_jira_tracker.py; this asserts only that the seam still
    fails loudly and names every variable it needs.
    """
    for name in (*JiraTracker.REQUIRED_ENV, JiraTracker.SECURITY_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(TrackerConfigError) as exc:
        build_tracker("jira", "/tmp")
    message = str(exc.value)
    for name in JiraTracker.REQUIRED_ENV:
        assert name in message


async def test_filing_stamps_the_ticket_key_onto_the_envelope(triage):
    """The two loops meet at the ticket, and this write is the junction.

    Remediation selects work by `envelope.jira.key`. Without the stamp a run
    files tickets and then verifies nothing — which is exactly what a full-loop
    run did before this was fixed.
    """
    ctx, tools = triage
    envelope = make_envelope(ctx)

    result = await tools["create_issue"]({
        "title": "Refund endpoint has no role check",
        "body": BODY,
        "envelope_id": envelope.id,
    })
    assert not is_error(result), text_of(result)
    key = result["structuredContent"]["key"]

    stamped = ctx.store.get_envelope(envelope.id)
    assert stamped.jira.key == key
    assert stamped.jira.project
    assert [e for e in ctx.store.envelopes() if e.jira.key], "remediation can now find it"


async def test_filing_without_an_envelope_stamps_nothing(triage):
    ctx, tools = triage
    before = [e.id for e in ctx.store.envelopes()]
    result = await tools["create_issue"]({"title": "Ad-hoc issue", "body": BODY})
    assert not is_error(result)
    assert [e.id for e in ctx.store.envelopes()] == before
