"""Reproduction for finding 61297327-bc12-470b-bbea-60262fc048d4.

GET /v1/orders accepts and validates a ``limit`` query parameter (ge=1, le=100,
default 25) and echoes it back in the response body, but never applies it to the
query. target-app/api/app/routes/orders.py:49-51 chains only ``.offset(offset)``:

    rows = db.scalars(
        stmt.order_by(Order.created_at.desc(), Order.id.desc()).offset(offset)
    ).all()

The sibling handler list_invoices (target-app/api/app/routes/invoices.py:34)
builds the same shape of query and does call ``.limit(limit).offset(offset)``,
which makes this a dropped clause rather than a deliberate design choice.

Consequence: the endpoint returns every order in the caller's organization on
every call while the response claims ``limit: 25``. A paginating client renders
far more rows than it asked for, and its page arithmetic against ``total`` is
wrong.

ENVIRONMENT THIS REPRODUCTION IS PINNED TO
    branch   qa/repro/61297327-orders-limit-ignored (target-app worktree unmodified)
    fixture  target-app/api/seed/fixtures.sql ("default")
    flags    none set, no clock override
    api      http://localhost:8000 (override with QAAS_API_URL)
    user     viewer@northwind.test / 'password123', org 1, role 'viewer'
    data     org 1 has 30 orders under the default fixture

MINIMISATION
    The reporter's path involved admin credentials and comparison against
    invoices. Neither is needed. The shortest sequence that shows the defect is
    one login plus one GET:

        GET /v1/orders?limit=1

    * No admin role -- the lowest-privilege seeded user (viewer) reproduces it,
      so this is not entangled with any authorization behaviour.
    * No setup and no mutation -- the test only reads, so it is order-independent
      and safe to re-run against the same environment without cleanup.
    * limit=1 is the tightest boundary the schema allows (ge=1) and needs only
      2 orders to be present, far below the 30 the fixture supplies.

WHAT IS *NOT* BROKEN -- established by probing, and recorded so the fix stays
narrow to adding ``.limit(limit)``:
    * ``offset`` IS applied: ?limit=1&offset=1 returned 29 of 30 rows.
    * The status filter IS applied: ?limit=1&status=paid returned 6 rows,
      total=6 -- the WHERE clause works, only LIMIT is missing.
    * Authentication is enforced: no token -> 401.
    * The sibling GET /v1/invoices honours limit correctly.
Controls for the first three are below and must keep passing.

KNOWN FLAKE RISK (environment, not the defect): during probing, one
POST /v1/auth/login returned 500 once and succeeded on every subsequent
attempt. Login is a precondition here, not the assertion under test, so such a
failure surfaces as a precondition error rather than a false reproduction.
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
            "Start it with env_control.spin_up before running this reproduction."
        )


@pytest.fixture(scope="module")
def viewer_token():
    """The lowest-privilege seeded user; the defect needs no more than this."""
    status, payload = _request(
        "POST",
        "/v1/auth/login",
        body={"email": "viewer@northwind.test", "password": PASSWORD},
    )
    assert status == 200, (
        f"fixture precondition: viewer@northwind.test could not sign in (got {status})"
    )
    return payload["access_token"]


def test_orders_list_respects_limit_parameter(viewer_token):
    """GET /v1/orders must return at most `limit` items.

    This is the defect. limit=1 is the tightest value the schema permits
    (Query(ge=1, le=100)), so a correct implementation returns exactly one row
    whenever the organization has any orders at all.
    """
    status, payload = _request("GET", "/v1/orders?limit=1", token=viewer_token)
    assert status == 200, f"fixture precondition: expected 200 from GET /v1/orders, got {status}"

    # Without more rows than the limit, the assertion below would pass vacuously.
    assert payload["total"] > 1, (
        f"fixture precondition: this reproduction needs at least 2 orders in org 1, "
        f"but total={payload['total']}. Re-seed the 'default' fixture."
    )

    assert len(payload["items"]) <= 1, (
        f"GET /v1/orders?limit=1 returned {len(payload['items'])} items while the "
        f"response body echoed limit={payload['limit']}. The handler validates and "
        f"echoes `limit` but never applies it: orders.py:49-51 chains only "
        f".offset(offset), with no .limit(limit). Compare invoices.py:34, which "
        f"correctly chains .limit(limit).offset(offset)."
    )


def test_orders_list_applies_its_documented_default_limit(viewer_token):
    """With no query parameters, the endpoint must apply its default limit of 25.

    Separate from the explicit-limit case because this is what a client that
    passes no pagination parameters at all experiences: the response advertises
    `limit: 25` while carrying every row in the organization.
    """
    status, payload = _request("GET", "/v1/orders", token=viewer_token)
    assert status == 200, f"fixture precondition: expected 200 from GET /v1/orders, got {status}"

    declared = payload["limit"]
    assert declared == 25, f"expected the schema default limit of 25, got {declared}"
    assert payload["total"] > declared, (
        f"fixture precondition: this reproduction needs more than {declared} orders "
        f"in org 1, but total={payload['total']}. Re-seed the 'default' fixture."
    )

    assert len(payload["items"]) <= declared, (
        f"GET /v1/orders with no parameters returned {len(payload['items'])} items "
        f"while the response body claims limit={declared}. A client paginating on "
        f"the echoed limit renders every order in the organization on page 1."
    )


def test_orders_list_still_applies_offset(viewer_token):
    """Control: `offset` works today and must keep working.

    Narrows the fix to a missing .limit(): if this ever fails, the pagination
    defect is larger than one dropped clause.
    """
    status, page = _request("GET", "/v1/orders?offset=1", token=viewer_token)
    assert status == 200, f"expected 200, got {status}"
    _, full = _request("GET", "/v1/orders?offset=0", token=viewer_token)

    assert len(page["items"]) == len(full["items"]) - 1, (
        "offset=1 should skip exactly one row relative to offset=0; it did not, "
        "so OFFSET is not being applied either."
    )


def test_orders_list_still_applies_status_filter(viewer_token):
    """Control: the WHERE clause works today and must keep working.

    Establishes that only the LIMIT clause is missing, not the whole query.
    """
    status, payload = _request("GET", "/v1/orders?status=paid", token=viewer_token)
    assert status == 200, f"expected 200, got {status}"

    assert payload["total"] > 0, "fixture precondition: expected at least one paid order in org 1"
    assert all(item["status"] == "paid" for item in payload["items"]), (
        "?status=paid returned orders in another status; the filter is not applied."
    )


def test_invoices_list_respects_limit_parameter(viewer_token):
    """Control: the sibling endpoint honours `limit`, and must keep doing so.

    This is the comparison that makes the orders defect a dropped clause rather
    than a design decision. It passes today.
    """
    status, payload = _request("GET", "/v1/invoices?limit=1", token=viewer_token)
    assert status == 200, f"expected 200, got {status}"

    assert payload["total"] > 1, (
        f"fixture precondition: this control needs at least 2 invoices, "
        f"but total={payload['total']}"
    )
    assert len(payload["items"]) <= 1, (
        f"GET /v1/invoices?limit=1 returned {len(payload['items'])} items. If this "
        f"control fails, the pagination defect is not confined to orders.py."
    )
