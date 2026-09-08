"""Contract diff: the change a consumer feels, and the test that proves it.

Two things are load-bearing here. First, breaking-ness is a fixed rule, not a
judgement call, so the same fixture must always produce the same verdicts.
Second, `generate_contract_test` emits *evidence* — a file that fails against a
violating server and passes against a conforming one. That claim is checked by
actually running the generated file against both, because a contract test that
cannot fail is worse than no test at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from conftest import REPO_ROOT, is_error, structured, text_of

from qaas.mcp.contract_diff import RULES, build, build_tools
from qaas.mcp.context import handlers

SPEC_A = """
openapi: 3.1.0
info: { title: Sample, version: "1.0.0" }
components:
  schemas:
    Thing:
      type: object
      required: [id, currency, status]
      properties:
        id: { type: integer }
        currency: { type: string }
        status: { type: string, enum: [open, paid, void] }
paths:
  /v1/things:
    get:
      operationId: listThings
      responses:
        "200":
          description: A page of things
          content:
            application/json:
              schema:
                type: object
                required: [items, total]
                properties:
                  items: { type: array, items: { $ref: "#/components/schemas/Thing" } }
                  total: { type: integer }
  /v1/legacy:
    get:
      operationId: legacyThings
      responses:
        "200":
          description: Gone in B
          content:
            application/json:
              schema: { $ref: "#/components/schemas/Thing" }
"""

# B differs from A in exactly four ways: /v1/legacy is gone, Thing lost the
# required `currency`, gained an optional `note`, and widened its status enum.
SPEC_B = """
openapi: 3.1.0
info: { title: Sample, version: "1.0.0" }
components:
  schemas:
    Thing:
      type: object
      required: [id, status]
      properties:
        id: { type: integer }
        note: { type: string }
        status: { type: string, enum: [open, paid, void, refunded] }
paths:
  /v1/things:
    get:
      operationId: listThings
      responses:
        "200":
          description: A page of things
          content:
            application/json:
              schema:
                type: object
                required: [items, total]
                properties:
                  items: { type: array, items: { $ref: "#/components/schemas/Thing" } }
                  total: { type: integer }
"""


@pytest.fixture
def tools(make_ctx):
    return handlers(build_tools(make_ctx("CONDUIT")))


@pytest.fixture
def specs(tmp_path):
    (tmp_path / "spec_a.yaml").write_text(SPEC_A)
    (tmp_path / "spec_b.yaml").write_text(SPEC_B)
    return tmp_path / "spec_a.yaml", tmp_path / "spec_b.yaml"


async def _diff(tools, a: Path, b: Path) -> list[dict]:
    result = await tools["diff_openapi"]({"spec_a": str(a), "spec_b": str(b)})
    assert not is_error(result), text_of(result)
    return structured(result)["changes"]


def test_the_server_exposes_the_tools_architecture_5_2_names(make_ctx):
    ctx = make_ctx("CONDUIT")
    assert [t.name for t in build_tools(ctx)] == [
        "diff_openapi", "classify_breaking", "find_consumers", "generate_contract_test",
    ]
    assert build(ctx)["name"] == "contract_diff"  # the name conduit.yaml allowlists


# -- the diff -------------------------------------------------------------


async def test_the_four_kinds_of_change_are_each_detected_once(tools, specs):
    changes = await _diff(tools, *specs)
    assert {(c["kind"], c["path"]) for c in changes} == {
        ("endpoint_removed", "/v1/legacy"),
        ("response_field_removed", "/v1/things"),
        ("response_field_added", "/v1/things"),
        ("response_enum_widened", "/v1/things"),
    }


async def test_only_the_changes_that_cost_a_consumer_something_are_breaking(tools, specs):
    """A removed endpoint and a removed field break callers; growth does not."""
    changes = await _diff(tools, *specs)
    assert {c["kind"] for c in changes if c["breaking"]} == {"endpoint_removed", "response_field_removed"}
    assert {c["kind"] for c in changes if not c["breaking"]} == {"response_field_added", "response_enum_widened"}


async def test_the_removed_field_is_named_by_its_consumer_visible_path(tools, specs):
    changes = await _diff(tools, *specs)
    removed = next(c for c in changes if c["kind"] == "response_field_removed")
    assert "items[].currency" in removed["detail"]
    assert "required" in removed["detail"]
    assert removed["method"] == "GET"


async def test_only_breaking_filters_the_result(tools, specs):
    a, b = specs
    result = await tools["diff_openapi"]({"spec_a": str(a), "spec_b": str(b), "only_breaking": True})
    assert {c["kind"] for c in structured(result)["changes"]} == {"endpoint_removed", "response_field_removed"}
    assert structured(result)["breaking_count"] == 2


async def test_identical_specs_produce_no_changes(tools, specs):
    a, _ = specs
    result = await tools["diff_openapi"]({"spec_a": str(a), "spec_b": str(a)})
    assert not is_error(result)
    assert structured(result)["changes"] == []


async def test_a_missing_spec_file_is_a_readable_refusal(tools, specs, tmp_path):
    a, _ = specs
    result = await tools["diff_openapi"]({"spec_a": str(a), "spec_b": str(tmp_path / "nope.yaml")})
    assert is_error(result)
    assert "nope.yaml" in text_of(result)


async def test_an_unreachable_app_says_so_and_offers_the_file_route(tools, specs, monkeypatch):
    """When the target app is not up, the agent must be told how to proceed."""
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", "http://127.0.0.1:1")
    result = await tools["diff_openapi"]({"spec_a": str(specs[0])})
    assert is_error(result)
    assert "spin_up" in text_of(result) and "file path" in text_of(result)


# -- API-06: the seeded defect this server exists to catch ------------------


async def test_a_missing_required_response_field_is_detected_against_the_real_spec(tools, tmp_path):
    """Seeded defect API-06: the Invoice response drops the spec-required `currency`."""
    spec = yaml.safe_load((REPO_ROOT / "target-app" / "openapi.yaml").read_text())
    invoice = spec["components"]["schemas"]["Invoice"]
    invoice["properties"].pop("currency")
    invoice["required"] = [f for f in invoice["required"] if f != "currency"]
    drifted = tmp_path / "implemented.yaml"
    drifted.write_text(yaml.safe_dump(spec))

    changes = await _diff(tools, REPO_ROOT / "target-app" / "openapi.yaml", drifted)
    hits = [c for c in changes if c["kind"] == "response_field_removed" and c["path"] == "/v1/invoices"]
    assert len(hits) == 1, changes
    assert "items[].currency" in hits[0]["detail"]
    assert hits[0]["breaking"] is True
    # And nothing else about that endpoint changed, so the finding is unambiguous.
    assert [c["kind"] for c in changes if c["path"] == "/v1/invoices"] == ["response_field_removed"]


# -- the shape of the comparison the agent will actually run ----------------

DECLARED = """
openapi: 3.1.0
info: { title: Corvid, version: "1.0.0" }
components:
  securitySchemes:
    bearerAuth: { type: http, scheme: bearer }
  schemas:
    Invoice:
      type: object
      required: [id, number, currency]
      properties:
        id: { type: integer }
        number: { type: string }
        currency: { type: string }
security:
  - bearerAuth: []
paths:
  /v1/invoices:
    get:
      operationId: listInvoices
      parameters:
        - { name: limit, in: query, schema: { type: integer } }
      responses:
        "200":
          description: Invoices
          content:
            application/json:
              schema:
                type: object
                required: [items, total]
                properties:
                  items: { type: array, items: { $ref: "#/components/schemas/Invoice" } }
                  total: { type: integer }
        "401": { description: Unauthorized }
"""

# What FastAPI actually emits: security declared per operation rather than
# globally, a 422 it adds for validated query parameters, and `anyOf` for a
# nullable column. None of those are drift; the missing `currency` is.
IMPLEMENTED = """
openapi: 3.1.0
info: { title: Corvid Orders API, version: "1.0.0" }
components:
  securitySchemes:
    HTTPBearer: { type: http, scheme: bearer }
  schemas:
    Invoice:
      type: object
      required: [id, number]
      properties:
        id: { type: integer }
        number: { anyOf: [{ type: string }, { type: "null" }] }
paths:
  /v1/invoices:
    get:
      operationId: list_invoices_v1_invoices_get
      security:
        - HTTPBearer: []
      parameters:
        - { name: limit, in: query, required: false, schema: { type: integer, maximum: 100, minimum: 1, default: 25 } }
      responses:
        "200":
          description: Successful Response
          content:
            application/json:
              schema:
                type: object
                required: [items, total]
                properties:
                  items: { type: array, items: { $ref: "#/components/schemas/Invoice" } }
                  total: { type: integer }
        "401": { description: Unauthorized }
        "422": { description: Validation Error }
"""


async def test_fastapi_conventions_are_not_reported_as_drift(tools, tmp_path):
    """The live spec spells auth and validation differently. Only real loss is breaking."""
    (tmp_path / "declared.yaml").write_text(DECLARED)
    (tmp_path / "implemented.yaml").write_text(IMPLEMENTED)
    changes = await _diff(tools, tmp_path / "declared.yaml", tmp_path / "implemented.yaml")

    assert not [c for c in changes if c["kind"].startswith("security")]  # both require a bearer token
    assert [c["kind"] for c in changes if c["breaking"]] == ["response_field_removed"]
    kinds = {c["kind"] for c in changes}
    assert "status_code_added" in kinds  # FastAPI's 422
    assert "response_type_widened" in kinds  # anyOf null on `number`


# -- classification --------------------------------------------------------


@pytest.mark.parametrize(
    "kind,verdict",
    [
        ("endpoint_removed", "breaking"),
        ("response_field_removed", "breaking"),
        ("response_required_dropped", "breaking"),
        ("response_type_narrowed", "breaking"),
        ("request_field_added_required", "breaking"),
        ("status_code_removed", "breaking"),
        ("response_field_added", "non_breaking"),
        ("response_enum_widened", "non_breaking"),
        ("endpoint_added", "non_breaking"),
        ("parameter_added_optional", "non_breaking"),
    ],
)
async def test_classify_breaking_applies_the_consumer_impact_rules(tools, kind, verdict):
    result = await tools["classify_breaking"]({"change": {"kind": kind, "path": "/v1/things", "method": "GET"}})
    assert not is_error(result)
    assert structured(result)["verdict"] == verdict
    assert structured(result)["breaking"] is (verdict == "breaking")
    assert structured(result)["reason"]


async def test_classification_agrees_with_the_diff_for_every_change_it_emits(tools, specs):
    """The two tools read one table; this is the test that keeps it that way."""
    for change in await _diff(tools, *specs):
        result = await tools["classify_breaking"]({"change": change})
        assert structured(result)["breaking"] is change["breaking"], change


async def test_an_unrecognised_change_is_unknown_rather_than_a_guess(tools):
    result = await tools["classify_breaking"]({"change": {"kind": "vibes_changed"}})
    assert not is_error(result)
    assert structured(result)["verdict"] == "unknown"


async def test_a_change_without_a_kind_is_refused_with_the_vocabulary(tools):
    result = await tools["classify_breaking"]({"change": {"detail": "something moved"}})
    assert is_error(result)
    assert "endpoint_removed" in text_of(result)


def test_every_rule_verdict_is_one_of_the_three_words():
    assert {v for v, _ in RULES.values()} <= {"breaking", "non_breaking"}
    assert all(reason.endswith(".") for _, reason in RULES.values())


# -- consumers -------------------------------------------------------------


async def test_find_consumers_reports_file_and_line_and_skips_vendor_trees(make_ctx, tmp_path):
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "web" / "src" / "api.ts").write_text(
        'export const listOrders = () => fetch(`${BASE}/v1/orders?limit=25`);\n'
        'export const getOrder = (id: number) => fetch(`${BASE}/v1/orders/${id}`);\n'
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text('fetch("/v1/orders")\n')
    (tmp_path / "notes.txt").write_text("/v1/orders is called from the web app\n")

    tools = handlers(build_tools(make_ctx("CONDUIT", repo_root=tmp_path)))
    result = await tools["find_consumers"]({"endpoint": "GET /v1/orders"})
    hits = structured(result)["consumers"]

    assert {(h["file"], h["line"]) for h in hits} == {("web/src/api.ts", 1), ("web/src/api.ts", 2)}
    assert not any("node_modules" in h["file"] for h in hits)  # vendored code is not a consumer
    assert not any(h["file"].endswith(".txt") for h in hits)  # prose is not a call site


async def test_find_consumers_says_plainly_when_it_found_nothing(make_ctx, tmp_path):
    tools = handlers(build_tools(make_ctx("CONDUIT", repo_root=tmp_path)))
    result = await tools["find_consumers"]({"endpoint": "/v1/nothing"})
    assert not is_error(result)
    assert structured(result)["consumers"] == []
    assert "weak evidence" in text_of(result)


async def test_find_consumers_rejects_something_that_is_not_a_path(tools):
    result = await tools["find_consumers"]({"endpoint": "orders"})
    assert is_error(result)


# -- generated contract tests ----------------------------------------------


async def test_generated_test_is_written_under_the_run_store_and_is_valid_python(make_ctx):
    ctx = make_ctx("CONDUIT")
    tools = handlers(build_tools(ctx))
    result = await tools["generate_contract_test"](
        {"endpoint": "/v1/invoices", "method": "GET", "expectation": "currency is missing from Invoice"}
    )
    assert not is_error(result), text_of(result)

    payload = structured(result)
    path = Path(payload["path"])
    assert path.parent == ctx.store.root / "generated"
    assert path.exists()
    compile(path.read_text(), str(path), "exec")  # it must at least parse

    assert payload["expected_status"] == 200
    assert "currency" in payload["required_item_fields"]
    assert sorted(payload["required_fields"]) == ["items", "limit", "offset", "total"]
    assert payload["uri"].startswith("artifact://")  # citable as evidence
    assert "currency is missing" in payload["source"]  # the why survives into the file


async def test_generating_for_an_endpoint_the_spec_does_not_declare_is_refused(tools):
    result = await tools["generate_contract_test"]({"endpoint": "/v1/nope", "method": "GET"})
    assert is_error(result)
    assert "/v1/invoices" in text_of(result)  # it says what it does know


async def test_a_parameterised_path_is_refused_rather_than_faked(tools):
    result = await tools["generate_contract_test"]({"endpoint": "/v1/orders/{order_id}", "method": "GET"})
    assert is_error(result)
    assert "path parameters" in text_of(result)


# -- the generated test must actually discriminate --------------------------

_INVOICE = {
    "id": 1, "order_id": 1, "number": "INV-1001", "amount_cents": 12000,
    "currency": "USD", "issued_at": "2026-01-01T00:00:00Z", "status": "open",
}


def _stub_server(conforming: bool) -> tuple[ThreadingHTTPServer, int]:
    """A two-line Corvid: conforming, or missing `currency` exactly as API-06 does."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            if not self.path.startswith("/v1/invoices"):
                return self._send(404, {"error": {"code": "not_found", "message": "no"}})
            if "Authorization" not in self.headers:
                return self._send(401, {"error": {"code": "unauthorized", "message": "no token"}})
            invoice = dict(_INVOICE)
            if not conforming:
                invoice.pop("currency")
            self._send(200, {"items": [invoice], "total": 1, "limit": 25, "offset": 0})

        def _send(self, status: int, body: dict) -> None:
            blob = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args) -> None:  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _run_generated(test_file: Path, port: int, cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "QAAS_TARGET_BASE_URL": f"http://127.0.0.1:{port}",
        "QAAS_TARGET_TOKEN": "stub-token",  # skip the login round-trip
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=180, cwd=str(cwd), env=env,
    )


async def test_the_generated_test_passes_against_a_conforming_server_and_fails_against_a_violating_one(
    make_ctx, tmp_path
):
    """The whole point of generating it: it has to be able to fail."""
    tools = handlers(build_tools(make_ctx("CONDUIT")))
    result = await tools["generate_contract_test"]({"endpoint": "/v1/invoices", "method": "GET"})
    assert not is_error(result), text_of(result)

    # Run it outside the repo so this project's pytest addopts do not apply.
    workspace = tmp_path / "generated-run"
    workspace.mkdir()
    test_file = workspace / "test_generated_contract.py"
    test_file.write_text(structured(result)["source"])

    good, good_port = _stub_server(conforming=True)
    try:
        passing = _run_generated(test_file, good_port, workspace)
    finally:
        good.shutdown()
    assert passing.returncode == 0, textwrap.indent(passing.stdout + passing.stderr, "  ")

    bad, bad_port = _stub_server(conforming=False)
    try:
        failing = _run_generated(test_file, bad_port, workspace)
    finally:
        bad.shutdown()
    assert failing.returncode != 0, "a contract test that cannot fail is not evidence"
    assert "currency" in failing.stdout
