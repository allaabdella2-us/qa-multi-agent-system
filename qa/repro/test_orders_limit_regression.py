"""Regression cover for CORVID-7: GET /v1/orders must apply its `limit`.

The defining reproduction (test_orders_limit_ignored.py) proves the single
reported symptom: ?limit=1 returned all 30 rows. This module states the
*contract* that was violated, so the clause cannot be dropped again in a
different shape:

    A page of GET /v1/orders holds exactly min(limit, total - offset) items,
    for every limit the schema accepts and every offset -- and paging with a
    fixed limit yields every order exactly once.

That is deliberately wider than the reproduction. The reproduction pins one
point (limit=1); a handler could satisfy it and still be wrong at limit=5, at a
non-zero offset, or past the end of the collection. Each test below fails
against the unfixed handler, which chained only .offset(offset).

ENVIRONMENT
    fixture  target-app/api/seed/fixtures.sql ("default"); org 1 holds 30 orders
    user     viewer@northwind.test / 'password123' -- lowest-privilege seeded user
    api      http://localhost:8000 (override with QAAS_API_URL)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE_URL = os.environ.get("QAAS_API_URL", "http://localhost:8000").rstrip("/")
PASSWORD = "password123"


def _request(method, path, token=None, body=None):
    """Return (status_code, parsed_json_or_none). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return exc.code, None
    except urllib.error.URLError as exc:
        pytest.fail(
            f"Could not reach the target app at {BASE_URL} ({exc}). "
            "Start it with env_control.spin_up before running this regression."
        )


@pytest.fixture(scope="module")
def viewer_token():
    status, payload = _request(
        "POST",
        "/v1/auth/login",
        body={"email": "viewer@northwind.test", "password": PASSWORD},
    )
    assert status == 200, (
        f"fixture precondition: viewer@northwind.test could not sign in (got {status})"
    )
    return payload["access_token"]


@pytest.fixture(scope="module")
def total_orders(viewer_token):
    status, payload = _request("GET", "/v1/orders?limit=100", token=viewer_token)
    assert status == 200, f"fixture precondition: expected 200, got {status}"
    total = payload["total"]
    assert total >= 6, (
        f"fixture precondition: these regressions need at least 6 orders in org 1 to "
        f"exercise limits below the collection size, but total={total}. Re-seed 'default'."
    )
    return total


@pytest.mark.parametrize("limit", [1, 2, 5, 25])
def test_page_holds_exactly_the_requested_limit(viewer_token, total_orders, limit):
    """The page size is the requested limit whenever enough rows exist.

    Parameterised because the reproduction pins only limit=1. A handler that
    special-cased the reproduced value -- or applied a hardcoded cap -- would
    satisfy that one point and fail here.
    """
    status, payload = _request("GET", f"/v1/orders?limit={limit}", token=viewer_token)
    assert status == 200, f"expected 200, got {status}"

    expected = min(limit, total_orders)
    assert len(payload["items"]) == expected, (
        f"GET /v1/orders?limit={limit} returned {len(payload['items'])} items; "
        f"expected exactly {expected} with total={total_orders}. The handler must "
        f"chain .limit(limit) onto the row query, as invoices.py does."
    )
    assert payload["limit"] == limit, (
        f"the response echoed limit={payload['limit']} for a request of limit={limit}"
    )


def test_limit_composes_with_offset(viewer_token, total_orders):
    """limit must bound the page at a non-zero offset too.

    `offset` already worked while `limit` did not, so the two clauses have to be
    checked together: applying limit only when offset is zero would pass the
    reproduction and still corrupt every page after the first.
    """
    limit = 5
    status, payload = _request(
        "GET", f"/v1/orders?limit={limit}&offset=3", token=viewer_token
    )
    assert status == 200, f"expected 200, got {status}"

    expected = min(limit, max(0, total_orders - 3))
    assert len(payload["items"]) == expected, (
        f"GET /v1/orders?limit={limit}&offset=3 returned {len(payload['items'])} "
        f"items; expected {expected}. limit is not applied alongside offset."
    )


def test_paging_yields_every_order_exactly_once(viewer_token, total_orders):
    """The property a paginating client actually depends on.

    Walks the whole collection in fixed-size pages and checks the pages tile it:
    no duplicates, no gaps, and page arithmetic against `total` holds. This is
    what was broken in the field -- every page returned the entire table.
    """
    limit = 4
    seen: list[int] = []
    for offset in range(0, total_orders, limit):
        status, payload = _request(
            "GET", f"/v1/orders?limit={limit}&offset={offset}", token=viewer_token
        )
        assert status == 200, f"expected 200 at offset={offset}, got {status}"
        assert len(payload["items"]) <= limit, (
            f"page at offset={offset} held {len(payload['items'])} items, "
            f"more than the requested limit of {limit}"
        )
        seen.extend(item["id"] for item in payload["items"])

    assert len(seen) == total_orders, (
        f"paging in {limit}-row pages yielded {len(seen)} rows for a collection of "
        f"{total_orders}; the pages do not tile the collection"
    )
    assert len(set(seen)) == len(seen), (
        "the same order id appeared on more than one page; pages overlap"
    )


def test_offset_past_the_end_returns_an_empty_page(viewer_token, total_orders):
    """The far boundary: past the end is empty, not the whole table."""
    status, payload = _request(
        "GET", f"/v1/orders?limit=5&offset={total_orders + 10}", token=viewer_token
    )
    assert status == 200, f"expected 200, got {status}"
    assert payload["items"] == [], (
        f"an offset past the end returned {len(payload['items'])} items; expected none"
    )
    assert payload["total"] == total_orders, (
        "`total` must keep counting the whole collection, not the page"
    )
