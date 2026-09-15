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
    "board", "dashboard",
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
    assert "MAPPER" in result.output
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
# router -- rather than growing a second way to decide what is under test.


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
    _scratch_target(config, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", str(config), "--repo", str(repo), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "MAPPER" in result.output

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
    _scratch_target(config, tmp_path)
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


# -- `qaas run --from-board` ------------------------------------------------
#
# The one place the board drives the system rather than recording it. A person
# drags a card into a chosen status and the next run works on exactly those
# tickets. It is a pull, not a subscription: put it on a timer and "drag a card
# and an agent picks it up" is literally true, without sixteen agents polling a
# rate-limited API for the rest of the run. Everything after the tickets are
# chosen is scheduled by ROUTER out of the ledger exactly as before -- the board
# chooses the *work*, never the order it happens in.

def _scratch_target(config: Path, root: Path, name: str = "corvid") -> Path:
    """A minimal target profile inside a scratch config, pointing at a real path.

    The suite names a target through `QAAS_TARGET`, and a named target absent
    from the config path is fatal -- it used to be silently ignored, which left
    `target_root()` pointing at whatever directory the process happened to be
    standing in while the run reported success. So a scratch config needs a
    profile, and it has to be a *scratch* one: symlinking the repository's own
    `config/targets/` in was tried and `_writable_targets_dir` wrote a generated
    profile straight back through the link into the real checkout.
    """
    targets = config / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    (root / "app").mkdir(parents=True, exist_ok=True)
    path = targets / f"{name}.yaml"
    path.write_text(
        f"name: {name}\nroot: {root}\ndescription: a scratch target\n"
        "default_branch: main\nlayout:\n  backend: [app]\n"
        "environment:\n  mode: none\nauth:\n  mode: none\n",
        encoding="utf-8",
    )
    return path


def _board_project(tmp_path: Path, *, status: str, label: str = "repo-corvid"):
    """A run holding one ticketed envelope, and a local ticket in `status`."""
    from qaas.adapters.tracker import build_tracker
    from qaas.envelope import (
        Dedupe, DefectClass, DefectEnvelope, Domain, Evidence, Impact, Location,
        Reproduction, Severity, TrackerRef,
    )
    from qaas.store import AgentResult, RunStore

    config = tmp_path / ".qaas" / "config"
    config.mkdir(parents=True)
    (config / "system.yaml").write_text((Path(CONFIG) / "system.yaml").read_text())
    (config / "agents").symlink_to(Path(CONFIG) / "agents")
    _scratch_target(config, tmp_path)

    root = tmp_path / ".qaas"
    store = RunStore("run-board", root=root, create=True)
    store.log("run_started", mode="full-loop", agents=["API"], wall_clock_s=900,
              target_root=str(tmp_path))
    envelope = DefectEnvelope(
        run_id="run-board", discovered_by="API", domain=Domain.SECURITY,
        defect_class=DefectClass.VULNERABILITY, title="Cross-tenant order read",
        summary="An org can read another org's orders.",
        location=Location(service="orders", paths=["api/app/routes/orders.py:44"]),
        evidence=[Evidence(type="log", uri="artifact://run-board/x.log")],
        reproduction=Reproduction(status="reproduced", steps=["GET /v1/orders"]),
        impact=Impact(user_facing=True, security_relevant=False),
        severity=Severity.BLOCKER, confidence=0.9, dedupe=Dedupe(),
        jira=TrackerRef(key="QAAS-1"),
    )
    store.put_envelope(envelope)
    store.put_result(AgentResult(agent="API", cost_usd=1.0))
    store.log("run_finished", run_id="run-board", cost_usd=1.0)

    tracker = build_tracker("local", root)
    issue = tracker.create_issue(
        project="QAAS", title=envelope.title, body="x",
        labels=[label, f"qaas-env-{envelope.id}"], severity="blocker",
        envelope_id=envelope.id,
    )
    tracker.transition(issue.key, status, by="a human", comment="dragged the card")
    return config, root


def _board_run(runner, tmp_path, search_for, *, card_sits_in="in_review",
               target="corvid", monkeypatch=None):
    """Put the card in one status, search for another. They are different
    things, and every test below turns on the difference."""
    config, _ = _board_project(tmp_path, status=card_sits_in)
    if monkeypatch is not None:
        monkeypatch.setenv("QAAS_TARGET", target)
        monkeypatch.chdir(tmp_path)
    return runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--config", str(config),
         "--root", str(tmp_path / ".qaas"), "--from-board", search_for, "--dry-run"],
    )


def test_from_board_picks_up_a_dragged_card(runner, tmp_path, monkeypatch):
    result = _board_run(runner, tmp_path, "in_review", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "QAAS-1" in result.output
    # And it resolves the run holding that envelope, so nobody has to know it.
    assert "run-board" in result.output


def test_from_board_matches_the_status_case_blind(runner, tmp_path, monkeypatch):
    """"Ready for Fix" is whatever casing someone typed making the column.

    Both backends match a status exactly, so asking the adapter to filter meant
    `--from-board "ready for fix"` silently found nothing on a board that had
    three cards sitting in it.
    """
    result = _board_run(runner, tmp_path, "IN_REVIEW", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "QAAS-1" in result.output


def test_from_board_ignores_a_status_nobody_is_in(runner, tmp_path, monkeypatch):
    result = _board_run(runner, tmp_path, "closed", monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    assert "no tickets in 'closed'" in result.output


def test_from_board_is_scoped_to_this_repository(runner, tmp_path, monkeypatch):
    """A shared project holds every repo's tickets; the label is what separates
    them. Without it one repository's run would fix another's defects."""
    config, root = _board_project(tmp_path, status="in_review", label="repo-elsewhere")
    monkeypatch.setenv("QAAS_TARGET", "corvid")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--config", str(config), "--root", str(root),
         "--from-board", "in_review", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "no tickets" in result.output
    assert "QAAS-1" not in result.output


def test_from_board_is_resolved_before_the_dry_run_renders(runner, tmp_path, monkeypatch):
    """`--dry-run` exists to answer "what would this do". A plan that omits
    which tickets it would work on is a rehearsal of a different run."""
    result = _board_run(runner, tmp_path, "in_review", monkeypatch=monkeypatch)
    board_line = result.output.index("from the board")
    plan_line = result.output.index("VERIFIER")
    assert board_line < plan_line, result.output
