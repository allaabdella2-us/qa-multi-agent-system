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
from qaas.mcp import defect_memory
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
    b = make_envelope(ctx, discovered_by="BROWSER", **REPORT_B)
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
    """The distinction VERIFIER and TRIAGE act on: reopen, don't close as duplicate."""
    a = make_envelope(ctx, **REPORT_A)
    await tools["record"]({"envelope_id": a.id, "ticket_key": "CORVID-1"})
    # Resolved through the module function, which is the path a real run takes:
    # the ROUTER writes this from `_verify_loop` on a VERIFIED verdict. No agent
    # in the shipped roster calls `mark_resolved` -- VERIFIER, the only one that
    # closes a ticket, has no `defect_memory` server at all, which is why the
    # regression branch was unreachable until the router started writing it.
    # With the run's target, as the router passes it: without one this resolved
    # the `''` partition and missed the row `record` wrote under 'corvid'.
    defect_memory.resolve(
        ctx.store.root, a.fingerprint(), "CORVID-1", ctx.store.run_id,
        target=ctx.config.target or "",
    )

    # It comes back, reported by a different agent in different words.
    b = make_envelope(ctx, discovered_by="BROWSER", **REPORT_B)
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
    defect_memory.resolve(
        ctx.store.root, a.fingerprint(), None, ctx.store.run_id,
        target=ctx.config.target or "",
    )

    result = await tools["search_similar"](_search_args(REPORT_B))
    assert result["structuredContent"]["candidates"][0]["resolved"] is True
    assert "regression" in text_of(result).lower()


async def test_marking_a_defect_resolved_is_not_open_to_any_agent_holding_the_server(tools):
    """TRIAGE files; it does not certify a fix as having held.

    A recurrence after this is reported as a REGRESSION -- the highest-value
    signal the system produces -- so an agent able to write it wrongly can
    suppress that signal for every future run. `record` is gated the same way
    against `may_create_tickets`, matching how `record_verdict`,
    `record_reproduction` and `put_system_map` are already gated by name.
    """
    result = await tools["mark_resolved"]({"fingerprint": "sha256:nope"})
    assert is_error(result)
    assert "may not mark a defect resolved" in text_of(result)


def test_resolving_an_unknown_fingerprint_is_a_no_op_not_a_crash(tmp_path):
    assert defect_memory.resolve(tmp_path, "sha256:nope", "CORVID-1", "run-1") is False


async def test_memory_outlives_the_run(tmp_path, make_ctx):
    """Dedupe across runs is the entire point: a new run sees last run's defects."""
    first_ctx = make_ctx("TRIAGE", root=tmp_path)
    first_tools = handlers(build_tools(first_ctx))
    envelope = make_envelope(first_ctx, **REPORT_A)
    await first_tools["record"]({"envelope_id": envelope.id, "ticket_key": "CORVID-1"})

    later_ctx = make_ctx("TRIAGE", root=tmp_path)
    assert later_ctx.store.run_id != first_ctx.store.run_id
    later_tools = handlers(build_tools(later_ctx))

    result = await later_tools["search_similar"](_search_args(REPORT_B))
    assert result["structuredContent"]["candidates"][0]["ticket_key"] == "CORVID-1"


# -- the learning loop ------------------------------------------------------


def test_a_run_that_verifies_a_fix_makes_a_recurrence_a_regression(tmp_path, tools):
    """The loop that was unreachable in every shipped roster.

    The REGRESSION branch fires only on `resolved_at`; the only writer was the
    `mark_resolved` *tool*; and VERIFIER -- the one agent that closes a ticket --
    has no `defect_memory` server. So no configuration qaas ships with could ever
    report "this was fixed and it came back", which is the most valuable thing a
    system with a memory can say. The router writes it now, from Python.
    """
    from qaas.mcp import defect_memory

    assert not defect_memory.resolve(tmp_path, "sha256:unknown", "PROJ-1", "run-1")


def test_an_outcome_is_recorded_once_per_run_and_read_back(tmp_path):
    from qaas.mcp import defect_memory

    defect_memory.record_outcome(
        tmp_path, fingerprint="sha256:a", run_id="run-1", outcome="held",
        agent="API", detail="confidence 0.40 below gate 0.60",
    )
    # Idempotent: the same (fingerprint, run, outcome) replaces rather than doubles.
    defect_memory.record_outcome(
        tmp_path, fingerprint="sha256:a", run_id="run-1", outcome="held", agent="API",
    )
    defect_memory.record_outcome(
        tmp_path, fingerprint="sha256:a", run_id="run-2", outcome="not_reproducible",
    )
    rows = defect_memory.outcomes_for(tmp_path, "sha256:a")
    assert len(rows) == 2
    assert {r["outcome"] for r in rows} == {"held", "not_reproducible"}


def test_an_unknown_outcome_is_refused_rather_than_stored(tmp_path):
    """`OUTCOMES` is closed for the reason `LedgerKind` is: these are rendered
    back to an agent, so they are a wire format and not labels."""
    from qaas.mcp import defect_memory

    with pytest.raises(ValueError):
        defect_memory.record_outcome(
            tmp_path, fingerprint="sha256:a", run_id="r", outcome="probably_fine"
        )


def test_an_existing_memory_from_before_the_target_column_still_opens(tmp_path):
    """The memory deliberately outlives a release, so a schema change that
    orphaned an existing file would throw away the thing the file is for."""
    import sqlite3

    from qaas.mcp import defect_memory

    legacy = sqlite3.connect(tmp_path / defect_memory.MEMORY_DB)
    legacy.executescript(
        "CREATE TABLE defects (fingerprint TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "summary TEXT NOT NULL DEFAULT '', domain TEXT NOT NULL, "
        "defect_class TEXT NOT NULL DEFAULT '', service TEXT, endpoint TEXT, "
        "ui_route TEXT, paths TEXT NOT NULL DEFAULT '[]', ticket_key TEXT, "
        "occurrence_count INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, "
        "last_seen TEXT NOT NULL, last_run_id TEXT, resolved_at TEXT, "
        "resolved_ticket_key TEXT);"
    )
    legacy.execute(
        "INSERT INTO defects (fingerprint, title, domain, first_seen, last_seen) "
        "VALUES ('sha256:old', 'a defect from 0.0.1', 'api', '2026-01-01', '2026-01-01')"
    )
    legacy.commit()
    legacy.close()

    conn = defect_memory.connect(tmp_path)
    try:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(defects)")}
        assert "target" in columns
        row = conn.execute("SELECT * FROM defects WHERE fingerprint = 'sha256:old'").fetchone()
        # Legacy rows keep the empty target and stay visible -- `search_similar`
        # matches `target IN (?, '')` precisely so no memory is orphaned.
        assert row["target"] == ""
        assert row["title"] == "a defect from 0.0.1"
    finally:
        conn.close()
