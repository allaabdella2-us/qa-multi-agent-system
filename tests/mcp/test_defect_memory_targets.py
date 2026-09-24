"""Defect memory is partitioned by target: in every read and every write.

The `target` column went in with `search_similar` scoped to it and nothing
else. The table stayed keyed by `fingerprint` alone, and the fingerprint does not
include the target, so target A recording `GET /v1/users` as SHOP-7 made a
different defect on the same endpoint in target B "looks new" to `search_similar`
and then "already tracked as SHOP-7. Add evidence there; do not file again" to
`record`. These tests pin the partition on each path, and the migration that
re-keys a memory written before it, because that memory deliberately outlives a
release.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import is_error, make_envelope, text_of

from qaas.mcp import defect_memory
from qaas.mcp.context import handlers
from qaas.mcp.defect_memory import MEMORY_DB, build_tools

# Two different defects on one endpoint. With only an endpoint for a location
# they fingerprint identically -- which is the collision the key has to survive.
USERS_LEAK = dict(
    title="User list returns other tenants' email addresses",
    summary="GET /v1/users includes rows from every tenant, so emails leak across accounts.",
    location={"endpoint": "GET /v1/users"},
)
USERS_PAGING = dict(
    title="User list ignores the page size parameter",
    summary="GET /v1/users returns every row whatever the limit, and large tenants time out.",
    location={"endpoint": "GET /v1/users"},
)


def _in(make_ctx, root, target: str | None, agent: str = "TRIAGE"):
    """A context and its tools, pointed at `target`, over one shared memory."""
    ctx = make_ctx(agent, root=root)
    ctx.config = ctx.config.model_copy(update={"target": target})
    return ctx, handlers(build_tools(ctx))


def _search(report: dict) -> dict:
    return {
        "title": report["title"],
        "summary": report["summary"],
        "domain": "api",
        "class": "bug",
        "endpoint": report["location"]["endpoint"],
    }


def _rows(root, table: str = "defects") -> list[dict]:
    conn = sqlite3.connect(root / MEMORY_DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY target, fingerprint")]
    finally:
        conn.close()


# -- the suppression ----------------------------------------------------------


async def test_another_targets_ticket_does_not_suppress_a_defect_here(tmp_path, make_ctx):
    """The reported bug, end to end."""
    shop_a, tools_a = _in(make_ctx, tmp_path, "shop-a")
    leak = make_envelope(shop_a, **USERS_LEAK)
    await tools_a["record"]({"envelope_id": leak.id, "ticket_key": "SHOP-7"})

    shop_b, tools_b = _in(make_ctx, tmp_path, "shop-b")
    paging = make_envelope(shop_b, **USERS_PAGING)
    assert paging.fingerprint() == leak.fingerprint(), "the collision this test needs"

    looked = await tools_b["search_similar"](_search(USERS_PAGING))
    assert looked["structuredContent"]["candidates"] == []
    assert "looks new" in text_of(looked)

    recorded = await tools_b["record"]({"envelope_id": paging.id})
    assert not is_error(recorded)
    body = recorded["structuredContent"]
    assert body["first_time"] is True
    assert body["occurrence_count"] == 1
    assert body["ticket_key"] is None
    assert body["regression"] is False
    assert "already tracked" not in text_of(recorded)
    assert "do not file again" not in text_of(recorded)
    assert "SHOP-7" not in text_of(recorded)

    # A second sighting in B counts against B's row, never A's ticket.
    again = await tools_b["record"]({"envelope_id": paging.id, "ticket_key": "SHOP-B-1"})
    assert again["structuredContent"]["occurrence_count"] == 2
    assert again["structuredContent"]["ticket_key"] == "SHOP-B-1"

    by_target = {r["target"]: r for r in _rows(tmp_path)}
    assert set(by_target) == {"shop-a", "shop-b"}
    assert by_target["shop-a"]["ticket_key"] == "SHOP-7"
    assert by_target["shop-a"]["occurrence_count"] == 1, "B's sightings leaked into A's count"


async def test_a_legacy_row_is_a_labelled_candidate_and_never_a_suppression(tmp_path, make_ctx):
    """`''` rows predate the column. They may be anyone's: shown, never obeyed."""
    legacy_ctx, legacy_tools = _in(make_ctx, tmp_path, None)
    old = make_envelope(legacy_ctx, **USERS_LEAK)
    await legacy_tools["record"]({"envelope_id": old.id, "ticket_key": "SHOP-7"})
    assert _rows(tmp_path)[0]["target"] == ""

    shop, tools = _in(make_ctx, tmp_path, "shop-b")
    looked = await tools["search_similar"](_search(USERS_PAGING))
    (candidate,) = looked["structuredContent"]["candidates"]
    assert candidate["legacy"] is True
    assert candidate["ticket_key"] == "SHOP-7"
    assert "recorded before targets were tracked" in text_of(looked)

    paging = make_envelope(shop, **USERS_PAGING)
    recorded = await tools["record"]({"envelope_id": paging.id})
    body = recorded["structuredContent"]
    assert body["first_time"] is True
    assert body["occurrence_count"] == 1
    assert body["ticket_key"] is None, "a legacy ticket was adopted as this target's"
    assert "do not file again" not in text_of(recorded)
    # Named as a lead the agent can check against the tracker, not an instruction.
    assert body["legacy_match"]["ticket_key"] == "SHOP-7"
    assert "recorded before targets were tracked" in text_of(recorded)

    # Now this target has its own row, which supersedes the legacy one here...
    again = await tools["search_similar"](_search(USERS_PAGING))
    (mine,) = again["structuredContent"]["candidates"]
    assert mine["legacy"] is False
    assert mine["ticket_key"] is None

    # ...and the legacy row is exactly as it was, still the untargeted run's own.
    legacy_row = next(r for r in _rows(tmp_path) if r["target"] == "")
    assert legacy_row["occurrence_count"] == 1
    assert legacy_row["ticket_key"] == "SHOP-7"
    untargeted = await legacy_tools["record"]({"envelope_id": old.id})
    assert untargeted["structuredContent"]["occurrence_count"] == 2
    assert "already tracked as SHOP-7" in text_of(untargeted)


async def test_a_resolved_legacy_row_is_not_a_regression_in_a_named_target(tmp_path, make_ctx):
    """REGRESSION of another project's ticket is the same bug with a louder voice."""
    legacy_ctx, legacy_tools = _in(make_ctx, tmp_path, None)
    old = make_envelope(legacy_ctx, **USERS_LEAK)
    await legacy_tools["record"]({"envelope_id": old.id, "ticket_key": "SHOP-7"})
    assert defect_memory.resolve(tmp_path, old.fingerprint(), "SHOP-7", "run-0")

    shop, tools = _in(make_ctx, tmp_path, "shop-b")
    looked = await tools["search_similar"](_search(USERS_PAGING))
    assert "a recurrence is a regression" not in text_of(looked)

    paging = make_envelope(shop, **USERS_PAGING)
    recorded = await tools["record"]({"envelope_id": paging.id})
    assert recorded["structuredContent"]["regression"] is False
    assert recorded["structuredContent"]["legacy_match"]["resolved"] is True
    assert not list(shop.store.ledger("regression"))


# -- resolution ---------------------------------------------------------------


async def test_resolve_is_scoped_to_the_target(tmp_path, make_ctx):
    shop_a, tools_a = _in(make_ctx, tmp_path, "shop-a")
    shop_b, tools_b = _in(make_ctx, tmp_path, "shop-b")
    leak = make_envelope(shop_a, **USERS_LEAK)
    paging = make_envelope(shop_b, **USERS_PAGING)
    fp = leak.fingerprint()
    await tools_a["record"]({"envelope_id": leak.id, "ticket_key": "SHOP-7"})
    await tools_b["record"]({"envelope_id": paging.id, "ticket_key": "SHOP-B-1"})

    # What the router does on a VERIFIED in target A.
    assert defect_memory.resolve(tmp_path, fp, "SHOP-7", "run-a", target="shop-a") is True
    # A target that never saw it, and the untargeted partition, know nothing.
    assert defect_memory.resolve(tmp_path, fp, "X-1", "run-c", target="shop-c") is False
    assert defect_memory.resolve(tmp_path, fp, "X-1", "run-c") is False

    resolved = {r["target"]: r["resolved_at"] for r in _rows(tmp_path)}
    assert resolved["shop-a"] and resolved["shop-b"] is None

    # B's open defect comes back: an occurrence, not a regression of SHOP-7.
    b_again = await tools_b["record"]({"envelope_id": paging.id})
    assert b_again["structuredContent"]["regression"] is False
    assert "SHOP-7" not in text_of(b_again)
    # A's fixed defect comes back: that one *is* a regression.
    a_again = await tools_a["record"]({"envelope_id": leak.id})
    assert a_again["structuredContent"]["regression"] is True
    assert a_again["structuredContent"]["regression_of"] == "SHOP-7"


async def test_resolve_from_a_named_target_leaves_a_legacy_row_alone(tmp_path, make_ctx):
    legacy_ctx, legacy_tools = _in(make_ctx, tmp_path, None)
    old = make_envelope(legacy_ctx, **USERS_LEAK)
    await legacy_tools["record"]({"envelope_id": old.id, "ticket_key": "SHOP-7"})

    assert defect_memory.resolve(tmp_path, old.fingerprint(), "SHOP-B-1", "r", target="shop-b") is False
    assert _rows(tmp_path)[0]["resolved_at"] is None


async def test_mark_resolved_and_get_occurrences_are_scoped(tmp_path, make_ctx):
    shop_a, tools_a = _in(make_ctx, tmp_path, "shop-a")
    leak = make_envelope(shop_a, **USERS_LEAK)
    fp = leak.fingerprint()
    await tools_a["record"]({"envelope_id": leak.id, "ticket_key": "SHOP-7"})
    await tools_a["record"]({"envelope_id": leak.id})

    # VERIFIER is an agent the shipped policy lets transition tickets.
    _, verifier_b = _in(make_ctx, tmp_path, "shop-b", agent="VERIFIER")
    refused = await verifier_b["mark_resolved"]({"fingerprint": fp})
    assert is_error(refused)
    assert "not in defect memory for this target" in text_of(refused)
    assert _rows(tmp_path)[0]["resolved_at"] is None

    _, tools_b = _in(make_ctx, tmp_path, "shop-b")
    unknown = await tools_b["get_occurrences"]({"fingerprint": fp})
    assert is_error(unknown), "B was shown A's count and ticket as its own"

    _, verifier_a = _in(make_ctx, tmp_path, "shop-a", agent="VERIFIER")
    assert not is_error(await verifier_a["mark_resolved"]({"fingerprint": fp}))
    history = (await tools_a["get_occurrences"]({"fingerprint": fp}))["structuredContent"]
    assert history["occurrence_count"] == 2
    assert history["resolved"] is True
    assert history["legacy"] is False


async def test_get_occurrences_falls_back_to_a_labelled_legacy_row(tmp_path, make_ctx):
    """The same visibility `search_similar` has, so a candidate it showed can be
    looked up -- and said to be legacy when it is."""
    legacy_ctx, legacy_tools = _in(make_ctx, tmp_path, None)
    old = make_envelope(legacy_ctx, **USERS_LEAK)
    await legacy_tools["record"]({"envelope_id": old.id, "ticket_key": "SHOP-7"})

    _, tools = _in(make_ctx, tmp_path, "shop-b")
    result = await tools["get_occurrences"]({"fingerprint": old.fingerprint()})
    assert not is_error(result)
    assert result["structuredContent"]["legacy"] is True
    assert "recorded before targets were tracked" in text_of(result)


# -- outcomes -----------------------------------------------------------------


def test_outcomes_are_keyed_and_read_back_by_target(tmp_path):
    for target in ("shop-a", "shop-b"):
        defect_memory.record_outcome(
            tmp_path, fingerprint="sha256:u", run_id="run-1", outcome="held", target=target,
        )
    # Idempotent per (target, fingerprint, run, outcome): replaced, not doubled.
    defect_memory.record_outcome(
        tmp_path, fingerprint="sha256:u", run_id="run-1", outcome="held", target="shop-a",
        detail="second write",
    )
    assert len(_rows(tmp_path, "outcomes")) == 2

    a = defect_memory.outcomes_for(tmp_path, "sha256:u", target="shop-a")
    assert [(r["target"], r["detail"]) for r in a] == [("shop-a", "second write")]
    assert defect_memory.outcomes_for(tmp_path, "sha256:u", target="shop-c") == []
    assert defect_memory.outcomes_for(tmp_path, "sha256:u", target="") == []
    # Unscoped only on request, and then every row says whose it is.
    assert {r["target"] for r in defect_memory.outcomes_for(tmp_path, "sha256:u")} == {
        "shop-a", "shop-b",
    }


async def test_search_renders_only_this_targets_outcomes(tmp_path, make_ctx):
    """Another project's "could NOT be reproduced" beside this project's defect
    is a softer suppression by the same route as the hard one."""
    shop_b, tools_b = _in(make_ctx, tmp_path, "shop-b")
    paging = make_envelope(shop_b, **USERS_PAGING)
    fp = paging.fingerprint()
    await tools_b["record"]({"envelope_id": paging.id})

    defect_memory.record_outcome(
        tmp_path, fingerprint=fp, run_id="run-a", outcome="not_reproducible", target="shop-a",
    )
    defect_memory.record_outcome(tmp_path, fingerprint=fp, run_id="run-0", outcome="held")

    looked = await tools_b["search_similar"](_search(USERS_PAGING))
    assert looked["structuredContent"]["candidates"][0]["last_outcome"] is None
    assert "could NOT be reproduced" not in text_of(looked)
    assert "HELD" not in text_of(looked)

    defect_memory.record_outcome(
        tmp_path, fingerprint=fp, run_id="run-b", outcome="verified", target="shop-b",
    )
    looked = await tools_b["search_similar"](_search(USERS_PAGING))
    assert looked["structuredContent"]["candidates"][0]["last_outcome"]["outcome"] == "verified"
    assert "was verified" in text_of(looked)
