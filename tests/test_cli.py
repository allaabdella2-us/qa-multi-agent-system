"""The CLI surface: every command registered, and the offline ones runnable.

A command defined *below* the `if __name__ == "__main__"` block still registers
under the `qaas` console script — the module is imported top to bottom either
way — but is invisible to `python -m qaas.cli`, and nothing else in this suite
would have noticed. `sweep` was in exactly that position. Hence both tests: one
on the resulting command list, one on the arrangement that caused it.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from qaas import cli

REPO = Path(__file__).resolve().parents[1]
CONFIG = str(REPO / "src" / "qaas" / "defaults" / "config")

EXPECTED_COMMANDS = {
    "init", "targets", "doctor", "validate",
    "runs", "show", "trace", "map", "run", "score", "sweep", "tracker-check",
}


@pytest.fixture
def runner():
    return CliRunner()


def _command_names() -> set[str]:
    return {c.name or c.callback.__name__ for c in cli.app.registered_commands}


def test_every_command_is_registered():
    assert _command_names() == EXPECTED_COMMANDS


def test_no_command_is_defined_after_the_entry_point_guard():
    source = Path(cli.__file__).read_text()
    guard = source.index('if __name__ == "__main__":')
    trailing = source[guard:]
    assert "@app.command()" not in trailing, (
        "a command is defined after the __main__ guard; it will not be reachable "
        "through `python -m qaas.cli`"
    )


def test_help_lists_every_command(runner):
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_COMMANDS:
        assert name in result.output


def test_validate_accepts_the_shipped_config(runner):
    """`qaas validate` is the offline check the README tells people to run first."""
    result = runner.invoke(cli.app, ["validate", "--config", CONFIG])
    assert result.exit_code == 0, result.output
    assert "config ok" in result.output


def test_dry_run_renders_the_plan_without_calling_the_api(runner):
    result = runner.invoke(cli.app, ["run", "--mode", "pr-check", "--config", CONFIG, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "CARTOGRAPHER" in result.output
    assert "tools:" in result.output


def test_unknown_agent_is_rejected_before_anything_runs(runner):
    result = runner.invoke(
        cli.app, ["run", "--mode", "pr-check", "--config", CONFIG, "--only", "NOBODY", "--dry-run"]
    )
    assert result.exit_code == 1
    assert "unknown agents" in result.output


# -- `qaas run --repo` ------------------------------------------------------
#
# Phase E: a run can be pointed at a repository directly. It is sugar over
# `--target`, so what these check is that it goes through the *same* path --
# provisions a profile, resolves a target root from it, and hands that to the
# conductor -- rather than growing a second way to decide what is under test.


def _fake_repo(path: Path) -> Path:
    """A directory that `build_profile` can inspect without any network."""
    (path / "api" / "app").mkdir(parents=True)
    (path / "api" / "app" / "main.py").write_text("app = 1\n")
    (path / "README.md").write_text("# Widget\n\nA thing.\n")
    return path


def test_run_repo_provisions_a_profile_and_dry_runs(runner, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path / "widget")
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "system.yaml").write_text((Path(CONFIG) / "system.yaml").read_text())
    (config / "agents").symlink_to(Path(CONFIG) / "agents")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", str(config), "--repo", str(repo), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "CARTOGRAPHER" in result.output

    written = config / "targets" / "widget.yaml"
    assert written.is_file(), result.output
    assert "target: widget" in result.output or "widget" in result.output


def test_run_repo_is_idempotent_and_reuses_the_profile(runner, tmp_path, monkeypatch):
    """Pointing a run at the same repository twice must run twice, not fail.

    `qaas init` refuses to clobber a profile you may have corrected by hand;
    `--repo` cannot inherit that, or the second invocation of a cron line dies.
    """
    repo = _fake_repo(tmp_path / "widget")
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "system.yaml").write_text((Path(CONFIG) / "system.yaml").read_text())
    (config / "agents").symlink_to(Path(CONFIG) / "agents")
    monkeypatch.chdir(tmp_path)

    argv = ["run", "--mode", "pr-check", "--config", str(config), "--repo", str(repo), "--dry-run"]
    first = runner.invoke(cli.app, argv)
    assert first.exit_code == 0, first.output

    # Something a human might have fixed after the guess. It must survive.
    written = config / "targets" / "widget.yaml"
    written.write_text(written.read_text() + "\ndescription: hand-corrected\n")

    second = runner.invoke(cli.app, argv)
    assert second.exit_code == 0, second.output
    assert "reusing the existing profile" in second.output
    assert "hand-corrected" in written.read_text()


def test_run_repo_and_target_together_are_refused(runner, tmp_path):
    result = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", CONFIG,
         "--repo", str(tmp_path), "--target", "corvid", "--dry-run"],
    )
    assert result.exit_code == 1
    assert "two different targets" in result.output


def test_a_clone_never_lands_in_the_callers_source_tree(tmp_path, monkeypatch):
    """`init` defaulted `--clone-to` to a bare `targets/`, relative to the cwd,
    so pointing qaas at a URL from inside your own repository dropped a foreign
    checkout in the middle of it."""
    monkeypatch.chdir(tmp_path)
    root = cli._clone_root(None)
    assert root.name == "targets"
    assert root.parent.name == ".qaas", root


def test_a_generated_profile_does_not_hide_the_ones_already_there(tmp_path, monkeypatch):
    """`run --repo` writes into `.qaas/config/targets/`. Profile lookup used to
    take the first config layer that had a `targets/` at all, so the first
    generated profile made every hand-written one in `<project>/config/targets/`
    disappear from `qaas targets`, `--target` and `qaas doctor`."""
    project = tmp_path / "proj"
    (project / "config" / "targets").mkdir(parents=True)
    (project / "config" / "system.yaml").write_text(
        (Path(CONFIG) / "system.yaml").read_text()
    )
    (project / "config" / "agents").symlink_to(Path(CONFIG) / "agents")
    (project / "config" / "targets" / "handwritten.yaml").write_text(
        "name: handwritten\nroot: app\n"
    )
    generated = project / ".qaas" / "config" / "targets"
    generated.mkdir(parents=True)
    (generated / "generated.yaml").write_text("name: generated\nroot: other\n")

    monkeypatch.chdir(project)
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    assert set(cli._target_files(None)) == {"handwritten", "generated"}


def test_a_nearer_layer_shadows_a_profile_of_the_same_name(tmp_path, monkeypatch):
    """Shadowing by filename, not wholesale replacement of the directory."""
    project = tmp_path / "proj"
    (project / "config" / "targets").mkdir(parents=True)
    (project / "config" / "targets" / "app.yaml").write_text("name: app\nroot: from-project\n")
    nearer = project / ".qaas" / "config" / "targets"
    nearer.mkdir(parents=True)
    (nearer / "app.yaml").write_text("name: app\nroot: from-state\n")

    monkeypatch.chdir(project)
    found = cli._target_files(None)
    assert set(found) == {"app"}
    assert found["app"].read_text().strip().endswith("from-state")
