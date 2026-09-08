"""M0 verification: the config is a real contract, and the §5.3 cap is enforced."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from qaas.config import MAX_MCP_SERVERS_PER_AGENT, AgentSpec, SystemConfig, load_config

REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / "src" / "qaas" / "prompts"

PHASE_1 = {"CARTOGRAPHER", "CONDUIT", "SURFACE", "FORGE", "CLERK", "PROOF"}
PHASE_3 = {"MENDER", "ARBITER"}
ROSTER = PHASE_1 | PHASE_3


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(REPO / "config")


def spec(**kw) -> dict:
    base = dict(name="TESTER", layer="discovery", role="r", prompt="CONDUIT.md")
    base.update(kw)
    return base


# -- the real config --------------------------------------------------------


def test_the_built_roster_is_present(cfg):
    assert set(cfg.agents) == ROSTER


def test_the_discovery_loop_can_run_without_the_fix_loop(cfg):
    """Phase 1 must stay independently runnable: the discovery modes name no
    remediation agent, so adding the fixer did not make finding defects depend
    on being able to fix them."""
    for mode in ("pr-check", "nightly", "incident"):
        assert not (set(cfg.run_modes[mode].agents) & PHASE_3), mode


def test_every_agent_has_a_prompt_file(cfg):
    missing = [n for n, s in cfg.agents.items() if not s.prompt_path(PROMPTS).exists()]
    assert not missing


def test_no_agent_exceeds_the_tool_budget(cfg):
    """§5.3: 'None exceeds 6. That is intentional.'"""
    for name, s in cfg.agents.items():
        assert len(s.mcp_servers) <= MAX_MCP_SERVERS_PER_AGENT, name


def test_discovery_agents_are_read_only(cfg):
    """§2: the finder never fixes. Discovery agents file nothing and write nothing."""
    for name, s in cfg.agents.items():
        if s.layer == "discovery":
            assert s.policy.read_only, f"{name} is a discovery agent with write access"


def test_clerk_is_the_only_ticket_creator(cfg):
    creators = {n for n, s in cfg.agents.items() if s.policy.may_create_tickets}
    assert creators == {"CLERK"}


def test_proof_may_transition_but_not_create(cfg):
    proof = cfg.agents["PROOF"]
    assert proof.policy.may_transition_tickets
    assert not proof.policy.may_create_tickets


def test_writers_are_confined_to_their_own_branch_namespaces(cfg):
    """Two agents may write, and each only where its own work belongs (§8.1)."""
    writers = {n for n, s in cfg.agents.items() if s.policy.write_paths}
    assert writers == {"FORGE", "MENDER"}
    assert cfg.agents["FORGE"].policy.branch_patterns == ["qa/repro/*"]
    assert cfg.agents["MENDER"].policy.branch_patterns == ["fix/*"]


def test_forge_writes_only_tests_never_product_code(cfg):
    """The reproducer must not be able to fix what it is reproducing."""
    assert cfg.agents["FORGE"].policy.write_paths == ["qa/repro"]
    assert not cfg.agents["FORGE"].policy.may_open_pr


def test_the_fixer_has_a_bounded_autonomy_envelope(cfg):
    """§8.2. Without these, 'MENDER may write product code' has no limit."""
    policy = cfg.agents["MENDER"].policy
    assert policy.max_diff_files and policy.max_diff_files <= 10
    assert policy.max_diff_lines and policy.max_diff_lines <= 300
    forbidden = " ".join(policy.forbidden_paths).lower()
    for cls in ("migration", "auth", "payment", "secret", ".tf"):
        assert cls in forbidden, f"§8.2 names {cls} as needing human approval"


def test_the_reviewer_cannot_write_code(cfg):
    """ARBITER reviewing with write access would defeat the separation."""
    policy = cfg.agents["ARBITER"].policy
    assert policy.read_only
    assert not policy.write_paths and not policy.may_open_pr


def test_only_the_fixer_may_open_a_pull_request(cfg):
    openers = {n for n, s in cfg.agents.items() if s.policy.may_open_pr}
    assert openers == {"MENDER"}


def test_incident_mode_files_nothing(cfg):
    """§9: incident runs are diagnostic, read-only, no filing."""
    incident = cfg.run_modes["incident"]
    assert not incident.files_tickets
    assert "CLERK" not in incident.agents


def test_every_run_mode_is_budgeted(cfg):
    for name, mode in cfg.run_modes.items():
        assert mode.max_budget_usd > 0, name
        assert mode.max_wall_clock_s > 0, name


def test_enabled_agents_resolves_a_mode(cfg):
    names = [s.name for s in cfg.enabled_agents("pr-check")]
    assert names == ["CARTOGRAPHER", "CONDUIT", "SURFACE", "FORGE", "CLERK"]


def test_unknown_mode_is_an_error(cfg):
    with pytest.raises(KeyError):
        cfg.enabled_agents("does-not-exist")


# -- validation rules -------------------------------------------------------


def test_seventh_mcp_server_is_rejected():
    with pytest.raises(ValidationError, match="cap is 6"):
        AgentSpec.model_validate(spec(mcp_servers=[f"s{i}" for i in range(7)]))


def test_duplicate_mcp_server_is_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        AgentSpec.model_validate(spec(mcp_servers=["envelope", "envelope"]))


def test_unknown_agent_field_is_rejected():
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(spec(temperature=0.7))


def test_run_mode_naming_an_unknown_agent_is_rejected():
    with pytest.raises(ValidationError, match="unknown agents"):
        SystemConfig.model_validate(
            {
                "run_modes": {"nightly": {"trigger": "cron", "agents": ["GHOST"]}},
                "agents": {},
            }
        )


def test_duplicate_agent_definition_is_rejected(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "system.yaml").write_text("project: t\n")
    for filename in ("a.yaml", "b.yaml"):
        (tmp_path / "agents" / filename).write_text(
            yaml.safe_dump(spec(name="TWIN"))
        )
    with pytest.raises(ValueError, match="duplicate agent definition"):
        load_config(tmp_path)


def test_missing_system_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path)
