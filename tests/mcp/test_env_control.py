"""Environment control: it must degrade, never hang and never raise.

The target app is built by other people on other days. Docker may be absent, the
daemon may be down, the compose file may not exist yet. Every one of those is a
tool result an agent can read and act on, so the bulk of this file asserts the
shape of failure rather than the shape of success. The one test that needs a
real daemon is marked `docker` and excluded from the default run.
"""

from __future__ import annotations

import sys
import time

import pytest
from conftest import REPO_ROOT, is_error, structured, text_of

from qaas.mcp.context import handlers
from qaas.mcp.env_control import (
    DOCKER_BIN_ENV,
    _exec,
    _load_compose,
    _parse_ps,
    _rows_affected,
    _service_urls,
    build,
    build_tools,
)

#: Every tool with arguments that would otherwise be valid, so a refusal can
#: only be about the environment and not about the request.
ALL_CALLS: dict[str, dict] = {
    "spin_up": {},
    "seed": {"fixture": "default"},
    "reset": {},
    "set_flag": {"key": "checkout.new_review", "value": True},
    "get_flags": {},
    "set_clock": {"iso_timestamp": "2026-01-31T23:59:59Z"},
    "http_request": {"method": "GET", "path": "/v1/orders"},
    "impersonate": {"role": "admin"},
    "status": {},
    "tear_down": {},
}

DOCKER_TOOLS = ("spin_up", "seed", "reset", "status", "tear_down")

#: `http_request` bottoms out in a socket rather than in `docker compose`, so
#: "no compose file" is not what stops it -- `environment.is_reachable` is. It
#: is excluded from the compose-degradation sweep and covered on its own below,
#: where the refusal is asserted *without* a connection being attempted: a
#: suite that reaches the network is a suite that fails on a train.
NON_COMPOSE_TOOLS = ("http_request",)
OFFLINE_TOOLS = ("set_flag", "get_flags", "set_clock")


@pytest.fixture
def tools(make_ctx):
    """Tools bound to the real repository, so the real compose file is in play."""
    return handlers(build_tools(make_ctx("BROWSER")))


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setenv(DOCKER_BIN_ENV, "/nonexistent/bin/docker")


@pytest.fixture
def fake_docker(monkeypatch):
    """A docker that exists and is executable, so preflight passes.

    Nothing in the tests using this fixture reaches the point of running it —
    they all assert on argument validation, which happens first. That ordering
    is the property being tested.
    """
    monkeypatch.setenv(DOCKER_BIN_ENV, sys.executable)


def test_the_server_exposes_the_tools_architecture_5_2_names(make_ctx):
    ctx = make_ctx("BROWSER")
    assert [t.name for t in build_tools(ctx)] == [
        "spin_up", "seed", "reset", "set_flag", "get_flags", "set_clock",
        "http_request", "impersonate", "status", "tear_down",
    ]
    assert set(ALL_CALLS) == {t.name for t in build_tools(ctx)}  # no tool escapes the refusal tests
    assert build(ctx)["name"] == "env_control"  # the name surface.yaml allowlists


# -- degradation -----------------------------------------------------------


@pytest.mark.parametrize(
    "name", sorted(set(ALL_CALLS) - set(NON_COMPOSE_TOOLS))
)
async def test_every_tool_refuses_cleanly_when_there_is_no_compose_file(make_ctx, tmp_path, name):
    """An empty repo root: no target-app, therefore no environment to control."""
    tools = handlers(build_tools(make_ctx("BROWSER", target_root=tmp_path)))
    result = await tools[name](ALL_CALLS[name])
    assert is_error(result)
    assert "compose" in text_of(result)
    assert str(tmp_path) in text_of(result)  # it names the path it looked at


@pytest.mark.parametrize("name", DOCKER_TOOLS)
async def test_the_docker_tools_refuse_cleanly_when_the_binary_is_missing(tools, no_docker, name):
    result = await tools[name](ALL_CALLS[name])
    assert is_error(result)
    assert "docker" in text_of(result).lower()
    assert DOCKER_BIN_ENV in text_of(result)  # it names why it could not find it


@pytest.mark.parametrize("name", OFFLINE_TOOLS)
async def test_the_flag_tools_still_work_without_docker(tools, no_docker, name):
    """Flags and the clock are run metadata; they do not need a daemon to record."""
    result = await tools[name](ALL_CALLS[name])
    assert not is_error(result), text_of(result)


# -- flags and clock -------------------------------------------------------


async def test_a_flag_round_trips_and_is_readable_by_the_next_tool_call(tools, no_docker):
    await tools["set_flag"]({"key": "checkout.new_review", "value": True})
    await tools["set_flag"]({"key": "pricing.tier", "value": "gold"})
    result = await tools["get_flags"]({})
    assert structured(result)["flags"] == {"checkout.new_review": True, "pricing.tier": "gold"}


async def test_setting_a_flag_warns_that_the_app_does_not_read_it_yet(tools, no_docker):
    """An agent must not attribute behaviour to a flag the app never sees."""
    result = await tools["set_flag"]({"key": "a.b", "value": 1})
    assert "does not read this file yet" in text_of(result)


async def test_a_flag_key_that_is_not_an_identifier_is_refused(tools, no_docker):
    result = await tools["set_flag"]({"key": "rm -rf /; echo", "value": True})
    assert is_error(result)
    assert "identifier" in text_of(result)


async def test_the_clock_override_is_recorded_alongside_the_flags(tools, no_docker):
    await tools["set_clock"]({"iso_timestamp": "2026-01-31T23:59:59Z"})
    assert structured(await tools["get_flags"]({}))["clock"] == "2026-01-31T23:59:59Z"


async def test_a_clock_that_is_not_a_timestamp_is_refused(tools, no_docker):
    result = await tools["set_clock"]({"iso_timestamp": "next tuesday"})
    assert is_error(result)
    assert "ISO-8601" in text_of(result)


async def test_flags_are_scoped_to_one_run(make_ctx, no_docker):
    """Two runs must not inherit each other's environment, or a repro is a lie."""
    first = handlers(build_tools(make_ctx("BROWSER")))
    await first["set_flag"]({"key": "a.b", "value": True})
    second = handlers(build_tools(make_ctx("BROWSER")))
    assert structured(await second["get_flags"]({}))["flags"] == {}


# -- arguments never become commands ---------------------------------------


@pytest.mark.parametrize("fixture", ["../../etc/passwd", "fixtures.sql; rm -rf /", "a/b"])
async def test_a_fixture_name_that_is_not_a_bare_filename_is_refused(tools, fake_docker, fixture):
    result = await tools["seed"]({"fixture": fixture})
    assert is_error(result)
    assert "bare filename" in text_of(result)


async def test_an_unknown_fixture_lists_the_ones_that_exist(tools, fake_docker):
    result = await tools["seed"]({"fixture": "nosuchfixture"})
    assert is_error(result)
    assert "fixtures" in text_of(result)  # the shipped default is named


async def test_spin_up_refuses_a_service_the_compose_file_does_not_declare(tools, fake_docker):
    result = await tools["spin_up"]({"services": ["db", "postgres-prod"]})
    assert is_error(result)
    assert "postgres-prod" in text_of(result)
    assert "db, api, web" in text_of(result) or "db" in text_of(result)


async def test_spin_up_refuses_to_pretend_it_is_on_another_branch(tools, fake_docker):
    """Building whatever is checked out while claiming a branch would poison a repro."""
    result = await tools["spin_up"]({"branch": "definitely-not-the-checked-out-branch"})
    assert is_error(result)
    assert "check the branch out" in text_of(result).lower()


# -- impersonation ---------------------------------------------------------


async def test_impersonate_reports_an_unreachable_api_rather_than_hanging(tools, monkeypatch):
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CORVID_PASSWORD", "password123")
    result = await tools["impersonate"]({"role": "admin"})
    assert is_error(result)
    assert "spin_up" in text_of(result)


async def test_impersonate_refuses_before_connecting_when_the_password_is_unset(
    tools, monkeypatch
):
    """A missing credential is reported as a missing credential.

    It used to be impossible to reach this: the password was the hardcoded
    literal `password123`, so impersonation always had one -- and pointing qaas
    at a real application meant POSTing that string at its login endpoint. Now
    the profile names an environment variable, and an unset one stops here
    rather than surfacing later as an unexplained 401.
    """
    monkeypatch.delenv("CORVID_PASSWORD", raising=False)
    result = await tools["impersonate"]({"role": "admin"})
    assert is_error(result)
    assert "CORVID_PASSWORD" in text_of(result), "the error must name the variable to set"


async def test_impersonate_refuses_a_role_the_target_does_not_declare(tools):
    """The roles offered come from the profile, not from a fixed demo list --
    corvid declares four, including `other_org` for cross-tenant checks."""
    result = await tools["impersonate"]({"role": "root"})
    assert is_error(result)
    text = text_of(result)
    assert "admin" in text and "other_org" in text
    assert "root" in text


# -- pure helpers ----------------------------------------------------------


async def test_a_subprocess_that_overruns_its_budget_is_killed(tmp_path):
    """Every docker call goes through this. A hung agent is worse than a failed one."""
    started = time.monotonic()
    proc = await _exec(["/bin/sleep", "30"], timeout=0.3)
    assert proc.timed_out and not proc.okay
    assert "timed out" in proc.err
    assert time.monotonic() - started < 10  # it did not wait for sleep to finish


async def test_a_binary_that_is_not_there_comes_back_as_a_result_not_an_exception():
    proc = await _exec(["/nonexistent/bin/docker", "ps"], timeout=5)
    assert not proc.okay
    assert proc.tail()  # something a human can read


def test_service_urls_come_from_the_real_compose_file():
    compose = _load_compose(REPO_ROOT / "target-app" / "docker-compose.yml")
    urls = _service_urls(compose)
    assert urls["api"] == "http://localhost:8000"
    assert urls["web"] == "http://localhost:5173"
    assert urls["db"] == "postgresql://corvid:corvid@localhost:55432/corvid"


def test_compose_ps_output_parses_in_both_shapes():
    jsonl = '{"Service":"db","State":"running","Health":"healthy"}\n{"Service":"api","State":"running","Health":""}'
    array = '[{"Service":"db","State":"running","Health":"healthy"}]'
    assert [r["Service"] for r in _parse_ps(jsonl)] == ["db", "api"]
    assert [r["Service"] for r in _parse_ps(array)] == ["db"]
    assert _parse_ps("") == []
    assert _parse_ps("not json at all") == []


def test_rows_affected_pairs_psql_tags_with_the_statements_that_caused_them():
    sql = "INSERT INTO users VALUES (1);\nINSERT INTO orders VALUES (2);\nUPDATE orders SET x=1;\n"
    output = "INSERT 0 3\nINSERT 0 12\nUPDATE 4\n"
    assert _rows_affected(sql, output) == {"users": 3, "orders": 16}


def test_rows_affected_reports_nothing_rather_than_guessing_when_psql_is_silent():
    assert _rows_affected("INSERT INTO users VALUES (1);", "") == {}


def test_the_real_fixture_file_parses_into_per_table_counts():
    """The shipped fixture is the one seed() will actually run."""
    sql = (REPO_ROOT / "target-app" / "api" / "seed" / "fixtures.sql").read_text()
    fake_output = "\n".join("INSERT 0 1" for _ in range(40))
    assert "users" in _rows_affected(sql, fake_output)


# -- the one test that needs a daemon --------------------------------------


@pytest.mark.docker
async def test_a_full_environment_round_trip(tools, monkeypatch):
    """Excluded from the default run. Needs Docker and a buildable target app.

    The explicit `seed` here is load-bearing: compose already applies
    fixtures.sql through initdb, so this is the *second* application of that
    file within one container's life. It used to die on `duplicate key value
    violates unique constraint "organizations_pkey"`, which left `status`
    reporting no fixture and made a reproduction impossible to pin. The fixture
    truncates first now; this assertion is what keeps it that way.
    """
    # The demo's fixture password is public -- it is in fixtures.sql, and the
    # accounts exist only inside a throwaway container. It still comes from the
    # environment, because the profile names a variable and never a literal.
    monkeypatch.setenv("CORVID_PASSWORD", "password123")
    try:
        up = await tools["spin_up"]({})
        assert not is_error(up), text_of(up)
        assert structured(up)["services"]["api"]["state"].startswith("running")

        assert not is_error(await tools["seed"]({"fixture": "default"}))

        token = await tools["impersonate"]({"role": "admin"})
        assert not is_error(token), text_of(token)
        assert structured(token)["token"]

        state = await tools["status"]({})
        assert structured(state)["fixture"] == "default"

        assert not is_error(await tools["reset"]({}))
        assert not is_error(await tools["tear_down"]({}))
    finally:
        # tear_down is the point of the test, but the rest of the `docker` tier
        # shares one environment and this file sorts ahead of tests/target_app.
        # Leaving it torn down failed every seeded-defect test behind it with
        # `Connection refused` -- a collapse that looked like the app, not the
        # suite. Put back what we removed, pass or fail.
        await tools["spin_up"]({})


@pytest.mark.parametrize(
    "entry, expected",
    [
        (8000, 8000),
        ("8000", 8000),
        ("8000:8000", 8000),
        ("127.0.0.1:8000:8000", 8000),
        ("0.0.0.0:5432:5432", 5432),
        ("8000-8002:8000-8002", 8000),
        ("8000:8000/tcp", 8000),
        ({"published": 8000}, 8000),
        ("not-a-port", None),
    ],
)
def test_a_published_host_port_is_read_in_every_spelling_compose_allows(entry, expected):
    """The three-part form binds an interface, and its first segment is an address.

    Splitting on the first colon read `127.0.0.1` as the port, so a service bound
    to an explicit interface reported no reachable URL at all.
    """
    from qaas.mcp.env_control import _host_port

    assert _host_port(entry) == expected


# -- the lifecycle gate -----------------------------------------------------


@pytest.mark.parametrize("tool_name", ["spin_up", "seed", "reset", "tear_down"])
@pytest.mark.parametrize("mode", ["none", "external"])
async def test_a_target_this_system_does_not_own_is_never_reset(make_ctx, mode, tool_name):
    """`environment.mode` is the load-bearing field of the target system, and
    this server had never read it.

    `none` means static reads only; `external` means "exercise it, but someone
    else may be relying on it and a reset has no undo". The server checked only
    whether a compose file happened to exist and whether `docker` was on PATH --
    so a compose file left lying in a repository was enough to destroy a shared
    staging environment the profile had explicitly declared off-limits.
    """
    ctx = make_ctx("REPRODUCER")
    profile = ctx.config.profile.model_copy(deep=True)
    profile.environment.mode = mode
    ctx.config = ctx.config.model_copy(update={"profile": profile})

    result = await handlers(build_tools(ctx))[tool_name]({})
    assert is_error(result), f"{tool_name} was allowed against a {mode} environment"
    assert f"environment.mode: {mode}" in text_of(result)


@pytest.mark.parametrize("tool_name", ["status", "get_flags"])
async def test_reading_an_environment_is_never_gated_on_owning_it(make_ctx, tool_name):
    """The gate is on the lifecycle, not on the server. An agent pointed at an
    external environment must still be able to look at it."""
    ctx = make_ctx("REPRODUCER")
    profile = ctx.config.profile.model_copy(deep=True)
    profile.environment.mode = "external"
    ctx.config = ctx.config.model_copy(update={"profile": profile})

    result = await handlers(build_tools(ctx))[tool_name]({})
    assert "does not own the environment" not in text_of(result)


# -- http_request -----------------------------------------------------------


async def test_a_target_with_no_running_instance_is_not_dialled(make_ctx):
    """Refused before any socket is opened.

    Two things at once: `environment.mode: none` means there is nothing to call,
    and a test suite that reaches the network is a suite that fails on a train.
    """
    ctx = make_ctx("API")
    profile = ctx.config.profile.model_copy(deep=True)
    profile.environment.mode = "none"
    ctx.config = ctx.config.model_copy(update={"profile": profile})

    result = await handlers(build_tools(ctx))["http_request"]({"method": "GET", "path": "/v1/orders"})
    assert is_error(result)
    assert "no running instance" in text_of(result)


@pytest.mark.parametrize(
    "path", ["http://evil.example/x", "https://evil.example/x", "//evil.example/x"]
)
async def test_an_agent_cannot_name_a_host(make_ctx, monkeypatch, path):
    """A path, resolved against the one origin this run is pointed at.

    `WebFetch` is refused to every agent on the grounds that findings come from
    the code and the running app rather than the web. A tool that let an agent
    name its own host would be `WebFetch` wearing a different label, and the
    gate would mean nothing.
    """
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", "http://localhost:9999")
    ctx = make_ctx("API")
    profile = ctx.config.profile.model_copy(deep=True)
    profile.environment.mode = "external"
    profile.environment.api_url = "http://localhost:9999"
    ctx.config = ctx.config.model_copy(update={"profile": profile})

    result = await handlers(build_tools(ctx))["http_request"]({"method": "GET", "path": path})
    assert is_error(result)
    assert "Pass a path" in text_of(result)
