"""Resource resolution: the bug class that made `pip install` produce a dead CLI.

Before `paths.py`, `config/` and `.claude/skills/` lived at the repo root,
outside the wheel, and `SKILLS_DIR` climbed `Path(__file__).parents[2]` -- which
lands on the repo root from a checkout and on `site-packages/../..` from an
install. So `qaas validate` failed for every pip user, and agents ran with no
skills at all, silently, because a missing skill is an empty listing rather than
an error.

The load-bearing test here is `test_nothing_resolves_outside_the_package_when_
there_is_no_project`: it is that whole class of bug in one assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support import CONFIG_SEARCH, PACKAGED_CONFIG, PACKAGED_PROMPTS, PACKAGED_SKILLS

from qaas.config import load_config

#: However many ship; the point is that shadowing one does not lose the rest.
PACKAGED_AGENTS = sorted((PACKAGED_CONFIG / "agents").glob("*.yaml"))
from qaas.paths import Workspace, find_project, package_root


# -- what ships -------------------------------------------------------------


def test_the_package_carries_its_own_config_prompts_and_skills():
    """If any of these is empty the wheel is dead on arrival."""
    assert (PACKAGED_CONFIG / "system.yaml").is_file()
    assert list((PACKAGED_CONFIG / "agents").glob("*.yaml")), "no agents ship"
    assert list(PACKAGED_PROMPTS.glob("*.md")), "no prompts ship"
    assert list(PACKAGED_SKILLS.glob("*/SKILL.md")), "no skills ship"


def test_every_prompt_and_skill_an_agent_names_actually_ships():
    """`qaas validate` used to catch this only in a source checkout, where the
    answer was always yes. Caught offline, forever, now."""
    cfg = load_config(search=CONFIG_SEARCH)
    ws = Workspace.resolve()
    available = set(ws.skill_names())
    for name, spec in cfg.agents.items():
        assert ws.prompt_file(spec.prompt) is not None, f"{name}: no prompt {spec.prompt}"
        missing = [s for s in spec.skills if s not in available]
        assert not missing, f"{name}: names skills that ship nowhere: {missing}"


def test_the_packaged_defaults_name_no_target(monkeypatch):
    """"Installed but not pointed at anything" is a legitimate state. Shipping
    `target: corvid` would make a fresh install try to load a demo profile that
    is deliberately not in the wheel."""
    # This repo's own suite exports QAAS_TARGET=corvid to select the bundled
    # calibration target; clear it to see what actually ships.
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    assert load_config(PACKAGED_CONFIG).target is None


# -- resolution order -------------------------------------------------------


def test_nothing_resolves_outside_the_package_when_there_is_no_project(tmp_path):
    """The whole `pip install` failure mode, in one assertion."""
    ws = Workspace.resolve(cwd=tmp_path)
    assert ws.project is None
    pkg = package_root()
    for label, dirs in (
        ("config", ws.config_dirs), ("prompts", ws.prompt_dirs), ("skills", ws.skill_dirs)
    ):
        assert dirs, f"{label} resolved to nothing at all"
        for d in dirs:
            assert d.is_relative_to(pkg), f"{label} escaped the package: {d}"


def test_a_project_config_shadows_the_packaged_one(tmp_path):
    (tmp_path / ".qaas" / "config").mkdir(parents=True)
    (tmp_path / ".qaas" / "config" / "system.yaml").write_text("project: mine\n")
    ws = Workspace.resolve(cwd=tmp_path)
    assert ws.project == tmp_path.resolve()
    assert ws.config_dirs[0] == (tmp_path / ".qaas" / "config").resolve()
    assert ws.config_file("system.yaml").read_text() == "project: mine\n"


def test_an_explicit_config_dir_outranks_the_project(tmp_path):
    (tmp_path / ".qaas" / "config").mkdir(parents=True)
    (tmp_path / ".qaas" / "config" / "system.yaml").write_text("project: project\n")
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    (explicit / "system.yaml").write_text("project: explicit\n")
    ws = Workspace.resolve(config=explicit, cwd=tmp_path)
    assert ws.config_file("system.yaml").read_text() == "project: explicit\n"


def test_find_project_walks_up_from_a_subdirectory(tmp_path):
    (tmp_path / ".qaas" / "config").mkdir(parents=True)
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_project(deep) == tmp_path.resolve()


def test_find_project_returns_none_rather_than_guessing(tmp_path):
    assert find_project(tmp_path) is None


# -- layering ---------------------------------------------------------------


def test_one_agent_file_can_be_shadowed_without_forking_the_rest(tmp_path):
    """The reason agents layer by filename instead of whole-directory: someone
    who raises MENDER's budget should keep receiving improvements to the other
    seven, not freeze on today's roster."""
    override = tmp_path / "config" / "agents"
    override.mkdir(parents=True)
    shipped = (PACKAGED_CONFIG / "agents" / "mender.yaml").read_text()
    (override / "mender.yaml").write_text(shipped.replace("max_turns: 80", "max_turns: 99"))

    cfg = load_config(search=(tmp_path / "config", *CONFIG_SEARCH))
    assert cfg.agents["MENDER"].max_turns == 99, "the override did not win"
    assert len(cfg.agents) == len(PACKAGED_AGENTS), "shadowing one agent must not drop the others"
    assert cfg.agents["CONDUIT"].max_turns == 60, "an untouched agent changed"


def test_system_yaml_is_taken_whole_from_the_first_layer(tmp_path):
    """Merging run-mode dictionaries across layers would produce a configuration
    nobody wrote and nobody could read back."""
    override = tmp_path / "config"
    override.mkdir(parents=True)
    (override / "system.yaml").write_text("project: only-mine\ntracker: local\nvcs: local\n")
    cfg = load_config(search=(override, *CONFIG_SEARCH))
    assert cfg.project == "only-mine"
    assert not cfg.run_modes, "run modes leaked in from a lower layer"


@pytest.mark.parametrize("var", ["QAAS_TARGET"])
def test_the_target_can_be_selected_from_the_environment(monkeypatch, var):
    monkeypatch.setenv(var, "corvid")
    assert load_config(search=CONFIG_SEARCH).target == "corvid"


def test_a_lone_target_is_used_without_being_named(tmp_path, monkeypatch):
    """Choosing between two profiles would be guessing; with one there is
    nothing to guess at. This is also what keeps a source checkout working now
    that the packaged `system.yaml` names no target."""
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    cfgdir = tmp_path / "config"
    (cfgdir / "targets").mkdir(parents=True)
    (cfgdir / "targets" / "only.yaml").write_text("name: only\nroot: .\n")
    cfg = load_config(search=(cfgdir, PACKAGED_CONFIG))
    assert cfg.target == "only"
    assert cfg.profile is not None and cfg.profile.name == "only"


def test_two_targets_and_no_name_stays_unresolved(tmp_path, monkeypatch):
    """Running the wrong application is worse than not running."""
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    cfgdir = tmp_path / "config"
    (cfgdir / "targets").mkdir(parents=True)
    (cfgdir / "targets" / "a.yaml").write_text("name: a\nroot: .\n")
    (cfgdir / "targets" / "b.yaml").write_text("name: b\nroot: .\n")
    cfg = load_config(search=(cfgdir, PACKAGED_CONFIG))
    assert cfg.target is None and cfg.profile is None
