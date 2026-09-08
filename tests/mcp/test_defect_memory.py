"""Defect memory: the dedupe property the whole ticket-precision story rests on.

If two agents describing one defect in different words are not matched, the
system files duplicate storms and the team stops reading it (§10). These tests
pin that behaviour and its opposite — that two genuinely different defects are
left alone.
"""

from __future__ import annotations

import pytest
from conftest import is_error, make_envelope, text_of

from qaas.mcp.context import handlers
from qaas.mcp.defect_memory import MIN_SIMILARITY, build_tools, connect

# Same defect, two agents, no shared phrasing beyond the domain vocabulary.
REPORT_A = dict(
    title="Refund endpoint accepts any authenticated user",
    summary=(
        "POST /v1/refunds does not check the caller's role, so any logged-in account "
        "can refund an order belonging to someone else."
    ),
    location={"endpoint": "POST /v1/refunds", "paths": ["src/api/refunds.py"]},
)
REPORT_B = dict(
    title="Missing authorization on the refund handler",
    summary=(
        "Anyone with a session token may issue a chargeback against another person's "
        "purchase; the handler never consults the permission table."
    ),
    location={"endpoint": "POST /v1/refunds", "paths": ["src/api/refunds.py:42"]},
)
# A different defect entirely, in the same domain.
REPORT_C = dict(
    title="Order list ignores the page size parameter",
    summary=(
        "GET /v1/orders returns every row regardless of the limit query parameter, so "
        "a large tenant times the request out."
    ),
    location={"endpoint": "GET /v1/orders", "paths": ["src/api/orders.py"]},
)


@pytest.fixture
def tools(ctx):
    return handlers(build_tools(ctx))


def _search_args(report: dict, **extra) -> dict:
    return {
        "title": report["title"],
        "summary": report["summary"],
        "domain": "api",
        "endpoint": report["location"].get("endpoint"),
        "paths": report["location"].get("paths", []),
        **extra,
    }


async def test_the_same_defect_worded_differently_is_found_similar(ctx, tools):
    """The load-bearing property: wording differs, the defect does not."""
    first = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": first.id, "ticket_key": "CORVID-1"})

    result = await tools["search_similar"](_search_args(REPORT_B))
    assert not is_error(result)

    candidates = result["structuredContent"]["candidates"]
    assert len(candidates) == 1
    match = candidates[0]
    assert match["ticket_key"] == "CORVID-1"
    assert match["occurrence_count"] == 1
    assert match["last_seen"]
    assert match["fingerprint"] == first.fingerprint()
    assert match["similarity"] >= MIN_SIMILARITY
    assert "CORVID-1" in text_of(result)

    # Supplying the class lets the exact structural fingerprint match too, which
    # should only ever raise the score.
    with_class = await tools["search_similar"](_search_args(REPORT_B, **{"class": "bug"}))
    assert with_class["structuredContent"]["candidates"][0]["similarity"] > match["similarity"]


async def test_similarity_is_symmetric_and_deterministic(ctx, tools):
    """Two agents asking a minute apart must get the same answer, both ways round."""
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id})
    forward = await tools["search_similar"](_search_args(REPORT_B))
    again = await tools["search_similar"](_search_args(REPORT_B))
    assert (
        forward["structuredContent"]["candidates"][0]["similarity"]
        == again["structuredContent"]["candidates"][0]["similarity"]
    )


async def test_two_different_defects_are_not_found_similar(ctx, tools):
    """Same domain, same service, different defect — must not collapse together."""
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-1"})

    result = await tools["search_similar"](_search_args(REPORT_C, **{"class": "bug"}))
    assert not is_error(result)
    assert result["structuredContent"]["candidates"] == []
    assert result["structuredContent"]["searched"] == 1
    assert "looks new" in text_of(result)


async def test_a_different_domain_defect_is_not_found_similar(ctx, tools):
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id})

    result = await tools["search_similar"]({
        "title": "Checkout review screen loses the promo code on back navigation",
        "summary": "Going back from payment to review silently clears the applied discount.",
        "domain": "frontend",
        "ui_route": "/checkout/review",
        "paths": ["web/src/checkout/Review.tsx"],
    })
    assert result["structuredContent"]["candidates"] == []


async def test_fingerprint_reuses_the_envelope_hash(ctx, tools):
    envelope = make_envelope(ctx, **REPORT_A)
    result = await tools["fingerprint"]({"envelope_id": envelope.id})
    assert result["structuredContent"]["fingerprint"] == envelope.fingerprint()


async def test_fingerprint_of_an_unknown_envelope_is_a_readable_refusal(tools):
    result = await tools["fingerprint"]({"envelope_id": "not-a-real-id"})
    assert is_error(result)
    assert "Emit it first" in text_of(result)


async def test_a_second_record_increments_rather_than_duplicating(ctx, tools):
    """Two agents finding one defect must leave one row, not two."""
    a = make_envelope(ctx, **REPORT_A)
    b = make_envelope(ctx, discovered_by="SURFACE", **REPORT_B)
    assert a.fingerprint() == b.fingerprint()  # prose differs, structure does not

    first = await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-1"})
    assert first["structuredContent"]["occurrence_count"] == 1
    assert first["structuredContent"]["first_time"] is True

    second = await tools["record"]({"envelope_id": b.id})
    assert second["structuredContent"]["occurrence_count"] == 2
    assert second["structuredContent"]["first_time"] is False
    assert second["structuredContent"]["ticket_key"] == "CORVID-1"
    assert "do not file again" in text_of(second)

    conn = connect(ctx.store.root)
    try:
        rows = conn.execute("SELECT * FROM defects").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2


async def test_get_occurrences_reports_the_history(ctx, tools):
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-7"})
    await tools["record"]({"envelope_id": a.id})

    result = await tools["get_occurrences"]({"fingerprint": a.fingerprint()})
    body = result["structuredContent"]
    assert body["occurrence_count"] == 2
    assert body["ticket_key"] == "CORVID-7"
    assert body["first_seen"] <= body["last_seen"]
    assert body["resolved"] is False


async def test_get_occurrences_of_an_unknown_fingerprint_refuses(tools):
    result = await tools["get_occurrences"]({"fingerprint": "sha256:nope"})
    assert is_error(result)
    assert "not in defect memory" in text_of(result)


async def test_a_recurrence_after_resolution_is_a_regression_not_a_duplicate(ctx, tools):
    """The distinction PROOF and CLERK act on: reopen, don't close as duplicate."""
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-1"})
    await tools["mark_resolved"]({"fingerprint": a.fingerprint(), "ticket_key": "CORVID-1"})

    # It comes back, reported by a different agent in different words.
    b = make_envelope(ctx, discovered_by="SURFACE", **REPORT_B)
    result = await tools["record"]({"envelope_id": b.id})

    body = result["structuredContent"]
    assert body["regression"] is True
    assert body["regression_of"] == "CORVID-1"
    assert body["occurrence_count"] == 2
    assert "REGRESSION" in text_of(result)
    assert "regression of CORVID-1" in text_of(result)

    assert [e.detail["fingerprint"] for e in ctx.store.ledger("regression")] == [a.fingerprint()]

    # And it is open again, so the next recurrence is an ordinary occurrence.
    assert (await tools["get_occurrences"]({"fingerprint": a.fingerprint()}))["structuredContent"]["resolved"] is False


async def test_search_flags_a_resolved_match_as_a_regression_risk(ctx, tools):
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-1"})
    await tools["mark_resolved"]({"fingerprint": a.fingerprint()})

    result = await tools["search_similar"](_search_args(REPORT_B))
    assert result["structuredContent"]["candidates"][0]["resolved"] is True
    assert "regression" in text_of(result).lower()


async def test_mark_resolved_on_an_unknown_fingerprint_refuses(tools):
    result = await tools["mark_resolved"]({"fingerprint": "sha256:nope"})
    assert is_error(result)
    assert "nothing to resolve" in text_of(result)


async def test_memory_outlives_the_run(tmp_path, make_ctx):
    """Dedupe across runs is the entire point: a new run sees last run's defects."""
    first_ctx = make_ctx("CLERK", root=tmp_path)
    first_tools = handlers(build_tools(first_ctx))
    envelope = make_envelope(first_ctx, **REPORT_A)
    await first_tools["record"]({"envelope_id": envelope.id, "ticket_key": "CORVID-1"})

    later_ctx = make_ctx("CLERK", root=tmp_path)
    assert later_ctx.store.run_id != first_ctx.store.run_id
    later_tools = handlers(build_tools(later_ctx))

    result = await later_tools["search_similar"](_search_args(REPORT_B))
    assert result["structuredContent"]["candidates"][0]["ticket_key"] == "CORVID-1"
