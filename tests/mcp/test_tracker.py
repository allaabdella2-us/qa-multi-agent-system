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
def clerk(ctx):
    return ctx, handlers(build_tools(ctx))


def _file(tools, title: str = "Refund endpoint accepts any authenticated user", **extra):
    return tools["create_issue"]({"title": title, "body": BODY, **extra})


# -- §8.1 write permission matrix -----------------------------------------


async def test_a_read_only_agent_may_not_create_issues(make_ctx):
    """CONDUIT is a discovery agent: it emits envelopes, CLERK files them."""
    ctx = make_ctx("CONDUIT")
    assert ctx.agent.policy.read_only
    tools = handlers(build_tools(ctx))

    result = await _file(tools)
    assert is_error(result)
    assert "may not create tickets" in text_of(result)
    assert ctx.count("tickets") == 0

    denials = list(ctx.store.ledger("denial"))
    assert [d.detail["tool"] for d in denials] == ["create_issue"]
    assert denials[0].agent == "CONDUIT"

    # And nothing reached the tracker.
    assert LocalTracker(ctx.store.root).issues() == []


async def test_a_read_only_agent_may_not_transition_issues(clerk, make_ctx):
    ctx, tools = clerk
    filed = await _file(tools)
    key = filed["structuredContent"]["key"]

    conduit = make_ctx("CONDUIT", root=ctx.store.root)
    conduit_tools = handlers(build_tools(conduit))
    result = await conduit_tools["transition"]({"key": key, "status": "closed"})
    assert is_error(result)
    assert "may not transition tickets" in text_of(result)


async def test_proof_may_transition_but_not_create(clerk, make_ctx):
    """§8.1: PROOF is the closing authority, never the filing one."""
    ctx, tools = clerk
    key = (await _file(tools))["structuredContent"]["key"]

    proof = make_ctx("PROOF", root=ctx.store.root)
    proof_tools = handlers(build_tools(proof))

    assert is_error(await _file(proof_tools))

    moved = await proof_tools["transition"](
        {"key": key, "status": "closed", "comment": "verified: test_refund_authz now passes"}
    )
    assert not is_error(moved)
    assert moved["structuredContent"]["status"] == "closed"

    logged = [e for e in proof.store.ledger("ticket") if e.detail["action"] == "transitioned"]
    assert logged[0].detail["key"] == key
    assert logged[0].agent == "PROOF"


# -- §4.12 per-run rate limit ----------------------------------------------


async def test_clerk_files_up_to_the_cap_then_escalates(clerk):
    ctx, tools = clerk
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
    assert escalations[0].agent == "CLERK"
    assert escalations[0].detail["cap"] == cap
    assert escalations[0].detail["reason"] == "ticket cap reached"

    # The refusal is a refusal: the cap is not silently exceeded.
    assert ctx.count("tickets") == cap
    assert len(LocalTracker(ctx.store.root).issues()) == cap


# -- §10 security routing ---------------------------------------------------


async def test_a_security_relevant_envelope_routes_to_the_restricted_project(clerk):
    ctx, tools = clerk
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


async def test_a_vulnerability_class_envelope_routes_to_the_restricted_project(clerk):
    """The class alone is enough; the reporter need not also tick the flag."""
    ctx, tools = clerk
    envelope = make_envelope(ctx, domain="security", **{"class": "vulnerability"})
    result = await _file(tools, envelope_id=envelope.id)
    assert result["structuredContent"]["project"] == SECURITY_PROJECT


async def test_asking_for_a_public_project_for_a_security_finding_is_refused(clerk):
    ctx, tools = clerk
    envelope = make_envelope(ctx, domain="security", impact={"security_relevant": True})

    result = await _file(tools, envelope_id=envelope.id, project=DEFAULT_PROJECT)
    assert is_error(result)
    assert DEFAULT_PROJECT in text_of(result)
    assert SECURITY_PROJECT in text_of(result)
    assert "never go to a public project" in text_of(result)

    assert LocalTracker(ctx.store.root).issues() == []
    assert ctx.count("tickets") == 0
    assert list(ctx.store.ledger("denial"))[0].detail["tool"] == "create_issue"


async def test_an_ordinary_envelope_goes_to_the_default_project(clerk):
    ctx, tools = clerk
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
    later = make_ctx("CLERK", root=ctx.store.root)
    later_tools = handlers(build_tools(later))
    third = (await _file(later_tools, title="Third"))["structuredContent"]["key"]
    assert third == f"{DEFAULT_PROJECT}-3"

    # The sequence is shared with the restricted project, so keys stay unique.
    envelope = make_envelope(later, domain="security", impact={"security_relevant": True})
    fourth = (await _file(later_tools, envelope_id=envelope.id))["structuredContent"]["key"]
    assert fourth == f"{SECURITY_PROJECT}-4"

    assert [i.key for i in LocalTracker(ctx.store.root).issues()] == [first, second, third, fourth]


async def test_every_create_is_logged(clerk):
    ctx, tools = clerk
    key = (await _file(tools))["structuredContent"]["key"]
    created = [e for e in ctx.store.ledger("ticket") if e.detail["action"] == "created"]
    assert len(created) == 1
    assert created[0].agent == "CLERK"
    assert created[0].detail["key"] == key
    assert created[0].detail["project"] == DEFAULT_PROJECT


async def test_transition_refuses_an_unknown_key_without_dying(clerk, make_ctx):
    ctx, _ = clerk
    proof = make_ctx("PROOF", root=ctx.store.root)
    tools = handlers(build_tools(proof))
    result = await tools["transition"]({"key": "CORVID-999", "status": "closed"})
    assert is_error(result)
    assert "no issue 'CORVID-999'" in text_of(result)


async def test_transition_refuses_an_unknown_status_with_the_valid_list(clerk, make_ctx):
    ctx, tools = clerk
    key = (await _file(tools))["structuredContent"]["key"]
    proof = make_ctx("PROOF", root=ctx.store.root)
    result = await handlers(build_tools(proof))["transition"]({"key": key, "status": "donezo"})
    assert is_error(result)
    assert "in_review" in text_of(result)  # the message names the alternatives


async def test_link_relates_two_issues_and_refuses_dangling_targets(clerk):
    ctx, tools = clerk
    a = (await _file(tools, title="Original"))["structuredContent"]["key"]
    b = (await _file(tools, title="Recurrence"))["structuredContent"]["key"]

    linked = await tools["link"]({"key": b, "to": a, "type": "regression-of"})
    assert not is_error(linked)
    assert linked["structuredContent"]["links"] == [{"type": "regression-of", "to": a}]

    dangling = await tools["link"]({"key": b, "to": "CORVID-404", "type": "duplicates"})
    assert is_error(dangling)


async def test_link_is_refused_for_an_agent_with_no_tracker_write_access(make_ctx):
    ctx = make_ctx("CONDUIT")
    result = await handlers(build_tools(ctx))["link"]({"key": "CORVID-1", "to": "CORVID-2"})
    assert is_error(result)
    assert "a link is a write" in text_of(result)


async def test_search_finds_filed_issues_and_is_open_to_read_only_agents(clerk, make_ctx):
    ctx, tools = clerk
    await _file(tools, title="Refund endpoint accepts any authenticated user")
    await tools["create_issue"](
        {"title": "Order list ignores the page size parameter", "body": "GET /v1/orders returns every row."}
    )

    conduit = make_ctx("CONDUIT", root=ctx.store.root)
    conduit_tools = handlers(build_tools(conduit))

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


async def test_filing_stamps_the_ticket_key_onto_the_envelope(clerk):
    """The two loops meet at the ticket, and this write is the junction.

    Remediation selects work by `envelope.jira.key`. Without the stamp a run
    files tickets and then verifies nothing — which is exactly what a full-loop
    run did before this was fixed.
    """
    ctx, tools = clerk
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


async def test_filing_without_an_envelope_stamps_nothing(clerk):
    ctx, tools = clerk
    before = [e.id for e in ctx.store.envelopes()]
    result = await tools["create_issue"]({"title": "Ad-hoc issue", "body": BODY})
    assert not is_error(result)
    assert [e.id for e in ctx.store.envelopes()] == before
