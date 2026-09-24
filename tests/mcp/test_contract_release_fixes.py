"""contract_diff regressions found before 0.0.2.

Redirects that leave the target, blocking calls on the event loop, and a
generated contract test that could report a login failure or a correct 403 as a
contract violation. Stub servers bind 127.0.0.1, so the default run stays
offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import is_error, structured, text_of

from qaas.mcp import contract_diff
from qaas.mcp.context import handlers
from qaas.mcp.contract_diff import build_tools

SPEC = """
openapi: 3.1.0
info: { title: Tiny, version: "1.0.0" }
paths:
  /v1/things:
    get:
      responses:
        "200": { description: ok }
"""

_INVOICE = {
    "id": 1, "order_id": 1, "number": "INV-1001", "amount_cents": 12000,
    "currency": "USD", "issued_at": "2026-01-01T00:00:00Z", "status": "open",
}


def _serve(handle) -> tuple[ThreadingHTTPServer, str, list[str]]:
    """A stub server; `handle(handler)` answers, and every path it saw is kept."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.append(self.path)
            handle(self)

        do_GET = do_POST = _answer  # noqa: N815 - BaseHTTPRequestHandler's contract

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


def _send(handler, status: int, body: object = None, **headers: str) -> None:
    blob = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    handler.send_response(status)
    for key, value in headers.items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(blob)))
    handler.end_headers()
    handler.wfile.write(blob)


@pytest.fixture
def servers():
    started: list[ThreadingHTTPServer] = []

    def _start(handle):
        server, base, seen = _serve(handle)
        started.append(server)
        return base, seen

    yield _start
    for server in started:
        server.shutdown()


# -- 4. a spec fetch stays on the application under test ---------------------


async def test_a_spec_redirect_to_another_origin_is_not_followed(make_ctx, tmp_path, servers, monkeypatch):
    """The origin check ran on the URL handed in; `urlopen` then followed a 302
    anywhere, and whatever answered there was diffed as the target's spec."""
    elsewhere, elsewhere_saw = servers(lambda h: _send(h, 200, SPEC.encode()))
    target, _ = servers(lambda h: _send(h, 302, Location=f"{elsewhere}/openapi.json"))
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", target)
    (tmp_path / "spec.yaml").write_text(SPEC)
    tools = handlers(build_tools(make_ctx("API", target_root=tmp_path)))

    result = await tools["diff_openapi"]({"spec_a": "spec.yaml", "spec_b": f"{target}/openapi.json"})
    assert is_error(result)
    assert "different origin" in text_of(result)
    assert elsewhere_saw == [], "the fetch left the application under test"


async def test_a_spec_redirect_on_the_same_origin_is_still_followed(make_ctx, tmp_path, servers, monkeypatch):
    def handle(h):
        if h.path == "/old.json":
            return _send(h, 301, Location="/openapi.json")
        _send(h, 200, SPEC.encode())

    target, seen = servers(handle)
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", target)
    (tmp_path / "spec.yaml").write_text(SPEC)
    tools = handlers(build_tools(make_ctx("API", target_root=tmp_path)))

    result = await tools["diff_openapi"]({"spec_a": "spec.yaml", "spec_b": f"{target}/old.json"})
    assert not is_error(result), text_of(result)
    assert seen == ["/old.json", "/openapi.json"]


# -- 7. no blocking call on the event loop -----------------------------------


async def test_the_spec_fetch_and_the_consumer_walk_run_off_the_event_loop(make_ctx, tmp_path, monkeypatch):
    """A synchronous `urlopen` and an `os.walk` over the repository both ran
    inline in async handlers, stalling every other agent's tool calls."""
    on_loop: dict[str, bool] = {}

    def fake_fetch(url):
        on_loop["fetch"] = threading.current_thread() is threading.main_thread()
        return None, "stubbed"

    def fake_walk(root):
        on_loop["walk"] = threading.current_thread() is threading.main_thread()
        return iter(())

    monkeypatch.setattr(contract_diff, "_fetch_spec", fake_fetch)
    monkeypatch.setattr(contract_diff, "_walk_sources", fake_walk)
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", "http://127.0.0.1:9")
    (tmp_path / "spec.yaml").write_text(SPEC)
    tools = handlers(build_tools(make_ctx("API", target_root=tmp_path)))

    await tools["diff_openapi"]({"spec_a": "spec.yaml", "spec_b": "http://127.0.0.1:9/openapi.json"})
    await tools["find_consumers"]({"endpoint": "/v1/things"})
    assert on_loop == {"fetch": False, "walk": False}


# -- 11. the generated test is evidence about the contract, not the harness --


def _target_with_its_own_names(anonymous_status: int):
    """Logs in with `username`/`pass`, answers `{"data": {"jwt": ...}}`, and
    refuses an anonymous caller with `anonymous_status`."""

    def handle(h):
        if h.command == "POST" and h.path == "/api/session":
            body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
            if body.get("username") == "qa-user" and body.get("pass") == "qa-pass":
                return _send(h, 200, {"data": {"jwt": "JWT-1"}})
            return _send(h, 422, {"detail": f"expected username/pass, got {sorted(body)}"})
        if h.path.startswith("/v1/invoices"):
            if h.headers.get("Authorization") != "Bearer JWT-1":
                return _send(h, anonymous_status, {"detail": "Not authenticated"})
            return _send(h, 200, {"items": [_INVOICE], "total": 1, "limit": 25, "offset": 0})
        _send(h, 404, {"detail": "no"})

    return handle


def _run_generated(
    source: str, base: str, workspace: Path, token: str | None = None
) -> subprocess.CompletedProcess:
    workspace.mkdir(exist_ok=True)
    test_file = workspace / "test_generated_contract.py"
    test_file.write_text(source)
    env = {k: v for k, v in os.environ.items() if k != "QAAS_TARGET_TOKEN"}
    env.update(QAAS_TARGET_BASE_URL=base, QAAS_TARGET_USER="qa-user", QAAS_TARGET_PASSWORD="qa-pass")
    if token:
        env["QAAS_TARGET_TOKEN"] = token  # skip the login round-trip
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, timeout=180, cwd=str(workspace), env=env,
    )


@pytest.fixture
def generated(make_ctx):
    """A contract test generated under a profile whose login is not
    `email`/`password` -> `access_token`."""

    async def _generate() -> str:
        ctx = make_ctx("API")
        profile = ctx.config.profile.model_copy(deep=True)
        profile.auth.login_endpoint = "POST /api/session"
        profile.auth.username_field = "username"
        profile.auth.password_field = "pass"
        profile.auth.token_path = "data.jwt"
        ctx.config = ctx.config.model_copy(update={"profile": profile})
        result = await handlers(build_tools(ctx))["generate_contract_test"](
            {"endpoint": "/v1/invoices", "method": "GET"}
        )
        assert not is_error(result), text_of(result)
        source = structured(result)["source"]
        assert "AUTH_REQUIRED = True" in source, "the fixture endpoint must be declared authenticated"
        return source

    return _generate


async def test_the_generated_login_uses_the_profiles_field_names_and_token_path(
    generated, servers, tmp_path
):
    """The body was hard-coded `email`/`password` and the token read from
    `access_token`, so against this API every generated test failed at login
    and the envelope cited that as a contract violation. It also has to pass a
    correct 403 -- FastAPI `HTTPBearer`'s default -- which the 401-only check
    reported as an anonymous-access bypass."""
    source = await generated()
    base, _ = servers(_target_with_its_own_names(anonymous_status=403))
    run = _run_generated(source, base, tmp_path / "run")
    assert run.returncode == 0, run.stdout + run.stderr


async def test_the_auth_check_still_fails_when_anonymous_callers_get_in(generated, servers, tmp_path):
    """Accepting 403 must not make the check vacuous."""
    source = await generated()
    base, _ = servers(_target_with_its_own_names(anonymous_status=200))
    run = _run_generated(source, base, tmp_path / "run")
    assert run.returncode != 0
    assert "declared as authenticated but answered an anonymous call with 200" in run.stdout


async def test_a_403_for_an_anonymous_caller_is_not_reported_as_a_bypass(make_ctx, servers, tmp_path):
    """The login skipped with a preset token, so this isolates the status check:
    401-only reported FastAPI `HTTPBearer`'s default 403 as anonymous access."""
    result = await handlers(build_tools(make_ctx("API")))["generate_contract_test"](
        {"endpoint": "/v1/invoices", "method": "GET"}
    )
    assert not is_error(result), text_of(result)
    base, _ = servers(_target_with_its_own_names(anonymous_status=403))
    run = _run_generated(structured(result)["source"], base, tmp_path / "run", token="JWT-1")
    assert run.returncode == 0, run.stdout + run.stderr
