"""Verification tier 2: what each agent is actually handed, without an API call.

The config declares an allowlist; the registry turns it into options. These tests
prove nothing is added along the way — an agent that quietly receives a tool its
config never granted is exactly the drift §5.3 warns about.
"""

from pathlib import Path

import pytest

from support import CONFIG_SEARCH

from qaas.config import MAX_MCP_SERVERS_PER_AGENT, load_config
from qaas.mcp.context import ToolContext
from qaas.registry import (
    SDK_SERVER_MODULES,
    STDIO_SERVERS,
    UnknownServer,
    build_allowed_tools,
    build_mcp_servers,
    build_options,
    build_system_prompt,
    describe,
)
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    return load_config(search=CONFIG_SEARCH)


@pytest.fixture
def ctx_for(cfg, tmp_path):
    def build(agent_name: str) -> ToolContext:
        return ToolContext(
            store=RunStore.new(root=tmp_path),
            maps=SystemMapStore(tmp_path),
            config=cfg,
            agent=cfg.agents[agent_name],
            repo_root=REPO,
        )
    return build


AGENTS = ["CARTOGRAPHER", "CONDUIT", "SURFACE", "FORGE", "CLERK", "PROOF", "MENDER", "ARBITER"]


# -- every declared server is providable ------------------------------------


def test_every_server_named_in_config_has_an_implementation(cfg):
    known = set(SDK_SERVER_MODULES) | set(STDIO_SERVERS)
    for name, spec in cfg.agents.items():
        unknown = set(spec.mcp_servers) - known
        assert not unknown, f"{name} declares unimplemented servers: {unknown}"


@pytest.mark.parametrize("agent", AGENTS)
def test_each_agent_gets_exactly_the_servers_it_declared(ctx_for, cfg, agent):
    servers = build_mcp_servers(cfg.agents[agent], ctx_for(agent))
    assert sorted(servers) == sorted(cfg.agents[agent].mcp_servers)


def test_an_unknown_server_fails_loudly(cfg, ctx_for):
    spec = cfg.agents["CONDUIT"].model_copy(update={"mcp_servers": ["nonexistent"]})
    with pytest.raises(UnknownServer, match="nonexistent"):
        build_mcp_servers(spec, ctx_for("CONDUIT"))


# -- the allowlist ----------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_allowed_tools_add_nothing_beyond_the_config(cfg, agent):
    """Only the config's tools, plus harness plumbing that grants no capability."""
    from qaas.guardrails import ALWAYS_GRANTED

    spec = cfg.agents[agent]
    allowed = build_allowed_tools(spec)
    builtin = {t for t in allowed if not t.startswith("mcp__")}
    servers = [t for t in allowed if t.startswith("mcp__")]

    assert builtin == set(spec.builtin_tools) | ALWAYS_GRANTED
    assert sorted(servers) == sorted(f"mcp__{s}" for s in spec.mcp_servers)


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_can_actually_invoke_its_skills(ctx_for, cfg, agent):
    """Regression guard: the allowlist and the guardrail must agree on Skill.

    They are set in two different modules. If they drift, skills stay listed in
    the config and silently never load, which is invisible from the outside.
    """
    from qaas.guardrails import Guardrail

    assert "Skill" in build_allowed_tools(cfg.agents[agent])
    assert Guardrail(ctx_for(agent)).check("Skill", {"skill": "severity-rubric"}).allowed


@pytest.mark.parametrize("agent", AGENTS)
def test_no_agent_is_handed_more_than_six_servers(cfg, agent):
    """§5.3: 'None exceeds 6. That is intentional.'"""
    assert len(cfg.agents[agent].mcp_servers) <= MAX_MCP_SERVERS_PER_AGENT


@pytest.mark.parametrize("agent", ["CARTOGRAPHER", "CONDUIT", "SURFACE", "CLERK", "ARBITER"])
def test_read_only_agents_are_never_handed_a_write_tool(cfg, agent):
    forbidden = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    assert not forbidden & set(build_allowed_tools(cfg.agents[agent]))


def test_only_the_two_writing_agents_get_write_tools(cfg):
    writers = {
        name for name, spec in cfg.agents.items()
        if {"Write", "Edit"} & set(spec.builtin_tools)
    }
    assert writers == {"FORGE", "MENDER"}


# -- prompts ----------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_prompt_carries_the_shared_house_rules(cfg, agent):
    prompt = build_system_prompt(cfg.agents[agent])
    assert "Evidence or it did not happen" in prompt
    assert "emit_envelope" in prompt
    assert len(prompt) > 500, "a prompt this short is probably a stub"


def test_a_missing_prompt_is_an_error_not_an_empty_string(cfg):
    spec = cfg.agents["CONDUIT"].model_copy(update={"prompt": "NOPE.md"})
    with pytest.raises(FileNotFoundError):
        build_system_prompt(spec)


# -- assembled options ------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_options_assemble_with_the_agents_own_limits(ctx_for, cfg, agent):
    spec = cfg.agents[agent]
    options = build_options(spec, ctx_for(agent))

    assert options.model == spec.model
    assert options.effort == spec.effort
    assert options.max_turns == spec.max_turns
    assert options.max_budget_usd == spec.max_budget_usd
    assert options.can_use_tool is not None, "an agent without guardrails must never run"
    assert sorted(options.mcp_servers) == sorted(spec.mcp_servers)


@pytest.mark.parametrize("agent", AGENTS)
def test_local_machine_settings_never_leak_into_a_run(ctx_for, cfg, agent):
    """A run must not depend on one operator's ~/.claude.

    "project" is required — filesystem skills are only discoverable through it —
    and is safe because project settings live in the repo and travel with it.
    "user" and "local" are the ones that would make a run unreproducible.
    """
    sources = build_options(cfg.agents[agent], ctx_for(agent)).setting_sources
    assert "user" not in sources
    assert "local" not in sources
    assert sources == ["project"]


@pytest.mark.parametrize("agent", AGENTS)
def test_subagent_spawning_is_capped(ctx_for, cfg, agent):
    env = build_options(cfg.agents[agent], ctx_for(agent)).env
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert int(env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"]) <= 5


def test_describe_is_a_pure_dry_run(cfg):
    d = describe(cfg.agents["FORGE"])
    assert d["agent"] == "FORGE"
    assert "mcp__test_runner" in d["allowed_tools"]
    assert d["policy"]["branch_patterns"] == ["qa/repro/*"]
