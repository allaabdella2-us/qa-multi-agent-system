"""env_control regressions found before 0.0.2.

Each test is a bug reproduced against the shipped server. The HTTP ones stand up
stub servers on 127.0.0.1, the way the contract-test suite already does, so the
default run stays offline.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import is_error, structured, text_of

from qaas.mcp import env_control
from qaas.mcp.context import handlers
from qaas.mcp.env_control import DOCKER_BIN_ENV, _current_branch, _exec, build_tools
from qaas.target import Auth

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

LIFECYCLE = {"spin_up": {}, "seed": {"fixture": "default"}, "reset": {}, "tear_down": {}}


def _with_profile(ctx, **environment):
    profile = ctx.config.profile.model_copy(deep=True)
    for key, value in environment.items():
        setattr(profile.environment, key, value)
    ctx.config = ctx.config.model_copy(update={"profile": profile})
    return profile


def _gone(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class _Recorder:
    """Every request a stub server saw, headers included."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []


def _serve(routes: dict[str, tuple[int, dict[str, str], bytes]]) -> tuple[ThreadingHTTPServer, _Recorder]:
    seen = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.requests.append((self.command, self.path, dict(self.headers)))
            status, headers, body = routes.get(self.path, (200, {}, b"{}"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer  # noqa: N815 - BaseHTTPRequestHandler's contract

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def servers():
    started: list[ThreadingHTTPServer] = []

    def _start(routes):
        server, seen = _serve(routes)
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    yield _start
    for server in started:
        server.shutdown()


# -- 2. a timeout kills what the command started ----------------------------


@POSIX_ONLY
async def test_a_timeout_kills_the_whole_process_group(tmp_path):
    """`proc.kill()` reached the child alone. What it had started kept running
    and kept the pipes open, so the `communicate()` after the kill waited for it."""
    pidfile = tmp_path / "grandchild.pid"
    started = time.monotonic()
    proc = await _exec(
        ["/bin/sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=1
    )
    assert proc.timed_out
    assert time.monotonic() - started < 15, "the kill waited on a grandchild's pipe"
    assert _gone(int(pidfile.read_text().strip()))


# -- 3. one stray byte does not raise ---------------------------------------


def test_a_branch_name_that_is_not_utf8_does_not_raise(tmp_path, monkeypatch):
    """`text=True` decoded strictly and UnicodeDecodeError is neither OSError
    nor SubprocessError, so it escaped `spin_up` and `status`."""
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nprintf 'caf\\351\\n'\n")
    fake_git.chmod(0o755)
    monkeypatch.setattr(env_control.shutil, "which", lambda name: str(fake_git))
    assert _current_branch(tmp_path) == "caf\ufffd"


# -- 4. a redirect is an answer, never a hop --------------------------------


async def test_a_redirect_to_another_host_is_reported_not_followed(make_ctx, servers, monkeypatch):
    """A 302 from the target to another host sent `Authorization: Bearer ...` and
    `X-Api-Key` there, and the agent saw a 200 with that host's body."""
    elsewhere, elsewhere_saw = servers({"/": (200, {}, b'{"secret": "not yours"}')})
    target, target_saw = servers({
        "/leak": (302, {"Location": f"{elsewhere}/"}, b""),
        "/admin": (302, {"Location": "/login"}, b""),
    })
    monkeypatch.delenv("QAAS_TARGET_BASE_URL", raising=False)
    ctx = make_ctx("API")
    _with_profile(ctx, mode="external", api_url=target)
    tools = handlers(build_tools(ctx))

    result = await tools["http_request"]({
        "method": "GET", "path": "/leak", "token": "tok-123", "headers": {"X-Api-Key": "key-456"},
    })
    assert not is_error(result), text_of(result)
    assert structured(result)["status"] == 302
    assert f"Location: {elsewhere}/" in text_of(result)
    assert elsewhere_saw.requests == [], "the credential left the application under test"
    assert "not yours" not in text_of(result)

    # And the redirect is now observable at all: "does /admin send anonymous
    # users to the login page?" had no answer when urllib followed it silently.
    admin = await tools["http_request"]({"method": "GET", "path": "/admin"})
    assert structured(admin)["status"] == 302
    assert structured(admin)["location"] == "/login"
    assert [path for _, path, _ in target_saw.requests] == ["/leak", "/admin"]


# -- 7. no blocking call on the event loop ----------------------------------


async def test_the_branch_check_runs_off_the_event_loop(make_ctx, monkeypatch):
    """A sync `subprocess.run` inside an async handler stalls every concurrent
    agent's tool calls for as long as git takes."""
    monkeypatch.setenv(DOCKER_BIN_ENV, sys.executable)
    threads: list[bool] = []

    def recording(root):
        threads.append(threading.current_thread() is threading.main_thread())
        return "some-other-branch"

    monkeypatch.setattr(env_control, "_current_branch", recording)
    result = await handlers(build_tools(make_ctx("REPRODUCER")))["spin_up"]({"branch": "wanted"})
    assert is_error(result) and "some-other-branch" in text_of(result)
    assert threads == [False], "_current_branch ran on the event loop's thread"


# -- 9. no profile is not permission ----------------------------------------


@pytest.mark.parametrize("tool_name", sorted(LIFECYCLE))
async def test_the_lifecycle_tools_refuse_when_no_profile_is_loaded(make_ctx, monkeypatch, tool_name):
    """`mode is None` skipped the gate, so with no profile at all a compose file
    in the checkout was enough to spin up, reseed or tear down an environment
    nothing had said this run owns."""
    monkeypatch.setenv(DOCKER_BIN_ENV, sys.executable)
    ctx = make_ctx("REPRODUCER")
    ctx.config = ctx.config.model_copy(update={"profile": None})

    result = await handlers(build_tools(ctx))[tool_name](LIFECYCLE[tool_name])
    assert is_error(result), f"{tool_name} ran with no profile"
    assert "No target profile is loaded" in text_of(result)


# -- 10. impersonate needs a base URL, not a compose file -------------------


async def test_impersonate_works_against_an_external_target(make_ctx, tmp_path, servers, monkeypatch):
    """`preflight` asked for a compose file, so on an `external` target -- where
    `http_request` worked -- impersonate refused with "No compose file"."""
    base, saw = servers({"/v1/auth/login": (200, {}, json.dumps({"access_token": "T-EXT"}).encode())})
    monkeypatch.delenv("QAAS_TARGET_BASE_URL", raising=False)
    monkeypatch.setenv("CORVID_PASSWORD", "not-a-real-password")
    ctx = make_ctx("API", target_root=tmp_path)  # no compose file anywhere
    _with_profile(ctx, mode="external", api_url=base)

    result = await handlers(build_tools(ctx))["impersonate"]({"role": "admin"})
    assert not is_error(result), text_of(result)
    assert structured(result)["token"] == "T-EXT"
    method, path, _ = saw.requests[0]
    assert (method, path) == ("POST", "/v1/auth/login")


async def test_impersonate_refuses_when_there_is_no_running_instance(make_ctx, tmp_path):
    ctx = make_ctx("API", target_root=tmp_path)
    _with_profile(ctx, mode="none")
    result = await handlers(build_tools(ctx))["impersonate"]({"role": "admin"})
    assert is_error(result)
    assert "no running instance" in text_of(result)


async def test_token_mode_hands_back_the_token_the_profile_names(make_ctx, tmp_path, monkeypatch):
    """`auth.mode: token` was in the schema and the task brief says impersonate
    "will hand it to you"; the tool fell through to a login that does not exist."""
    ctx = make_ctx("API", target_root=tmp_path)
    profile = _with_profile(ctx, mode="external", api_url="http://127.0.0.1:1")
    profile.auth = Auth(mode="token", token_env="QAAS_TEST_BEARER")
    tools = handlers(build_tools(ctx))

    monkeypatch.delenv("QAAS_TEST_BEARER", raising=False)
    missing = await tools["impersonate"]({"role": "admin"})
    assert is_error(missing) and "QAAS_TEST_BEARER" in text_of(missing)

    monkeypatch.setenv("QAAS_TEST_BEARER", "bearer-from-env")
    result = await tools["impersonate"]({"role": "admin"})
    assert not is_error(result), text_of(result)
    assert structured(result)["token"] == "bearer-from-env"
    assert "cannot show you a second role" in text_of(result)


async def test_the_token_is_read_from_the_profiles_token_path(make_ctx, tmp_path, servers, monkeypatch):
    """`auth.token_path` was never read, so a login nesting its token came back
    "has no access_token" for a profile that had said where to look."""
    base, _ = servers({"/v1/auth/login": (200, {}, json.dumps({"data": {"token": "NESTED"}}).encode())})
    monkeypatch.delenv("QAAS_TARGET_BASE_URL", raising=False)
    monkeypatch.setenv("CORVID_PASSWORD", "not-a-real-password")
    ctx = make_ctx("API", target_root=tmp_path)
    profile = _with_profile(ctx, mode="external", api_url=base)
    profile.auth.token_path = "data.token"

    result = await handlers(build_tools(ctx))["impersonate"]({"role": "admin"})
    assert not is_error(result), text_of(result)
    assert structured(result)["token"] == "NESTED"


async def test_a_login_that_redirects_is_not_followed(make_ctx, tmp_path, servers, monkeypatch):
    elsewhere, elsewhere_saw = servers({"/": (200, {}, json.dumps({"access_token": "FOREIGN"}).encode())})
    base, _ = servers({"/v1/auth/login": (302, {"Location": f"{elsewhere}/"}, b"")})
    monkeypatch.delenv("QAAS_TARGET_BASE_URL", raising=False)
    monkeypatch.setenv("CORVID_PASSWORD", "not-a-real-password")
    ctx = make_ctx("API", target_root=tmp_path)
    _with_profile(ctx, mode="external", api_url=base)

    result = await handlers(build_tools(ctx))["impersonate"]({"role": "admin"})
    assert is_error(result) and "302" in text_of(result)
    assert elsewhere_saw.requests == []


async def test_spin_up_rebuilds_what_is_checked_out(make_ctx, monkeypatch):
    """A service built from source bakes its code into its image, and `up -d`
    alone reused the stale one: in a real fix cycle VERIFIER checked out FIXER's
    branch, spun up, measured the old code and returned NOT_FIXED for a correct
    fix. `--build` is what makes "builds whatever is checked out" true."""
    import sys

    from qaas.mcp import env_control
    from qaas.mcp.context import handlers

    monkeypatch.setenv(env_control.DOCKER_BIN_ENV, sys.executable)
    seen: list[list[str]] = []

    async def fake_exec(argv, timeout, *, stdin=None, cwd=None):
        seen.append(list(argv))
        return env_control._Proc(argv=list(argv), code=1, out="", err="stop here")

    monkeypatch.setattr(env_control, "_exec", fake_exec)
    tools = handlers(env_control.build_tools(make_ctx("BROWSER")))
    await tools["spin_up"]({})
    up = next(a for a in seen if "up" in a)
    assert "--build" in up, up
