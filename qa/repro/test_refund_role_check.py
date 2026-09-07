"""Reproduction for finding 045ed281-8060-41b3-85a8-24161fd1d415.

POST /v1/orders/{order_id}/refund is documented in target-app/openapi.yaml as:

    summary: Refund a paid order
    description: Requires the admin role. Members and viewers must receive 403.

The handler (target-app/api/app/routes/orders.py:115-130) depends on
``CurrentUser`` -- merely authenticated -- rather than ``AdminUser``. The role
dependency exists and is correct (target-app/api/app/auth.py:98-113); it simply
has no call site, so no role is ever checked.

ENVIRONMENT THIS REPRODUCTION IS PINNED TO
    branch   main (target-app worktree unmodified)
    fixture  target-app/api/seed/fixtures.sql ("default")
    flags    none set, no clock override
    api      http://localhost:8000 (override with QAAS_API_URL)
    users    viewer@northwind.test / member@northwind.test / admin@northwind.test,
             all with password 'password123', all in org 1

Each test builds and removes its own order, so the suite is order-independent
and may be re-run against the same environment.

WHAT IS *NOT* BROKEN -- established by probing, and worth stating so the fix
stays narrow:
    * Authentication is enforced: no token -> 401.
    * Tenant isolation is enforced: viewer in org 1 refunding an org 2 order
      -> 404. See test_refund_still_enforces_tenant_isolation below.
Only the role check and the order-state guard are missing.
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


def _token(email):
    status, payload = _request(
        "POST", "/v1/auth/login", body={"email": email, "password": PASSWORD}
    )
    assert status == 200, f"fixture precondition: {email} could not sign in (got {status})"
    return payload["access_token"]


@pytest.fixture(scope="module")
def admin_token():
    return _token("admin@northwind.test")


@pytest.fixture(scope="module")
def viewer_token():
    return _token("viewer@northwind.test")


@pytest.fixture
def order(admin_token):
    """A fresh order created by an admin, removed afterwards.

    Created by the admin so the setup path uses only legitimate privileges --
    the test must fail because of the refund call, not because of how the
    order came to exist.
    """
    status, created = _request(
        "POST",
        "/v1/orders",
        token=admin_token,
        body={
            "reference": "QA-REPRO-045ED281",
            "currency": "USD",
            "items": [
                {
                    "sku": "SKU-REPRO-1",
                    "description": "repro fixture line",
                    "quantity": 1,
                    "unit_price_cents": 1000,
                }
            ],
        },
    )
    assert status == 201, f"fixture precondition: could not create an order (got {status})"
    yield created
    _request("DELETE", f"/v1/orders/{created['id']}", token=admin_token)


def test_refund_rejects_viewer_role(order, viewer_token):
    """A viewer must not be able to refund an order.

    openapi.yaml, POST /v1/orders/{order_id}/refund:
    "Requires the admin role. Members and viewers must receive 403."

    The role is checked by a dependency, which runs before the handler body,
    so the order's status is irrelevant to this contract.
    """
    status, _ = _request("POST", f"/v1/orders/{order['id']}/refund", token=viewer_token)

    assert status == 403, (
        f"openapi.yaml requires the admin role for this endpoint and states that "
        f"viewers must receive 403; the API returned {status}. The handler depends "
        f"on CurrentUser instead of AdminUser, so no role is checked."
    )


def test_refund_does_not_refund_an_unpaid_order(order, admin_token):
    """Refund must not apply to an order that was never paid.

    The endpoint is titled "Refund a paid order", but the handler assigns
    status='refunded' unconditionally. Asserted as an admin so that the missing
    role check cannot be what makes this test fail.
    """
    order_id = order["id"]
    assert order["status"] == "draft", (
        f"fixture precondition: expected a draft order, got {order['status']!r}"
    )

    status, payload = _request("POST", f"/v1/orders/{order_id}/refund", token=admin_token)

    refunded = status == 200 and payload is not None and payload.get("status") == "refunded"
    assert not refunded, (
        "a draft order that was never paid was transitioned straight to "
        "'refunded'; the handler sets status='refunded' with no check of the "
        "order's current state, so any order can be refunded, repeatedly."
    )


def test_refund_still_enforces_tenant_isolation(viewer_token):
    """Control: records what still works, so the fix stays narrow.

    Order 33 belongs to org 2; the viewer belongs to org 1. This passes today
    and must keep passing. If it ever fails, the defect is much larger than the
    missing role check.
    """
    status, _ = _request("POST", "/v1/orders/33/refund", token=viewer_token)

    assert status == 404, (
        f"cross-organization refund should not be visible to the caller; got {status}"
    )
