"""Editable prompts: a pip user changing a prompt without editing site-packages.

A prompt is where an agent's judgement is set, and it is the first thing anyone
running this against their own code wants to change. Until this landed, the only
way to do that after a `pip install` was to edit the installed package -- an edit
git cannot see, `qaas prompts diff` could not show, and `pip install --upgrade`
silently discards.

Two properties carry the weight here:

  * each file resolves **independently**, first hit wins. Overriding `CONDUIT.md`
    must not drag a stale `_shared.md` along with it, and replacing `_shared.md`
    must not fork all eight agent prompts.
  * `<AGENT>.append.md` lands **between** the agent block and the shared block.
    The ordering is the point, not the presence: the house rules are the last
    word, so an addendum that could displace them would be an enforcement hole
    opened from a text file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from support import CONFIG_SEARCH, PACKAGED_CONFIG, PACKAGED_PROMPTS
from typer.testing import CliRunner

from qaas import cli
from qaas.config import load_config
from qaas.paths import Workspace, package_root
from qaas.registry import (
    SHARED_PROMPT,
    append_name,
    append_paths,
    build_system_prompt,
    describe,
    resolve_prompt_dirs,
)

AGENT = "CONDUIT"


@pytest.fixture(scope="module")
def cfg():
    return load_config(search=CONFIG_SEARCH)


@pytest.fixture
def spec(cfg):
    return cfg.agents[AGENT]


@pytest.fixture
def overrides(tmp_path) -> Path:
    """An empty prompt directory that outranks the packaged one."""
    d = tmp_path / "prompts"
    d.mkdir()
    return d


@pytest.fixture
def runner():
    return CliRunner()


def _project(tmp_path: Path) -> Path:
    """A directory `find_project` will recognise, with a real config in it.

    A project is defined by its *config*, so a prompts-only directory is not one
    and `Workspace.resolve()` would walk straight past it.
    """
    shutil.copytree(PACKAGED_CONFIG, tmp_path / ".qaas" / "config")
    return tmp_path


# -- composition ------------------------------------------------------------


def test_composition_is_byte_identical_when_nothing_is_overridden(cfg):
    """The frozen contract: agent block, blank line, shared block, one newline.

    Every prompt already in the repo was written against this shape. Layering
    was allowed to add a *layer*, not to reflow the bytes an agent receives.
    """
    shared = (PACKAGED_PROMPTS / SHARED_PROMPT).read_text()
    for name, s in cfg.agents.items():
        own = (PACKAGED_PROMPTS / s.prompt).read_text()
        expected = f"{own.rstrip()}\n\n{shared.strip()}\n"
        assert build_system_prompt(s, (PACKAGED_PROMPTS,)) == expected, name


def test_an_ejected_prompt_shadows_the_packaged_one(spec, overrides):
    (overrides / spec.prompt).write_text("MINE, NOT THE HOUSE ONE\n")
    prompt = build_system_prompt(spec, (overrides, PACKAGED_PROMPTS))

    assert prompt.startswith("MINE, NOT THE HOUSE ONE")
    packaged_first_line = (PACKAGED_PROMPTS / spec.prompt).read_text().splitlines()[0]
    assert packaged_first_line not in prompt


def test_overriding_the_agent_prompt_keeps_the_house_shared_rules(spec, overrides):
    (overrides / spec.prompt).write_text("MINE\n")
    prompt = build_system_prompt(spec, (overrides, PACKAGED_PROMPTS))

    assert "MINE" in prompt
    assert "Evidence or it did not happen" in prompt, "the house rules were dragged away"


def test_overriding_the_shared_rules_keeps_all_the_agent_prompts(cfg, overrides):
    """The `vice versa` half: `_shared.md` alone, without forking eight files."""
    (overrides / SHARED_PROMPT).write_text("HOUSE RULES, REWRITTEN\n")
    for name, s in cfg.agents.items():
        prompt = build_system_prompt(s, (overrides, PACKAGED_PROMPTS))
        assert "HOUSE RULES, REWRITTEN" in prompt, name
        assert "Evidence or it did not happen" not in prompt, name
        packaged_first_line = (PACKAGED_PROMPTS / s.prompt).read_text().splitlines()[0]
        assert packaged_first_line in prompt, f"{name}: lost its own prompt"


def test_each_file_resolves_independently(spec, tmp_path):
    """Agent prompt from one layer, shared rules from another, in one prompt.

    Resolving the pair from whichever single directory won first would make
    either override quietly drag the other along -- the bug this arrangement
    exists to prevent.
    """
    near, far = tmp_path / "near", tmp_path / "far"
    near.mkdir()
    far.mkdir()
    (near / spec.prompt).write_text("AGENT FROM NEAR\n")
    (far / SHARED_PROMPT).write_text("SHARED FROM FAR\n")

    prompt = build_system_prompt(spec, (near, far, PACKAGED_PROMPTS))
    assert prompt == "AGENT FROM NEAR\n\nSHARED FROM FAR\n"


def test_a_missing_prompt_is_still_an_error_not_an_empty_string(spec, overrides):
    nowhere = spec.model_copy(update={"prompt": "NOPE.md"})
    with pytest.raises(FileNotFoundError):
        build_system_prompt(nowhere, (overrides, PACKAGED_PROMPTS))


def test_a_missing_shared_file_is_an_error_not_a_silently_shorter_prompt(spec, overrides):
    """An agent running without the house rules is the failure worth shouting about."""
    with pytest.raises(FileNotFoundError):
        build_system_prompt(spec, (overrides,))


# -- <AGENT>.append.md ------------------------------------------------------


def test_an_append_lands_between_the_agent_block_and_the_shared_block(spec, overrides):
    (overrides / append_name(spec.prompt)).write_text("OUR HOUSE ADDENDUM\n")
    prompt = build_system_prompt(spec, (overrides, PACKAGED_PROMPTS))

    agent_at = prompt.index((PACKAGED_PROMPTS / spec.prompt).read_text().splitlines()[0])
    append_at = prompt.index("OUR HOUSE ADDENDUM")
    shared_at = prompt.index("Evidence or it did not happen")

    assert agent_at < append_at < shared_at, (
        "the addendum must follow the agent prompt and precede the house rules; "
        "an addendum that can displace the shared rules is an enforcement hole "
        "opened from a text file"
    )


def test_an_append_keeps_the_packaged_prompt_rather_than_replacing_it(spec, overrides):
    """The whole reason the suffix exists: no fork, so the next release lands."""
    packaged = (PACKAGED_PROMPTS / spec.prompt).read_text()
    (overrides / append_name(spec.prompt)).write_text("EXTRA\n")
    prompt = build_system_prompt(spec, (overrides, PACKAGED_PROMPTS))

    assert packaged.rstrip() in prompt
    assert "EXTRA" in prompt


def test_appends_accumulate_across_layers_broadest_first(spec, tmp_path):
    """Two layers both append; neither shadows the other, and the nearest is last."""
    near, far = tmp_path / "near", tmp_path / "far"
    near.mkdir()
    far.mkdir()
    (far / append_name(spec.prompt)).write_text("ORG WIDE\n")
    (near / append_name(spec.prompt)).write_text("THIS PROJECT\n")

    prompt = build_system_prompt(spec, (near, far, PACKAGED_PROMPTS))
    assert prompt.index("ORG WIDE") < prompt.index("THIS PROJECT")


def test_an_empty_append_changes_nothing(spec, overrides):
    """`touch CONDUIT.append.md` must not move a byte."""
    before = build_system_prompt(spec, (overrides, PACKAGED_PROMPTS))
    (overrides / append_name(spec.prompt)).write_text("\n   \n")
    assert build_system_prompt(spec, (overrides, PACKAGED_PROMPTS)) == before


def test_append_name_only_ever_targets_the_agents_own_prompt(cfg):
    names = {append_name(s.prompt) for s in cfg.agents.values()}
    assert "CONDUIT.append.md" in names
    assert SHARED_PROMPT not in names


def test_append_paths_finds_nothing_when_there_is_nothing(spec):
    assert append_paths((PACKAGED_PROMPTS,), spec.prompt) == []


# -- wiring: what the agent that actually runs is handed --------------------


def test_the_workspace_search_path_is_what_the_registry_uses(tmp_path, spec, monkeypatch):
    """`build_system_prompt` used to read `PROMPTS_DIR` unconditionally, so an
    override could be listed by the CLI and still never reach an agent."""
    project = _project(tmp_path)
    prompts = project / ".qaas" / "prompts"
    prompts.mkdir()
    (prompts / append_name(spec.prompt)).write_text("REACHES THE AGENT\n")

    monkeypatch.chdir(project)
    dirs = resolve_prompt_dirs()
    assert dirs[0] == prompts.resolve()
    assert "REACHES THE AGENT" in build_system_prompt(spec, dirs)


def test_a_dry_run_counts_the_prompt_the_real_run_would_send(spec, overrides):
    """A dry run reporting the packaged size while the run sends an override is
    worse than no dry run at all."""
    packaged = describe(spec, (PACKAGED_PROMPTS,))["prompt_chars"]
    (overrides / append_name(spec.prompt)).write_text("x" * 500 + "\n")
    assert describe(spec, (overrides, PACKAGED_PROMPTS))["prompt_chars"] > packaged + 400


# -- the CLI ----------------------------------------------------------------


def test_the_prompts_group_is_reachable_and_lists_its_three_commands(runner):
    """A group registers through `add_typer`, not `registered_commands`, so the
    suite's existing command-list assertion cannot see it."""
    top = runner.invoke(cli.app, ["--help"])
    assert top.exit_code == 0, top.output
    assert "prompts" in top.output

    group = runner.invoke(cli.app, ["prompts", "--help"])
    assert group.exit_code == 0, group.output
    for command in ("list", "eject", "diff"):
        assert command in group.output


def test_prompts_list_says_which_layer_each_prompt_came_from(runner, tmp_path, monkeypatch, spec):
    project = _project(tmp_path)
    prompts = project / ".qaas" / "prompts"
    prompts.mkdir()
    (prompts / spec.prompt).write_text("MINE\n")
    monkeypatch.chdir(project)

    result = runner.invoke(cli.app, ["prompts", "list"])
    assert result.exit_code == 0, result.output
    rows = {
        line.split("│")[1].strip(): line
        for line in result.output.splitlines()
        if line.count("│") > 3
    }
    assert "project" in rows[AGENT]
    assert "packaged" in rows["FORGE"]


def test_eject_writes_a_file_that_then_shadows_the_packaged_one(runner, tmp_path, monkeypatch, spec):
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    result = runner.invoke(cli.app, ["prompts", "eject", AGENT])
    assert result.exit_code == 0, result.output

    ejected = project / ".qaas" / "prompts" / spec.prompt
    assert ejected.read_text() == (PACKAGED_PROMPTS / spec.prompt).read_text()

    ejected.write_text("EDITED LOCALLY\n")
    assert build_system_prompt(spec, resolve_prompt_dirs()).startswith("EDITED LOCALLY")


def test_eject_refuses_to_overwrite_without_force(runner, tmp_path, monkeypatch, spec):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    assert runner.invoke(cli.app, ["prompts", "eject", AGENT]).exit_code == 0

    ejected = project / ".qaas" / "prompts" / spec.prompt
    ejected.write_text("MY WORK\n")

    refused = runner.invoke(cli.app, ["prompts", "eject", AGENT])
    assert refused.exit_code == 1, refused.output
    assert ejected.read_text() == "MY WORK\n", "an eject silently ate a local edit"

    forced = runner.invoke(cli.app, ["prompts", "eject", AGENT, "--force"])
    assert forced.exit_code == 0, forced.output
    assert ejected.read_text() == (PACKAGED_PROMPTS / spec.prompt).read_text()


def test_eject_all_skips_what_is_already_there_without_failing(runner, tmp_path, monkeypatch, cfg, spec):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    assert runner.invoke(cli.app, ["prompts", "eject", AGENT]).exit_code == 0
    (project / ".qaas" / "prompts" / spec.prompt).write_text("MY WORK\n")

    result = runner.invoke(cli.app, ["prompts", "eject", "--all"])
    assert result.exit_code == 0, result.output
    written = {p.name for p in (project / ".qaas" / "prompts").glob("*.md")}
    assert written == {s.prompt for s in cfg.agents.values()} | {SHARED_PROMPT}
    assert (project / ".qaas" / "prompts" / spec.prompt).read_text() == "MY WORK\n"


def test_eject_never_writes_inside_the_installed_package(runner, tmp_path, monkeypatch, cfg):
    """An edit inside site-packages is invisible to git and gone on the next
    upgrade. Enforced in Python, not documented in help text."""
    before = {p: p.read_bytes() for p in PACKAGED_PROMPTS.glob("*.md")}

    project = _project(tmp_path)
    monkeypatch.chdir(project)
    assert runner.invoke(cli.app, ["prompts", "eject", "--all", "--force"]).exit_code == 0

    assert {p: p.read_bytes() for p in PACKAGED_PROMPTS.glob("*.md")} == before
    for path in (project / ".qaas" / "prompts").glob("*.md"):
        assert not path.resolve().is_relative_to(package_root())

    # And the guard itself, for the case no fixture can produce: a state root
    # that resolves inside the package.
    inside = Workspace.resolve(cwd=tmp_path).__class__(
        project=None,
        config_dirs=(),
        prompt_dirs=(),
        plugin_dirs=(),
        skill_dirs=(),
        state_root=package_root(),
    )
    with pytest.raises(Exception):
        cli._eject_dir(inside)


def test_eject_rejects_a_name_no_agent_has(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(_project(tmp_path))
    result = runner.invoke(cli.app, ["prompts", "eject", "NOBODY"])
    assert result.exit_code == 1
    assert "unknown prompt" in result.output


def test_diff_is_quiet_until_something_is_edited(runner, tmp_path, monkeypatch, spec):
    project = _project(tmp_path)
    monkeypatch.chdir(project)

    clean = runner.invoke(cli.app, ["prompts", "diff"])
    assert clean.exit_code == 0, clean.output
    assert "no local prompt edits" in clean.output

    assert runner.invoke(cli.app, ["prompts", "eject", AGENT]).exit_code == 0
    still_clean = runner.invoke(cli.app, ["prompts", "diff"])
    assert "no local prompt edits" in still_clean.output, (
        "an untouched eject is not a divergence"
    )

    ejected = project / ".qaas" / "prompts" / spec.prompt
    ejected.write_text(ejected.read_text() + "\nONE EXTRA HOUSE RULE\n")
    edited = runner.invoke(cli.app, ["prompts", "diff", AGENT])
    assert edited.exit_code == 0, edited.output
    assert "+ONE EXTRA HOUSE RULE" in edited.output


def test_diff_shows_an_append_as_an_addition(runner, tmp_path, monkeypatch, spec):
    project = _project(tmp_path)
    prompts = project / ".qaas" / "prompts"
    prompts.mkdir()
    (prompts / append_name(spec.prompt)).write_text("APPENDED HOUSE LINE\n")
    monkeypatch.chdir(project)

    result = runner.invoke(cli.app, ["prompts", "diff"])
    assert result.exit_code == 0, result.output
    assert "+APPENDED HOUSE LINE" in result.output


def test_validate_still_passes_with_one_prompt_overridden(runner, tmp_path, monkeypatch, spec):
    """`validate` checked only `prompt_dirs[0]`, so overriding one prompt made it
    report the other seven as missing -- they resolve from the package fine."""
    project = _project(tmp_path)
    prompts = project / ".qaas" / "prompts"
    prompts.mkdir()
    (prompts / spec.prompt).write_text("MINE\n")
    monkeypatch.chdir(project)

    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "config ok" in result.output
