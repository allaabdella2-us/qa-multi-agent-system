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
from support import make_project, write_scratch_target

REPO = Path(__file__).resolve().parents[1]
CONFIG = str(REPO / "src" / "qaas" / "defaults" / "config")

EXPECTED_COMMANDS = {
    "init", "targets", "doctor", "validate",
    "runs", "show", "trace", "map", "run", "score", "sweep", "tracker-check",
    "board", "dashboard", "escalations", "answer",
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


def test_a_rehearsal_is_not_refused_for_a_danger_it_cannot_reach(runner, tmp_path, monkeypatch):
    """`--dry-run` writes nothing, so the write-damage gate must not stop it.

    Every BLOCKING_READINESS entry is justified by damage -- a root that is not
    there, or one inside a larger checkout an agent would branch. A rehearsal
    does neither, and refusing it removes the one command that answers "what
    would this run do" *before* the target has been made ready.

    This is the bundled demo's own shape, which is why it reached CI: target-app
    sits inside this repository on purpose, so on a fresh clone it has no `.git`
    and trips "not its own". The check passed on the machine it was written on
    only because that machine had since acquired a target-app/.git.
    """
    import subprocess

    project = make_project(tmp_path)
    inner = project / "app-under-test"
    write_scratch_target(project / ".qaas" / "config", inner, name="corvid")
    subprocess.run(["git", "init", "-q", str(project)], check=True)   # target is NOT its own repo
    monkeypatch.setenv("QAAS_TARGET", "corvid")
    monkeypatch.chdir(project)

    rehearsal = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", str(project / ".qaas" / "config"), "--dry-run"],
    )
    assert rehearsal.exit_code == 0, rehearsal.output
    assert "not usable" not in rehearsal.output

    # The protection itself is unchanged: a run that would really dispatch is
    # still refused, because that is the one that can rewrite someone's tree.
    real = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", str(project / ".qaas" / "config")],
    )
    assert real.exit_code == 1, real.output
    assert "not its own" in real.output


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


def test_a_clone_of_a_different_fork_is_not_reused_for_this_url(runner, tmp_path, monkeypatch):
    """The clone directory is named by the repository's *basename*.

    Every fork of `claude-code-training` therefore wants one path. Reuse was
    decided by `root.exists()` alone and the identity that matters -- the
    remote -- was never read, so passing one fork's URL printed "using existing
    clone" and pointed the whole roster at a different person's code. The path
    check in `_provision_target` cannot catch it: both sides resolve to that
    same directory, which is precisely the thing that is wrong.

    Found on the first run against a real repository, which is the argument for
    running against one.
    """
    import subprocess

    clones = tmp_path / "clones"
    theirs = clones / "shared-name"
    _fake_repo(theirs)
    subprocess.run(["git", "init", "-q", str(theirs)], check=True)
    subprocess.run(["git", "-C", str(theirs), "remote", "add", "origin",
                    "https://github.com/someone-else/shared-name.git"], check=True)

    config = tmp_path / "cfg"
    config.mkdir()
    (config / "system.yaml").write_text((Path(CONFIG) / "system.yaml").read_text())
    (config / "agents").symlink_to(Path(CONFIG) / "agents")
    _scratch_target(config, tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli.app,
        ["run", "--mode", "pr-check", "--config", str(config), "--clone-to", str(clones),
         "--repo", "https://github.com/me/shared-name.git", "--dry-run"],
    )
    assert result.exit_code == 1, result.output
    assert "is a clone of" in result.output
    assert "someone-else" in result.output


def test_the_same_repository_over_a_different_protocol_still_reuses():
    """Refusing every protocol switch would be its own bug.

    One repository is reachable as https and ssh, with and without `.git`, a
    trailing slash, or embedded credentials. None of those is a different
    repository.
    """
    same = cli._same_remote
    assert same("https://github.com/me/app.git", "https://github.com/me/app")
    assert same("https://github.com/me/app/", "https://github.com/me/app.git")
    assert same("git@github.com:me/app.git", "https://github.com/me/app")
    assert same("https://token@github.com/me/app.git", "https://github.com/me/app")
    assert not same("https://github.com/me/app", "https://github.com/you/app")


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


def test_naming_a_ticket_directly_also_finds_the_run_that_holds_it(runner, tmp_path, monkeypatch):
    """`--ticket` is the form the manual documents, and it got no resolution.

    `_run_holding_tickets` was called only inside the `--from-board` branch, so
    a bare `--ticket` left `run_id` as None. The router opened a fresh empty
    store, `_phase_verify` filtered the ticket against its zero envelopes,
    logged "unknown tickets", and the run exited 0 having dispatched nothing --
    a silent no-op that reads as a clean run in every summary it prints.
    """
    config, _ = _board_project(tmp_path, status="in_review")
    monkeypatch.setenv("QAAS_TARGET", "corvid")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--config", str(config),
         "--root", str(tmp_path / ".qaas"), "--ticket", "QAAS-1", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "run-board" in result.output


def test_a_ticket_no_run_knows_about_fails_loudly(runner, tmp_path, monkeypatch):
    """The fix cycle cannot read evidence it has no run for, so it must not start."""
    config, _ = _board_project(tmp_path, status="in_review")
    monkeypatch.setenv("QAAS_TARGET", "corvid")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--config", str(config),
         "--root", str(tmp_path / ".qaas"), "--ticket", "QAAS-999", "--dry-run"],
    )
    assert result.exit_code == 1, result.output
    assert "no envelope in any run" in result.output


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


@pytest.mark.parametrize(
    "output",
    [
        "Claude Code returned an error result: You've hit your session limit · resets 5:20pm",
        "Error: rate limit exceeded, try again later",
        "usage limit reached for this account",
    ],
)
def test_a_run_stops_before_dispatch_when_the_model_is_refusing_work(monkeypatch, output):
    """Thirteen agents walked into the same wall one at a time.

    An account session limit is not a code failure and the router handles it
    correctly — each agent escalates and the run carries on. But finding out
    thirteen times costs forty minutes and a bill to learn what one probe
    answers in three seconds. That run produced exactly one working agent.
    """
    import subprocess

    from qaas import cli

    monkeypatch.setattr(cli, "_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=output, stderr=""),
    )
    assert cli._quota_preflight() is not None


def test_a_healthy_model_lets_the_run_proceed(monkeypatch):
    import subprocess

    from qaas import cli

    monkeypatch.setattr(cli, "_claude_cli", lambda: "/fake/claude")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="ok", stderr=""),
    )
    assert cli._quota_preflight() is None


def test_a_preflight_that_cannot_run_is_not_a_reason_to_refuse(monkeypatch):
    """No binary, or a probe that times out, must not block a run. The preflight
    is an optimisation; treating its own failure as a failure would make it a
    liability."""
    from qaas import cli

    monkeypatch.setattr(cli, "_claude_cli", lambda: None)
    assert cli._quota_preflight() is None


# -- answering an escalation -------------------------------------------------
#
# `_verify_loop` ends every path in a verdict or an escalation, escalation is a
# designed terminal state, and no command could end one. The two cases that
# forced this: CORVID-7, where REVIEWER escalated a correct one-line fix on a
# product question ("applying it exposes a UI regression already filed as
# another ticket; ship now or hold?") and answering it meant calling the tracker
# adapter from a Python script; and QAAS-31, where REVIEWER escalated because
# the fix lay outside FIXER's write_paths and correctly refused to
# REQUEST_CHANGES, since demanding a change the author may not make deadlocks
# the loop.
#
# Everything below runs against the committed default (`tracker: local`) and
# touches no tracker at all: the decision lives in the run's own ledger, which
# is the only shape that behaves identically under `local` -- where "drag a
# card" means hand-editing JSON -- and under a real Jira board.

def _escalated(tmp_path, *, reason="QAAS-1: REVIEWER escalated the fix",
               agent="REVIEWER", ticket_key="QAAS-1", run_id="run-board"):
    """A run holding a ticketed envelope, blocked on a human."""
    from qaas.store import RunStore

    config, root = _board_project(tmp_path, status="in_review")
    store = RunStore(run_id, root=root, create=False)
    detail = {"reason": reason}
    if ticket_key is not None:
        detail["ticket_key"] = ticket_key
    store.log("escalation", agent=agent, **detail)
    return config, root


def _decisions(root, run_id="run-board"):
    from qaas.store import RunStore

    return [e.detail for e in RunStore(run_id, root=root, create=False).ledger("human_decision")]


def test_escalations_lists_what_is_blocked(runner, tmp_path):
    _, root = _escalated(tmp_path)
    result = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "REVIEWER escalated the fix" in result.output
    assert "qaas answer QAAS-1" in result.output, "a queue that cannot be acted on is a report"


def test_an_empty_queue_says_so(runner, tmp_path):
    _board_project(tmp_path, status="in_review")
    result = runner.invoke(cli.app, ["escalations", "--root", str(tmp_path / ".qaas")])
    assert result.exit_code == 0, result.output
    assert "nothing is waiting" in result.output


def test_answering_records_a_decision_the_next_run_reads(runner, tmp_path):
    """The whole point: the answer has to survive the process that took it."""
    _, root = _escalated(tmp_path)
    result = runner.invoke(
        cli.app,
        ["answer", "QAAS-1", "--decision", "proceed", "--root", str(root),
         "--note", "Ship it; the UI regression is tracked as QAAS-2."],
    )
    assert result.exit_code == 0, result.output
    recorded = _decisions(root)
    assert len(recorded) == 1
    assert recorded[0]["ticket_key"] == "QAAS-1"
    assert recorded[0]["decision"] == "proceed"
    assert "tracked as QAAS-2" in recorded[0]["note"]


def test_the_answer_lands_in_the_run_the_next_fix_cycle_resumes(runner, tmp_path):
    """`--ticket` and `--from-board` both resume the run holding the envelope.

    Writing the answer anywhere else puts it in a ledger the next run never
    opens, and the question is asked again with the answer sitting on disk.
    """
    from qaas.store import RunStore

    _, root = _escalated(tmp_path)
    assert cli._run_holding_tickets(root, {"QAAS-1"}) == "run-board"
    runner.invoke(
        cli.app,
        ["answer", "QAAS-1", "-d", "hold", "-n", "a human makes this edit", "--root", str(root)],
    )
    entries = list(RunStore("run-board", root=root, create=False).ledger("human_decision"))
    assert len(entries) == 1


def test_an_answered_escalation_leaves_the_queue(runner, tmp_path):
    _, root = _escalated(tmp_path)
    runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "proceed", "-n", "ship it", "--root", str(root)]
    )

    result = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "nothing is waiting" in result.output
    seen = runner.invoke(cli.app, ["escalations", "--root", str(root), "--all"])
    assert "answered" in seen.output and "ship it" in seen.output


def test_escalating_again_after_an_answer_is_waiting_again(runner, tmp_path):
    """The ledger is append-only, so position is the test.

    Pairing an answer with "any escalation for this ticket" would let last
    week's decision silently close a question asked after it.
    """
    from qaas.store import RunStore

    _, root = _escalated(tmp_path)
    runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "proceed", "-n", "ship it", "--root", str(root)]
    )
    RunStore("run-board", root=root, create=False).log(
        "escalation", agent="VERIFIER", ticket_key="QAAS-1",
        reason="QAAS-1: REGRESSED, the fix broke something else",
    )

    result = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "REGRESSED" in result.output
    assert "1 waiting" in result.output


def test_an_answer_written_into_a_later_run_still_clears_the_queue(runner, tmp_path):
    """The escalation and the answer can live in different ledgers.

    A later run that refiles the same defect holds the newest envelope, so that
    is the run the next fix cycle resumes and the run the answer is written to.
    Pairing them inside one ledger would leave the original escalation showing
    as blocked forever, with the answer sitting one file over.
    """
    from qaas.store import RunStore

    _, root = _escalated(tmp_path)
    later = RunStore("run-zzz-newer", root=root, create=True)
    later.log("run_started", mode="fix-cycle", agents=["VERIFIER"], wall_clock_s=900)
    envelope = RunStore("run-board", root=root, create=False).envelopes()[0]
    later.put_envelope(envelope.model_copy(update={"run_id": "run-zzz-newer"}))

    result = runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "proceed", "-n", "ship it", "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "run-zzz-newer" in result.output, "the answer must land where the fix cycle looks"
    assert not _decisions(root, "run-board")

    queue = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "nothing is waiting" in queue.output


def test_answering_a_ticket_nothing_is_blocked_on_is_refused(runner, tmp_path):
    """Answering ends an escalation. It is not a way to inject instructions
    into a run nobody asked a question about."""
    _, root = _escalated(tmp_path)
    result = runner.invoke(
        cli.app, ["answer", "QAAS-999", "-d", "hold", "-n", "no", "--root", str(root)]
    )
    assert result.exit_code == 1, result.output
    assert "nothing is blocked on QAAS-999" in result.output
    assert not _decisions(root)


def test_a_decision_outside_the_vocabulary_is_refused(runner, tmp_path):
    """`HumanDecision` is closed for the reason `LedgerKind` is: a misspelling
    must fail where it is typed, not become a decision every run ignores."""
    _, root = _escalated(tmp_path)
    result = runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "wont_fix", "-n", "accepted risk", "--root", str(root)]
    )
    assert result.exit_code == 1, result.output
    assert "proceed" in result.output and "hold" in result.output
    assert not _decisions(root)


def test_a_decision_is_matched_case_blind(runner, tmp_path):
    """A person types what they read. `--from-board` matches a status the same
    way and for the same reason."""
    _, root = _escalated(tmp_path)
    result = runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "PROCEED", "-n", "ship it", "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert _decisions(root)[0]["decision"] == "proceed"


def test_an_answer_with_no_reasoning_is_refused(runner, tmp_path):
    """`record_review` refuses REQUEST_CHANGES without concerns because FIXER
    gets them verbatim. An answer carrying nothing forward is the same retry
    one level up: the next run dispatches FIXER with an identical prompt."""
    _, root = _escalated(tmp_path)
    result = runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "proceed", "-n", "   ", "--root", str(root)]
    )
    assert result.exit_code == 1, result.output
    assert "no reasoning" in result.output
    assert not _decisions(root)


def test_the_latest_answer_replaces_the_one_before_it(runner, tmp_path):
    """Releasing a hold is answering again; nothing else could do it."""
    _, root = _escalated(tmp_path)
    runner.invoke(
        cli.app,
        ["answer", "QAAS-1", "-d", "hold", "-n", "wait for QAAS-2", "--root", str(root)],
    )
    again = runner.invoke(
        cli.app, ["answer", "QAAS-1", "-d", "proceed", "-n", "QAAS-2 landed", "--root", str(root)]
    )
    assert again.exit_code == 0, again.output
    assert "already carries an answer" in again.output
    assert [d["decision"] for d in _decisions(root)] == ["hold", "proceed"]


def test_an_escalation_with_no_ticket_is_listed_but_not_answerable(runner, tmp_path):
    """A capped fan-out and a failed agent escalate with no ticket. They are
    real and worth seeing, and there is nothing to hand back to the fix loop."""
    _, root = _escalated(
        tmp_path, agent="REPRODUCER", ticket_key=None,
        reason="REPRODUCER fan-out capped at 25 findings",
    )
    result = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "fan-out capped" in result.output
    assert "not answerable" in result.output
    assert "qaas answer" not in result.output


def test_the_ticket_is_recovered_from_a_ledger_written_before_the_field(runner, tmp_path):
    """`ticket_key` is in none of the ledgers already on disk, and every verify
    loop escalation leads with the key by construction. The reader recovers it;
    the writer never depends on that."""
    _, root = _escalated(tmp_path, ticket_key=None)
    result = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "qaas answer QAAS-1" in result.output


def test_show_says_how_to_answer_what_it_prints(runner, tmp_path):
    _, root = _escalated(tmp_path)
    result = runner.invoke(cli.app, ["show", "run-board", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "escalations" in result.output
    assert "qaas answer" in result.output


@pytest.mark.parametrize("content", ["", "- just\n- a list\n", "defects: [\n"])
def test_a_malformed_golden_ledger_is_an_error_not_a_traceback(tmp_path, content):
    import typer

    ledger = tmp_path / "defects.yaml"
    ledger.write_text(content)
    with pytest.raises(typer.Exit) as exited:
        cli._golden_or_exit(ledger)
    assert exited.value.exit_code == 1
