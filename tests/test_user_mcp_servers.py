"""A project can declare its own MCP servers without editing installed Python.

`SDK_SERVER_MODULES` and `STDIO_SERVERS` were module-level dicts in
`registry.py` with no YAML surface, so adding a server meant editing
site-packages -- which a `pip install` makes absurd.

The security shape matters as much as the feature, and is asserted here:
declaring a server grants nothing, an agent receives one only by naming it, and
a secret is referenced by environment variable rather than written down.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from support import CONFIG_SEARCH

from qaas.config import SystemConfig, load_config
from qaas.registry import MissingServerEnv, build_mcp_servers, expand_env
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore


@pytest.fixture
def ctx_for(tmp_path):
    """A ToolContext bound to a given config. build_mcp_servers needs one to
    instantiate the in-process servers alongside the declared ones."""
    def build(agent: str, cfg: SystemConfig) -> ToolContext:
        return ToolContext(
            store=RunStore.new(tmp_path),
            maps=SystemMapStore(tmp_path),
            config=cfg,
            agent=cfg.agents[agent],
            repo_root=tmp_path,
        )
    return build


def _cfg_with(servers: dict, agent_servers: list[str] | None = None) -> SystemConfig:
    cfg = load_config(search=CONFIG_SEARCH)
    update = {"mcp_servers": servers}
    if agent_servers is not None:
        agents = dict(cfg.agents)
        agents["CARTOGRAPHER"] = agents["CARTOGRAPHER"].model_copy(
            update={"mcp_servers": agent_servers}
        )
        update["agents"] = agents
    return SystemConfig.model_validate({**cfg.model_dump(exclude={"profile"}), **update})


STDIO = {"house-lint": {"type": "stdio", "command": "./tools/lint-mcp", "args": ["--strict"]}}


# -- the schema -------------------------------------------------------------


def test_a_stdio_server_can_be_declared_in_config():
    cfg = _cfg_with(STDIO)
    assert cfg.mcp_servers["house-lint"].command == "./tools/lint-mcp"
    assert cfg.mcp_servers["house-lint"].args == ["--strict"]


def test_an_http_server_can_be_declared():
    cfg = _cfg_with({"remote": {"type": "http", "url": "https://mcp.example/v1"}})
    assert cfg.mcp_servers["remote"].url == "https://mcp.example/v1"


def test_an_unknown_field_is_rejected_rather_than_ignored():
    """extra="forbid" everywhere: a typo'd key must not be silently dropped."""
    with pytest.raises(ValidationError):
        _cfg_with({"x": {"type": "stdio", "command": "c", "shell": True}})


def test_there_is_no_in_process_python_server_type():
    """Deliberate: a module path from a config file would mean
    importlib.import_module executing arbitrary code inside the process holding
    this user's Anthropic credentials, Jira token and GitHub auth. A subprocess
    is a subprocess; an import is a foothold."""
    with pytest.raises(ValidationError):
        _cfg_with({"x": {"type": "python", "module": "evil"}})


# -- resolution -------------------------------------------------------------


def test_a_declared_server_reaches_the_agents_options(ctx_for):
    cfg = _cfg_with(STDIO, agent_servers=["envelope", "house-lint"])
    ctx = ctx_for("CARTOGRAPHER", cfg)
    servers = build_mcp_servers(cfg.agents["CARTOGRAPHER"], ctx)
    assert set(servers) == {"envelope", "house-lint"}
    assert servers["house-lint"]["command"] == "./tools/lint-mcp"
    assert servers["house-lint"]["type"] == "stdio"


def test_a_declared_server_overrides_a_built_in(ctx_for):
    """Playwright is hardcoded down to `--browser chromium`. Testing Firefox
    should not require forking the package."""
    firefox = {"playwright": {"type": "stdio", "command": "npx",
                              "args": ["-y", "@playwright/mcp@latest", "--browser", "firefox"]}}
    cfg = _cfg_with(firefox, agent_servers=["envelope", "playwright"])
    ctx = ctx_for("CARTOGRAPHER", cfg)
    servers = build_mcp_servers(cfg.agents["CARTOGRAPHER"], ctx)
    assert "firefox" in servers["playwright"]["args"]
    assert "chromium" not in servers["playwright"]["args"]


def test_declaring_a_server_grants_no_agent_anything(ctx_for):
    """The whole trust posture in one assertion."""
    cfg = _cfg_with(STDIO)  # declared, but no agent names it
    ctx = ctx_for("CARTOGRAPHER", cfg)
    servers = build_mcp_servers(cfg.agents["CARTOGRAPHER"], ctx)
    assert "house-lint" not in servers


def test_an_agent_naming_nothing_that_exists_fails_at_load_time():
    """It used to surface as UnknownServer part-way through a paid run."""
    with pytest.raises(ValidationError, match="nothing provides"):
        _cfg_with({}, agent_servers=["envelope", "no-such-server"])


# -- secrets ----------------------------------------------------------------


def test_env_references_are_expanded(monkeypatch, ctx_for):
    monkeypatch.setenv("ACME_LINT_TOKEN", "s3cret")
    cfg = _cfg_with(
        {"lint": {"type": "stdio", "command": "lint", "env": {"TOKEN": "${ACME_LINT_TOKEN}"}}},
        agent_servers=["envelope", "lint"],
    )
    ctx = ctx_for("CARTOGRAPHER", cfg)
    servers = build_mcp_servers(cfg.agents["CARTOGRAPHER"], ctx)
    assert servers["lint"]["env"]["TOKEN"] == "s3cret"


def test_an_unset_reference_is_an_error_not_an_empty_string(monkeypatch, ctx_for):
    """The CLI would expand this itself and substitute "" for a missing
    variable, so a forgotten token surfaces much later as an unexplained auth
    failure instead of as the forgotten token it is."""
    monkeypatch.delenv("ACME_MISSING", raising=False)
    cfg = _cfg_with(
        {"lint": {"type": "stdio", "command": "lint", "env": {"TOKEN": "${ACME_MISSING}"}}},
        agent_servers=["envelope", "lint"],
    )
    ctx = ctx_for("CARTOGRAPHER", cfg)
    with pytest.raises(MissingServerEnv, match="ACME_MISSING"):
        build_mcp_servers(cfg.agents["CARTOGRAPHER"], ctx)


def test_expansion_is_idempotent_with_the_clis_own():
    """We expand, then the CLI expands again. A value with no ${} left is
    passed through unchanged, so doing both is harmless."""
    assert expand_env("already-expanded", where="t") == "already-expanded"
