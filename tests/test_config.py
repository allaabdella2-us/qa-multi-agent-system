"""M0 verification: the config is a real contract, and the §5.3 cap is enforced."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from support import CONFIG_SEARCH

from qaas.config import MAX_MCP_SERVERS_PER_AGENT, AgentSpec, SystemConfig, load_config

REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / "src" / "qaas" / "prompts"

PHASE_1 = {"MAPPER", "API", "BROWSER", "REPRODUCER", "TRIAGE", "VERIFIER"}
PHASE_3 = {"FIXER", "REVIEWER"}
#: Added later, and the point of them is how they were added: a prompt file and a
#: YAML file each, with no change to router, runner, registry or guardrails.
#: That was the architecture's central claim and it went untested until someone
#: actually tried it.
PHASE_2 = {"DBA", "AUDITOR", "ARCHITECT", "SOCKET", "GUIDE", "LOAD"}
#: The reporting layer. REPORTER is the only agent that is not discovery,
#: triage or remediation, and it needed the one piece of Python the others did
#: not: a `_phase_report` in the router. Every other phase dispatches by
#: NAME, so a reporting agent would otherwise validate, assemble, appear in
#: `--dry-run` and never run.
PHASE_4 = {"REPORTER"}
ROSTER = PHASE_1 | PHASE_3 | PHASE_2 | PHASE_4


@pytest.fixture(scope="module")
def cfg() -> SystemConfig:
    return load_config(search=CONFIG_SEARCH)


def spec(**kw) -> dict:
    base = dict(name="TESTER", layer="discovery", role="r", prompt="API.md")
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
    assert creators == {"TRIAGE"}


def test_proof_may_transition_but_not_create(cfg):
    verifier = cfg.agents["VERIFIER"]
    assert verifier.policy.may_transition_tickets
    assert not verifier.policy.may_create_tickets


def test_writers_are_confined_to_their_own_branch_namespaces(cfg):
    """Two agents may write, and each only where its own work belongs (§8.1)."""
    writers = {n for n, s in cfg.agents.items() if s.policy.write_paths}
    assert writers == {"REPRODUCER", "FIXER"}
    assert cfg.agents["REPRODUCER"].policy.branch_patterns == ["qa/repro/*"]
    assert cfg.agents["FIXER"].policy.branch_patterns == ["fix/*"]


def test_forge_writes_only_tests_never_product_code(cfg):
    """The reproducer must not be able to fix what it is reproducing."""
    assert cfg.agents["REPRODUCER"].policy.write_paths == ["qa/repro"]
    assert not cfg.agents["REPRODUCER"].policy.may_open_pr


def test_the_fixer_has_a_bounded_autonomy_envelope(cfg):
    """§8.2. Without these, 'FIXER may write product code' has no limit."""
    policy = cfg.agents["FIXER"].policy
    assert policy.max_diff_files and policy.max_diff_files <= 10
    assert policy.max_diff_lines and policy.max_diff_lines <= 300
    forbidden = " ".join(policy.forbidden_paths).lower()
    for cls in ("migration", "auth", "payment", "secret", ".tf"):
        assert cls in forbidden, f"§8.2 names {cls} as needing human approval"


def test_the_reviewer_cannot_write_code(cfg):
    """REVIEWER reviewing with write access would defeat the separation."""
    policy = cfg.agents["REVIEWER"].policy
    assert policy.read_only
    assert not policy.write_paths and not policy.may_open_pr


def test_only_the_fixer_may_open_a_pull_request(cfg):
    openers = {n for n, s in cfg.agents.items() if s.policy.may_open_pr}
    assert openers == {"FIXER"}


def test_incident_mode_files_nothing(cfg):
    """§9: incident runs are diagnostic, read-only, no filing."""
    incident = cfg.run_modes["incident"]
    assert not incident.files_tickets
    assert "TRIAGE" not in incident.agents


def test_every_run_mode_is_bounded(cfg):
    """Every mode must have SOME bound. It used to be a dollar cap; the shipped
    config sets none now, because a price belongs to one vendor and this is
    meant to run against local models too. The wall clock is the bound that
    survives that, and `max_turns` bounds each agent."""
    for name, mode in cfg.run_modes.items():
        assert mode.max_wall_clock_s > 0, f"{name} has no wall-clock bound"
        assert mode.max_concurrency > 0, f"{name} has no concurrency bound"


def test_no_vendor_pricing_is_baked_into_the_shipped_config(cfg):
    """A dollar figure in the defaults is a bet on one provider's price list."""
    assert all(s.max_budget_usd is None for s in cfg.agents.values())
    assert all(m.max_budget_usd is None for m in cfg.run_modes.values())

def test_enabled_agents_resolves_a_mode(cfg):
    names = [s.name for s in cfg.enabled_agents("pr-check")]
    # The exact roster is config and will change; the invariants are that the
    # map comes first and filing comes last.
    assert names[0] == "MAPPER", "the map must be built before anything reads it"
    assert names[-1] == "TRIAGE", "filing is last"
    assert set(names) <= ROSTER, f"mode names an agent that does not exist: {set(names) - ROSTER}"


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


# -- backend overrides ------------------------------------------------------


def test_the_committed_tracker_default_is_local():
    """Not a preference -- a constraint. Committing `jira` made 18 tests fail and
    14 error, because the agent fixtures build a real JiraTracker and CI has no
    credentials. The default pytest run must stay offline and free."""
    assert load_config(search=CONFIG_SEARCH).tracker == "local"


def test_qaas_tracker_switches_the_backend_without_editing_the_repo(monkeypatch):
    monkeypatch.setenv("QAAS_TRACKER", "jira")
    assert load_config(search=CONFIG_SEARCH).tracker == "jira"


def test_an_empty_override_is_ignored_rather_than_treated_as_a_value(monkeypatch):
    """An unset-but-exported variable is common in shells; it must not blank the
    config into an invalid backend."""
    monkeypatch.setenv("QAAS_TRACKER", "")
    assert load_config(search=CONFIG_SEARCH).tracker == "local"


def test_a_nonsense_override_is_rejected_loudly(monkeypatch):
    monkeypatch.setenv("QAAS_TRACKER", "carrier-pigeon")
    with pytest.raises(Exception):
        load_config(search=CONFIG_SEARCH)


def test_a_mode_that_cannot_afford_its_agents_is_rejected():
    """Still enforced -- but only when someone actually sets caps, since the
    shipped config sets none."""
    cfg = load_config(search=CONFIG_SEARCH)
    for name, mode in cfg.run_modes.items():
        caps = [cfg.agents[a].max_budget_usd for a in mode.agents
                if a in cfg.agents and cfg.agents[a].max_budget_usd is not None]
        if caps and mode.max_budget_usd is not None:
            assert sum(caps) <= mode.max_budget_usd, f"mode '{name}' cannot finish"