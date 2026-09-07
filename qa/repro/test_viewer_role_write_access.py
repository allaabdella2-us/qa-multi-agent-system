"""Reproduction for finding 2108631b — "viewer role can create and place orders".

Environment this reproduces against (pin all five):
  branch : qa/repro/2108631b-viewer-write-access (target-app unmodified from main)
  fixture: default (target-app/api/seed/fixtures.sql), 2 orgs / 4 users / 35 orders
  flags  : {} (none set)
  clock  : not overridden; no behaviour here is time-dependent
  role   : viewer@northwind.test (org 1, role 'viewer'), password 'password123'

What this file settles
----------------------
The finding claims that because the viewer role can create an order, a
"read-only" role has write access. The published contract does not agree, and
the contract is authoritative: openapi.yaml's own preamble says "This document
is the contract. Where the implementation disagrees with it, the implementation
is wrong until a spec change says otherwise."

  * createOrder (openapi.yaml, POST /v1/orders) documents 201 / 401 / 422 and
    no 403, and names no role restriction.
  * The ONLY role rule in the whole document is on refundOrder:
    "Requires the admin role. Members and viewers must receive 403."
  * No product surface — API description, UI copy, fixtures — describes the
    viewer role as read-only. The name is the only thing suggesting it.

So the two tests below encode the contract as written, and they disagree about
where the defect actually is. That disagreement is the point of this file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

BASE_URL = os.environ.get("CORVID_API_URL", "http://localhost:8000")
PASSWORD = "password123"  # fixtures.sql: "Every account signs in with the password 'password123'."


def _call(method, path, token=None, body=None):
    """Return (status, decoded_json_or_None). HTTP errors are returned, not raised."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method)
    if token:
        request.add_header("authorization", f"Bearer {token}")
    if data:
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return error.code, None


def _token(email):
    status, payload = _call(
        "POST", "/v1/auth/login", body={"email": email, "password": PASSWORD}
    )
    assert status == 200, f"fixture login for {email} failed with HTTP {status}"
    return payload["access_token"]


@pytest.fixture
def viewer_token():
    return _token("viewer@northwind.test")


@pytest.fixture
def admin_token():
    return _token("admin@northwind.test")


@pytest.fixture
def order_owned_by_org(admin_token):
    """An order created by an admin, removed afterwards.

    Created per-test so nothing here mutates the seeded fixture rows and the
    file can be run repeatedly without drifting.
    """
    status, payload = _call(
        "POST",
        "/v1/orders",
        token=admin_token,
        body={
            "reference": "FORGE-2108631B",
            "items": [
                {
                    "sku": "SKU-TEST-1",
                    "description": "repro fixture line",
                    "quantity": 1,
                    "unit_price_cents": 100,
                }
            ],
        },
    )
    assert status == 201, f"admin could not create the fixture order (HTTP {status})"
    order_id = payload["id"]
    yield order_id
    _call("DELETE", f"/v1/orders/{order_id}", token=admin_token)


def test_viewer_creating_an_order_matches_the_published_contract(viewer_token):
    """POST /v1/orders is not role-restricted by the spec, so 201 is correct.

    This is the finding's claimed defect, asserted against the contract rather
    than against the role's name. It passes today because the behaviour is
    conformant. If the spec is ever changed to make viewer read-only, this test
    is the one that must be updated -- in the same commit as that spec change,
    never on its own.
    """
    status, payload = _call(
        "POST",
        "/v1/orders",
        token=viewer_token,
        body={
            "reference": "FORGE-2108631B-VIEWER",
            "items": [
                {
                    "sku": "SKU-TEST-1",
                    "description": "repro line",
                    "quantity": 1,
                    "unit_price_cents": 100,
                }
            ],
        },
    )
    assert status == 201, (
        "openapi.yaml createOrder documents 201/401/422 and imposes no role "
        f"restriction, so a viewer creating an order must succeed; got HTTP {status}"
    )
    if payload and payload.get("id"):
        _call("DELETE", f"/v1/orders/{payload['id']}", token=_token("admin@northwind.test"))


def test_viewer_refunding_an_order_is_forbidden(viewer_token, order_owned_by_org):
    """The one role rule the spec actually states -- and the one that is broken.

    openapi.yaml, POST /v1/orders/{order_id}/refund:
      "Requires the admin role. Members and viewers must receive 403."
    routes/orders.py::refund_order depends on CurrentUser (authentication only)
    rather than AdminUser, so no role check runs. auth.py already defines
    require_admin/AdminUser; this endpoint simply does not use it.
    """
    status, _ = _call(
        "POST", f"/v1/orders/{order_owned_by_org}/refund", token=viewer_token
    )
    assert status == 403, (
        "openapi.yaml refundOrder states 'Requires the admin role. Members and "
        f"viewers must receive 403.'; the viewer got HTTP {status}"
    )
