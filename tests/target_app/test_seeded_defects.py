"""M1 verification: the golden ledger tells the truth.

Every seeded defect must actually reproduce, and every planted non-defect must
actually be correct. If this file goes red, every score the system reports is
measured against a lie — so it runs against the real containers, not a mock.

    docker compose -f target-app/docker-compose.yml up -d
    pytest tests/target_app -m docker

THIS FILE IS THE MERGE-TIME GUARD FOR A FORGOTTEN RETIREMENT. A seeded defect
test that starts failing means the defect is gone — someone repaired it. That is
allowed, but it is only half the change: set `fixed_in: <ref>` on the entry in
`defects.yaml` and rewrite the test below to assert the *fixed* behaviour, in the
same commit. Skip that and the entry becomes a phantom miss that quietly
understates recall on every future run. Retiring the entry is a human's job at
merge — no agent's sandbox includes the ledger, deliberately, because an agent
that can retire an entry can raise its own recall without fixing anything.
"""

from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.docker

API = "http://localhost:8000"
LEDGER = Path(__file__).resolve().parents[2] / "target-app" / "defects.yaml"
PASSWORD = "password123"


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    """Returns (status, body). Errors are responses here, not exceptions.

    The body comes back as a raw string when it is not JSON, which is itself a
    finding worth being able to see: a 500 that returns a bare traceback is not
    even well-formed, let alone safe.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _decode(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read().decode())


def _decode(raw: str):
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def login(email: str) -> str:
    status, body = call("POST", "/v1/auth/login", body={"email": email, "password": PASSWORD})
    assert status == 200, f"login failed for {email}: {status} {body}"
    return body["access_token"]


@pytest.fixture(scope="module")
def tokens():
    return {
        "admin": login("admin@northwind.test"),
        "member": login("member@northwind.test"),
        "viewer": login("viewer@northwind.test"),
        "other_org": login("admin@contoso.test"),
    }


@pytest.fixture(scope="module")
def ledger():
    return yaml.safe_load(LEDGER.read_text())


def test_the_app_is_up():
    assert call("GET", "/v1/health") == (200, {"status": "ok"})


# -- the seeded defects -----------------------------------------------------


def test_api_01_limit_is_declared_and_ignored(tokens):
    status, body = call("GET", "/v1/orders?limit=5", tokens["member"])
    assert status == 200
    assert body["limit"] == 5, "the response claims the limit was honoured"
    assert len(body["items"]) > 5, (
        "but every row came back — unbounded result set. "
        "If this now returns <= 5, API-01 has been repaired: set `fixed_in: <ref>` "
        "on its entry in defects.yaml and rewrite this test to assert the bound, "
        "in the same commit. See this module's docstring."
    )


def test_api_02_order_detail_leaks_across_organizations(tokens):
    """Northwind ids are 1-30; Contoso's admin must not be able to read one."""
    status, body = call("GET", "/v1/orders/1", tokens["other_org"])
    assert status == 200, "expected the cross-tenant read to succeed, i.e. the defect is present"
    assert body["org_id"] == 1, "another organization's order was returned in full"


def test_api_03_create_accepts_client_supplied_status_and_total(tokens):
    # A fresh reference each run: `reference` is unique, so a fixed one turns
    # the second run of this file into a 500 that looks like a different defect.
    status, body = call(
        "POST", "/v1/orders", tokens["member"],
        {
            "reference": f"MASS-{uuid.uuid4().hex[:8]}",
            "status": "paid",
            "total_cents": 0,
            "items": [{"sku": "X", "description": "x", "quantity": 1, "unit_price_cents": 999_00}],
        },
    )
    assert status == 201
    assert body["status"] == "paid", "client set the status"
    assert body["total_cents"] == 0, "client set the total, ignoring the items"


def test_api_04_a_viewer_can_refund(tokens):
    paid = next(
        o for o in call("GET", "/v1/orders?status=paid", tokens["admin"])[1]["items"]
    )
    status, _ = call("POST", f"/v1/orders/{paid['id']}/refund", tokens["viewer"])
    assert status == 200, "a viewer refunded an order; the spec restricts this to admin"


def test_api_05_unhandled_errors_leak_a_stack_trace(tokens):
    """Force a 500 and read the body. Any traceback in it is the defect."""
    status, body = call("GET", "/v1/orders/999999999999999999999", tokens["member"])
    assert status == 500, f"expected an unhandled error, got {status}"
    blob = body if isinstance(body, str) else json.dumps(body)
    assert "Traceback" in blob, "the 500 body carries a stack trace"
    assert "sqlalchemy" in blob or "/app/" in blob, "including library and file paths"


def test_api_06_invoices_omit_the_currency_the_spec_requires(tokens):
    status, body = call("GET", "/v1/invoices", tokens["member"])
    assert status == 200
    assert body["items"], "need at least one invoice to check"
    assert "currency" not in body["items"][0], "spec marks currency required on Invoice"


def test_api_07_deleting_a_missing_order_returns_200(tokens):
    status, _ = call("DELETE", "/v1/orders/987654", tokens["admin"])
    assert status == 200, "spec declares 404 for an unknown id"


def test_api_08_error_shapes_are_inconsistent(tokens):
    _, orders_err = call("GET", "/v1/orders/987654", tokens["member"])
    _, auth_err = call("POST", "/v1/auth/login", body={"email": "nope@x.test", "password": "wrong"})
    assert "error" in orders_err, f"orders uses the enveloped shape: {orders_err}"
    assert "detail" in auth_err, f"auth uses FastAPI's default shape: {auth_err}"


def test_api_09_the_websocket_is_absent_from_the_published_contract():
    """Found by API rather than planted, then recorded in the ledger."""
    import yaml

    spec = yaml.safe_load((LEDGER.parent / "openapi.yaml").read_text())
    assert not any("stream" in path for path in spec["paths"]), (
        "the spec now documents the stream endpoint; update the ledger entry"
    )
    handler = LEDGER.parent / "api" / "app" / "routes" / "stream.py"
    assert handler.exists() and "websocket" in handler.read_text().lower(), (
        "the endpoint is implemented, which is what makes its absence from the spec a defect"
    )


# -- the planted non-defects ------------------------------------------------


def test_not_01_the_legacy_endpoint_is_correctly_gone(tokens):
    """410 on a deliberately removed endpoint is right. Flagging it is a false positive."""
    status, body = call("GET", "/v1/orders/legacy", tokens["member"])
    assert status == 410
    assert "error" in body, "and it uses the documented error envelope"


# -- things that must NOT be broken -----------------------------------------


def test_authentication_is_actually_required():
    status, _ = call("GET", "/v1/orders")
    assert status == 401


def test_order_listing_is_scoped_to_the_callers_organization(tokens):
    _, mine = call("GET", "/v1/orders", tokens["member"])
    _, theirs = call("GET", "/v1/orders", tokens["other_org"])
    assert {o["org_id"] for o in mine["items"]} == {1}
    assert {o["org_id"] for o in theirs["items"]} == {2}


def test_invoice_pagination_actually_works(tokens):
    """Invoices paginate correctly; only orders has the limit defect."""
    status, body = call("GET", "/v1/invoices?limit=2", tokens["member"])
    assert status == 200
    assert len(body["items"]) <= 2


def test_offset_works_on_orders_even_though_limit_does_not(tokens):
    _, page = call("GET", "/v1/orders", tokens["member"])
    _, skipped = call("GET", "/v1/orders?offset=5", tokens["member"])
    assert len(skipped["items"]) == len(page["items"]) - 5


# -- the ledger and the app agree -------------------------------------------


def test_every_phase_1_defect_in_the_ledger_has_a_test_here(ledger):
    """A seeded defect nobody verifies is a seeded defect that may not exist."""
    covered = {
        name.split("test_")[1].split("_")[0].upper() + "-" + name.split("_")[2]
        for name in globals()
        if name.startswith("test_api_") or name.startswith("test_ui_")
    }
    # A retired defect no longer needs a test that reproduces it -- there is
    # nothing left to reproduce. It keeps its entry so past scores stay
    # reproducible, but it drops out of this obligation.
    api_defects = {
        d["id"] for d in ledger["defects"]
        if d["domain"] == "api" and d["phase"] == 1 and not d.get("fixed_in")
    }
    assert api_defects <= covered, f"unverified seeded defects: {api_defects - covered}"


def test_a_retired_defect_names_the_ref_that_repaired_it(ledger):
    """`fixed_in: true` or an empty string would retire an entry while recording
    nothing about why, which is how a ledger stops being an audit trail."""
    for d in ledger["defects"]:
        ref = d.get("fixed_in")
        if ref is None:
            continue
        assert isinstance(ref, str) and ref.strip(), (
            f"{d['id']} is retired but `fixed_in` names no ref"
        )
