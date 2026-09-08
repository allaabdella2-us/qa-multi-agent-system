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
CONFIG = str(REPO / "config")

EXPECTED_COMMANDS = {
    "init", "targets", "doctor", "validate",
    "runs", "show", "map", "run", "score", "sweep", "tracker-check",
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
