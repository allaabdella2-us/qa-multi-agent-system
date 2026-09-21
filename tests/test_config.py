"""M0 verification: the config is a real contract, and the §5.3 cap is enforced."""

import os
import re
import sys
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
#: The synthesis layer. SYNTHESIZER's input is the other agents' output rather
#: than the application, so it needed the second piece of Python the roster has
#: ever needed: a `_phase_synthesise` between discover and reproduce. Like
#: `_phase_report` it dispatches by LAYER, so a second synthesis agent is a
#: prompt and a YAML again.
PHASE_5 = {"SYNTHESIZER"}
ROSTER = PHASE_1 | PHASE_3 | PHASE_2 | PHASE_4 | PHASE_5


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


def test_the_board_records_that_a_fix_started_not_only_that_it_landed(cfg):
    """`In Progress` has to be written by someone, and FIXER is the only someone.

    FIXER held `may_transition_tickets` and the tracker server from the start and
    was never asked to use them, so no agent ever wrote the middle column: a live
    run moved a ticket To Do -> Done and a person watching the board saw a fix
    appear from nowhere. The board is this system's coordination channel rather
    than a report of it, which makes the transition part of FIXER's output
    contract and not a nicety in its prompt.
    """
    spec = cfg.agents["FIXER"]
    assert spec.policy.may_transition_tickets
    assert "tracker" in spec.mcp_servers
    assert "mcp__tracker__transition" in spec.must_call
    assert "mcp__vcs__open_pr" in spec.must_call


def test_the_fix_task_asks_for_the_transition_it_requires(cfg):
    """A `must_call` the task never mentions is a Stop-hook block waiting to happen."""
    from qaas import tasks

    task = tasks.fixer("QAAS-13", None, cfg)
    assert "in_progress" in task
    assert "QAAS-13" in task


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


def test_a_config_dir_that_does_not_exist_is_an_error(tmp_path):
    """`--config` heads the layered search; it does not replace it.

    So an *empty* directory legitimately falls through to the packaged layer --
    that is how a user with one overridden agent and no `system.yaml` of their
    own works. A directory that does not exist is a typo, and dropping it
    silently ran the packaged defaults while reporting success.
    """
    with pytest.raises(FileNotFoundError, match="not a directory"):
        load_config(tmp_path / "nope")


def test_an_empty_config_dir_layers_onto_the_packaged_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.agents, "an empty layer should fall through, not come back empty"


def test_a_search_of_one_directory_with_no_system_yaml_is_an_error(tmp_path):
    """`search=` is the "this and nothing else" form, and keeps the old rule."""
    with pytest.raises(FileNotFoundError):
        load_config(search=[tmp_path])


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


def test_a_mode_that_cannot_afford_its_agents_is_visible(tmp_path):
    """The shipped config sets no caps, so this asserted nothing at all.

    Its only assertion sat inside `if caps and mode.max_budget_usd is not None`,
    and `test_no_vendor_pricing_is_baked_into_the_shipped_config` pins every one
    of those to None -- so neither condition could hold and the test passed by
    never reaching an `assert`. Build a config that actually has the problem.
    """
    cfg = load_config(search=CONFIG_SEARCH)
    mode = cfg.run_modes["pr-check"]
    agents = {
        name: spec.model_copy(update={"max_budget_usd": 5.0})
        for name, spec in cfg.agents.items()
        if name in mode.agents
    }
    broke = cfg.model_copy(
        update={
            "agents": {**cfg.agents, **agents},
            "run_modes": {**cfg.run_modes, "pr-check": mode.model_copy(update={"max_budget_usd": 1.0})},
        }
    )
    underfunded = [
        name for name, m in broke.run_modes.items()
        if m.max_budget_usd is not None
        and sum(
            broke.agents[a].max_budget_usd or 0.0 for a in m.agents if a in broke.agents
        ) > m.max_budget_usd
    ]
    assert "pr-check" in underfunded, (
        "the arithmetic this test is about no longer detects an underfunded mode"
    )


def test_the_shipped_modes_are_affordable_where_caps_exist():
    cfg = load_config(search=CONFIG_SEARCH)
    checked = 0
    for name, mode in cfg.run_modes.items():
        caps = [cfg.agents[a].max_budget_usd for a in mode.agents
                if a in cfg.agents and cfg.agents[a].max_budget_usd is not None]
        if caps and mode.max_budget_usd is not None:
            checked += 1
            assert sum(caps) <= mode.max_budget_usd, f"mode '{name}' cannot finish"
    # The shipped config deliberately sets no dollar caps, so `checked` is 0 --
    # said out loud, because an assertion that never runs reads as one that
    # passed.
    assert checked == 0, "caps now exist; this test has become load-bearing"

# -- the documentation is part of the surface -------------------------------


def test_the_readme_roster_matches_the_shipped_one():
    """The README listed fifteen agents while the roster held sixteen.

    It had drifted twice by the time anyone noticed, and the second time the
    missing agent was SYNTHESIZER -- whose absence had been the headline finding
    of an audit the week before. A rendered picture of data that lives in YAML
    goes stale every time the data moves and says nothing when it does, which is
    why the README carries a table and this test carries the guarantee.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    cfg = load_config(search=CONFIG_SEARCH)

    documented = set(re.findall(r"^\| \*\*([A-Z][A-Z_]+)\*\* \|", readme, re.M))
    shipped = set(cfg.agents)

    assert documented == shipped, (
        f"README roster is out of step. Missing: {sorted(shipped - documented)}. "
        f"Listed but not shipped: {sorted(documented - shipped)}."
    )


def test_the_docs_agree_with_the_suite_about_its_own_size():
    """`pytest -q` prints a number and two files quote it.

    Both said 866 when it was 987 -- close enough to look maintained and wrong
    enough to mislead. Asserted as a floor rather than an equality, so adding a
    test does not fail the build; it fails only once the claim has fallen behind
    by enough to matter.
    """
    import subprocess

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO, capture_output=True, text=True, env={**os.environ, "QAAS_ENV_FILE": ""},
    ).stdout
    match = re.search(r"(\d+)/\d+ tests collected", collected)
    if match is None:  # pragma: no cover - collection shape changed
        pytest.skip("could not read the collected count from pytest")
    actual = int(match.group(1))

    for name in ("README.md", "CLAUDE.md"):
        claimed = re.search(r"([\d,]+) tests", (REPO / name).read_text(encoding="utf-8"))
        assert claimed, f"{name} no longer states a test count"
        stated = int(claimed.group(1).replace(",", ""))
        assert stated <= actual, f"{name} claims {stated} tests; only {actual} exist"
        assert stated >= actual * 0.9, (
            f"{name} claims {stated} tests and there are {actual} -- the claim has "
            "fallen behind by more than 10%"
        )


# -- an agent's paths belong to the target, not to the demo -----------------
#
# `write_paths` are target-relative globs and the shipped roster's were
# target-app's own directories: FIXER carried `[api/app, web/src, qa/repro]`.
# Against a real repository whose application lives under
# `build-battle/merchant-console/src`, two of those three matched no file and
# the survivor was `qa/repro` -- REPRODUCER's sandbox. FIXER ran, could reach no
# product code, committed nothing that changed the defect, and reported success.
# REVIEWER caught it by reading the diff; nothing else in the system would have.


def _project_with_layout(tmp_path, name, **layout):
    """A scratch config whose target declares a particular layout."""
    from support import make_project

    root = make_project(tmp_path)
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    targets = root / ".qaas" / "config" / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    body = [f"name: {name}", f"root: {app}", "description: scratch",
            "default_branch: main", "layout:"]
    for section, paths in layout.items():
        body.append(f"  {section}: [{', '.join(paths)}]")
    body += ["environment:", "  mode: none", "auth:", "  mode: none"]
    (targets / f"{name}.yaml").write_text("\n".join(body) + "\n", encoding="utf-8")
    return root / ".qaas" / "config"


def test_layout_tokens_resolve_to_this_target_s_own_directories(tmp_path):
    config = _project_with_layout(
        tmp_path, "widget", backend=["server/src"], frontend=["ui/src"]
    )
    cfg = load_config(config, target="widget")
    paths = cfg.agents["FIXER"].policy.write_paths

    assert "server/src" in paths and "ui/src" in paths
    assert "$backend" not in paths and "$frontend" not in paths
    # Literal entries beside the tokens are untouched.
    assert "qa/repro" in paths


def test_one_agent_file_serves_two_differently_shaped_repositories(tmp_path):
    """The whole point: agent config is global, so it cannot name directories.

    Editing FIXER for one repository used to mis-configure every other, and
    `qaas doctor` reported the damage in both directions at once.
    """
    a = _project_with_layout(tmp_path / "a", "alpha", backend=["services/api"])
    b = _project_with_layout(tmp_path / "b", "beta", frontend=["web/app"])

    assert "services/api" in load_config(a, target="alpha").agents["FIXER"].policy.write_paths
    assert "web/app" in load_config(b, target="beta").agents["FIXER"].policy.write_paths


def test_a_section_the_profile_leaves_empty_grants_nothing(tmp_path):
    """Expanding an empty section to the repository root would hand over the tree.

    An agent that may write nowhere is a state `qaas doctor` reports and a human
    fixes. It is not a reason to widen the §8.1 matrix by accident.
    """
    config = _project_with_layout(tmp_path, "backendless", backend=["server/src"])
    paths = load_config(config, target="backendless").agents["FIXER"].policy.write_paths

    assert "server/src" in paths
    assert "." not in paths and "" not in paths
    assert not any(p.startswith("$") for p in paths)


def test_an_unknown_token_is_left_alone_rather_than_failing_the_load(tmp_path):
    """It becomes a glob matching nothing, which doctor reports.

    Better than refusing to load: the file may belong to someone else, and a
    typo in it should not take the whole CLI down.
    """
    from qaas.config import expand_layout_tokens

    class _Layout:
        backend = ["server/src"]

    assert expand_layout_tokens(["$backend", "$nonsense"], _Layout()) == [
        "server/src", "$nonsense",
    ]


def test_a_per_target_agent_file_shadows_the_shared_one(tmp_path):
    """Paths vary by layout token; a model or a turn cap needs a real overlay."""
    config = _project_with_layout(tmp_path, "widget", backend=["server/src"])
    overlay = config / "agents" / "widget"
    overlay.mkdir(parents=True)
    spec = yaml.safe_load((config / "agents" / "fixer.yaml").read_text())
    spec["max_turns"] = 7
    (overlay / "fixer.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

    assert load_config(config, target="widget").agents["FIXER"].max_turns == 7


def test_an_overlay_for_another_target_is_not_applied(tmp_path):
    config = _project_with_layout(tmp_path, "widget", backend=["server/src"])
    overlay = config / "agents" / "somebody-else"
    overlay.mkdir(parents=True)
    spec = yaml.safe_load((config / "agents" / "fixer.yaml").read_text())
    spec["max_turns"] = 7
    (overlay / "fixer.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

    assert load_config(config, target="widget").agents["FIXER"].max_turns != 7


# -- how wide a fix may be belongs to the codebase --------------------------


def _with_budget(tmp_path, **budget):
    config = _project_with_layout(tmp_path, "widget", backend=["src"])
    path = config / "targets" / "widget.yaml"
    body = path.read_text()
    if budget:
        lines = "\n".join(f"  {k}: {v}" for k, v in budget.items())
        body += f"diff_budget:\n{lines}\n"
    path.write_text(body)
    return config


def test_a_target_may_widen_the_diff_budget(tmp_path):
    """§8.2 ships one global number and a legitimate fix's width is per-codebase.

    FIXER's `max_diff_files: 5` could not express a cross-currency defect
    spanning six aggregation sites: it edited five, was refused the sixth, and
    ended the round with an empty branch. Neither the budget nor the fix was
    wrong -- they belong to different scopes.
    """
    cfg = load_config(_with_budget(tmp_path, max_diff_files=12), target="widget")
    assert cfg.agents["FIXER"].policy.max_diff_files == 12


def test_a_target_with_no_budget_leaves_every_agent_alone(tmp_path):
    """Which is every profile that exists today."""
    cfg = load_config(_with_budget(tmp_path), target="widget")
    packaged = load_config(CONFIG_SEARCH[-1]).agents["FIXER"].policy.max_diff_files
    assert cfg.agents["FIXER"].policy.max_diff_files == packaged


def test_it_never_hands_a_budget_to_an_agent_that_had_none(tmp_path):
    """A raise or a lower, never a grant.

    An agent the §8.2 matrix left unbounded was left so on purpose, and a
    target profile must not be a quiet way to start bounding it -- or, worse,
    to bound something that was deliberately free.
    """
    cfg = load_config(_with_budget(tmp_path, max_diff_files=3), target="widget")
    unbounded = [
        name for name, spec in cfg.agents.items()
        if spec.policy.max_diff_files is None
    ]
    assert unbounded, "this test needs an agent with no budget to be about anything"
    for name in unbounded:
        assert cfg.agents[name].policy.max_diff_files is None
