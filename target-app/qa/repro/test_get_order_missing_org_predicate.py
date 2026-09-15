"""Reproduction for finding a4f5987f-1b88-448b-8ce6-0a0b257de0ff.

GET /v1/orders/{order_id} resolves an order by primary key alone, with no
organization predicate, so any authenticated user of any organization receives
the full body of any order by id.

The handler's own summary is "Fetch one order from the caller's organization",
so the intent is not in question. api/app/routes/orders.py:98 does:

    select(Order).where(Order.id == order_id)

Every sibling handler in the same file carries the tenant predicate --
list_orders (:44), delete_order (:110) and refund_order (:122) each add
`Order.org_id == user.org_id`. The detail handler is the lone exception, so
this is one missing predicate rather than a missing layer.

DUPLICATE NOTICE -- READ BEFORE OPENING A TICKET
------------------------------------------------
This is the same root cause, same line, as reproductions already filed under
findings e4399b89, d3eb5c50 and b405211e. a4f5987f is the fourth report of one
defect. Prior artifacts:

    qa/repro/test_get_order_cross_tenant_read.py
        on branch qa/repro/e4399b89-cross-tenant-order-read
    qa/repro/test_orders_tenant_isolation.py
        on branch qa/repro/b405211e-cross-tenant-order-read

One predicate closes all four. They should be merged, not worked in parallel.

What minimising established, by measurement
-------------------------------------------
  * The shortest path is two HTTP calls -- log in, then one GET. No prior
    request, no state setup, no flag, no clock.
  * Authentication IS enforced: the same GET with no token returns 401. Only
    the tenant scope is missing, which is why the endpoint looks healthy to an
    unauthenticated scan.
  * It is NOT role-gated. The caller used here is `viewer`, the lowest-privilege
    role in the fixture -- not the `admin` named on the report. A fix built on
    role checks would not close this.
  * The not-found path works: id 999, which exists in no organization, returns
    404. The defect is precisely the missing predicate, not broken lookup or
    error handling.
  * The id is incidental. Order 35 leaks to the same caller identically; 33 is
    named only because it is the id on the report.
  * The scoping layer demonstrably exists elsewhere: POST /v1/orders/33/refund
    as the same caller returns 404, because refund_order carries the predicate
    this handler omits. That contrast is the tightest available evidence that
    this is an omission in one query.

Environment this reproduces against
------------------------------------
    branch   qa/repro/a4f5987f-cross-tenant-order-read (api/ identical to main)
    fixture  default (api/seed/fixtures.sql), freshly reset
    flags    none set; no clock override
    caller   viewer@northwind.test -- org_id=1, role `viewer`
    target   order 33 -- 'CT-2003', org_id=2 (Contoso), paid, 4050 EUR

These tests drive the running API over HTTP exactly as a browser would, so they
need the stack up (api on :8000) and the default fixture loaded. Nothing here is
time-dependent and nothing mutates state, so test order does not matter and no
reset is required between them.

Expected once the defect is fixed: test_viewer_cannot_read_another_orgs_order
returns 404. The other three pass today and must keep passing -- they guard
against a fix that over-corrects into 404-ing or 403-ing reads the caller is
entitled to.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("CORVID_BASE_URL", "http://localhost:8000")
PASSWORD = os.environ.get("CORVID_PASSWORD", "password123")

# Fixture facts (api/seed/fixtures.sql): org 1 is Northwind, org 2 is Contoso.
NORTHWIND_VIEWER = "viewer@northwind.test"  # org_id=1, lowest-privilege role
CONTOSO_ORDER_ID = 33  # 'CT-2003', org_id=2 -- the order named in the report
NORTHWIND_ORDER_ID = 1  # 'NW-1001', org_id=1 -- the caller's own order
UNUSED_ORDER_ID = 999  # exists in no organization


def _request(method: str, path: str, token: str | None = None, body: dict | None = None):
    """Return (status, parsed_body). Does not raise on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE_URL}{path}", method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return exc.code, raw.decode(errors="replace")


def _login(email: str) -> str:
    status, body = _request(
        "POST", "/v1/auth/login", body={"email": email, "password": PASSWORD}
    )
    assert status == 200, (
        f"fixture precondition: {email} must be able to log in -- got {status} {body}"
    )
    return body["access_token"]


def _own_order_ids(token: str) -> set[int]:
    """Ids the caller's own organization exposes through the org-scoped list endpoint."""
    status, body = _request("GET", "/v1/orders?limit=100", token=token)
    assert status == 200, (
        f"precondition: GET /v1/orders must succeed -- got {status} {body}"
    )
    return {item["id"] for item in body["items"]}


def test_viewer_cannot_read_another_orgs_order():
    """THE DEFECT. A read-only user in org 1 must not receive org 2's order 33.

    This is the whole reproduction: log in, one GET. Today it returns 200 with
    the reference, status, monetary total, currency and line items of an order
    belonging to a different customer.
    """
    token = _login(NORTHWIND_VIEWER)

    # Precondition proved rather than assumed: the org-scoped list endpoint
    # agrees this order is not the caller's, whatever the fixture ids are.
    assert CONTOSO_ORDER_ID not in _own_order_ids(token), (
        f"precondition: order {CONTOSO_ORDER_ID} must be outside the caller's "
        "organization for this to be a cross-tenant read"
    )

    status, body = _request("GET", f"/v1/orders/{CONTOSO_ORDER_ID}", token=token)

    assert status == 404, (
        "an order outside the caller's organization must return 404, never its "
        f"contents -- got {status} with body {body}"
    )


def test_the_scoped_sibling_handler_refuses_the_same_order():
    """Boundary: the tenant predicate exists elsewhere, so this is one omission.

    Passes today. refund_order (orders.py:122) carries `Order.org_id ==
    user.org_id` and correctly 404s on the very order the detail handler hands
    over. Recorded because it rules out "the scoping layer is missing" and
    pins the defect to the single `where` clause at orders.py:98.

    This test does not mutate anything: the refund is refused before it applies.
    """
    token = _login(NORTHWIND_VIEWER)

    status, body = _request("POST", f"/v1/orders/{CONTOSO_ORDER_ID}/refund", token=token)

    assert status == 404, (
        "the org-scoped sibling handler must refuse another org's order -- "
        f"got {status} {body}"
    )


def test_the_same_read_without_a_token_is_rejected():
    """Boundary: authentication is enforced, so only the tenant scope is missing.

    Passes today. Recorded because it locates the defect precisely -- the
    endpoint is not open to the world, it is open to every logged-in customer,
    which is why an unauthenticated scan would call it healthy.
    """
    status, body = _request("GET", f"/v1/orders/{CONTOSO_ORDER_ID}")

    assert status == 401, (
        f"an anonymous caller must not reach this order -- got {status} {body}"
    )


def test_the_caller_can_still_read_their_own_order():
    """Control. Passes today and must keep passing: the fix must scope, not forbid.

    Fails if a fix closes the leak by rejecting the detail read outright, or by
    gating it on a role the reporting user does not hold.
    """
    token = _login(NORTHWIND_VIEWER)

    assert NORTHWIND_ORDER_ID in _own_order_ids(token), (
        f"precondition: order {NORTHWIND_ORDER_ID} must belong to the caller's organization"
    )

    status, body = _request("GET", f"/v1/orders/{NORTHWIND_ORDER_ID}", token=token)

    assert status == 200, f"a caller must still read their own order -- got {status} {body}"
    assert body["id"] == NORTHWIND_ORDER_ID
