"""Ordinary user mistakes get a sentence and a non-zero exit, never a traceback.

Every test here was a reviewer running the installed 0.0.2 wheel and getting
either a ~200-line traceback (a YAML typo in a profile, `--mode nopecheck`) or,
worse, a confident exit 0 about something that does not exist (`qaas show
run-typo`, `qaas score run-typo`, `escalations --run bogus`). The rule these
hold: a mistake in *the user's* input is reported as one, in words, with the
file or value it came from.

Offline and free like the rest of the suite. Nothing here reaches a dispatch:
the autouse fixture turns a real `Router` into an assertion and the quota probe
into a stub, because `_quota_preflight` runs `claude -p ok`, which is an API call.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from qaas import cli


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    import qaas.router

    class _Refuse:
        def __init__(self, *a, **k):
            raise AssertionError("a test reached a real dispatch")

    monkeypatch.setattr(qaas.router, "Router", _Refuse)
    monkeypatch.setattr(cli, "_quota_preflight", lambda: None)
    monkeypatch.setattr(cli, "_claude_cli", lambda: None)


@pytest.fixture
def runner():
    return CliRunner()


def _flat(output: str) -> str:
    """Rich wraps long paths mid-token at 80 columns; compare without whitespace."""
    return "".join(output.split())


def _refused_cleanly(result) -> None:
    assert result.exit_code == 1, result.output
    # An unhandled exception also exits 1 under CliRunner; this is the difference.
    assert isinstance(result.exception, SystemExit), repr(result.exception)


def _project(tmp_path: Path, monkeypatch, *, profile: str | None = None, name: str = "app") -> Path:
    """A scratch qaas project: `.qaas/config/` over the packaged defaults."""
    project = tmp_path / "proj"
    targets = project / ".qaas" / "config" / "targets"
    targets.mkdir(parents=True)
    (project / "app").mkdir()
    if profile is not None:
        (targets / f"{name}.yaml").write_text(profile, encoding="utf-8")
        monkeypatch.setenv("QAAS_TARGET", name)
    else:
        monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(project)
    return project


GOOD_PROFILE = "name: app\nroot: app\nenvironment:\n  mode: none\n"


def _run_with_ticket(root: Path, key: str = "QAAS-1", run_id: str = "run-a") -> None:
    from qaas.envelope import (
        Dedupe, DefectClass, DefectEnvelope, Domain, Evidence, Impact, Location,
        Reproduction, Severity, TrackerRef,
    )
    from qaas.store import RunStore

    store = RunStore(run_id, root=root, create=True)
    store.log("run_started", mode="full-loop", agents=["API"], wall_clock_s=900)
    store.put_envelope(DefectEnvelope(
        run_id=run_id, discovered_by="API", domain=Domain.SECURITY,
        defect_class=DefectClass.VULNERABILITY, title="Cross-tenant order read",
        summary="An org can read another org's orders.",
        location=Location(service="orders", paths=["api/app/routes/orders.py:44"]),
        evidence=[Evidence(type="log", uri=f"artifact://{run_id}/x.log")],
        reproduction=Reproduction(status="reproduced", steps=["GET /v1/orders"]),
        impact=Impact(user_facing=True, security_relevant=False),
        severity=Severity.BLOCKER, confidence=0.9, dedupe=Dedupe(),
        jira=TrackerRef(key=key),
    ))
    store.log("run_finished", run_id=run_id, cost_usd=0.0)


# -- a broken profile ---------------------------------------------------------

BROKEN = {
    # An unterminated flow sequence: PyYAML reports it at end of stream.
    "yaml": ("name: app\nroot: app\nenvironment:\n  mode: [oops\n", "line"),
    # Parses, and fails the schema twice -- once with a bracketed pydantic
    # error that Rich markup used to swallow.
    "schema": ("name: app\nroot: app\nenvironment:\n  mode: sideways\nbogus: 1\n",
               "type=extra_forbidden"),
}

COMMANDS_THAT_LOAD_CONFIG = [
    ["doctor"],
    ["targets"],
    ["run", "--mode", "pr-check", "--dry-run"],
    ["score"],
    ["prompts", "list"],
    ["tracker-check"],
]


@pytest.mark.parametrize("kind", sorted(BROKEN))
@pytest.mark.parametrize("argv", COMMANDS_THAT_LOAD_CONFIG, ids=lambda a: " ".join(a))
def test_a_broken_profile_is_named_not_raised(runner, tmp_path, monkeypatch, argv, kind):
    """Only `validate` handled this; the other six printed a ~200-line
    traceback, and the YAML one named `"<unicode string>"` instead of the file."""
    text, detail = BROKEN[kind]
    _project(tmp_path, monkeypatch, profile=text)
    result = runner.invoke(cli.app, argv)
    _refused_cleanly(result)
    flat = _flat(result.output)
    assert "app.yaml" in flat, result.output
    assert "<unicodestring>" not in flat
    assert detail in flat, result.output


def test_targets_marks_the_one_broken_profile_and_lists_the_rest(runner, tmp_path, monkeypatch):
    """`targets` is how someone finds out which profile is the bad one, so one
    bad file is a row rather than the end of the listing."""
    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    (project / ".qaas" / "config" / "targets" / "other.yaml").write_text("name: [\n")
    result = runner.invoke(cli.app, ["targets"])
    _refused_cleanly(result)
    assert "app" in result.output and "invalid" in result.output
    assert "other.yaml" in _flat(result.output)


# -- a mode that does not exist -----------------------------------------------


@pytest.mark.parametrize("argv", [
    ["run", "--mode", "nopecheck", "--dry-run"],
    ["run", "--mode", "nopecheck", "--only", "API", "--dry-run"],
    ["sweep", "--mode", "nopecheck"],
], ids=["run", "run --only", "sweep"])
def test_an_unknown_mode_is_a_sentence_not_a_keyerror(runner, tmp_path, monkeypatch, argv):
    _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    result = runner.invoke(cli.app, argv)
    _refused_cleanly(result)
    assert "unknown run mode 'nopecheck'" in result.output
    assert "pr-check" in result.output, "name the modes that do exist"


# -- run ids that do not exist ------------------------------------------------


def test_show_refuses_a_run_that_does_not_exist(runner, tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    root = project / ".qaas"
    _run_with_ticket(root)
    result = runner.invoke(cli.app, ["show", "run-typo", "--root", str(root)])
    _refused_cleanly(result)
    assert "no run 'run-typo'" in result.output
    assert "run-a" in result.output, "offer the runs that do exist"
    assert not (root / "runs" / "run-typo").exists(), "a reader must not create what it reads"


def test_score_refuses_a_run_that_does_not_exist_and_saves_nothing(runner, tmp_path, monkeypatch):
    """It scored 0/21 against nothing and persisted `scores/run-typo.json`."""
    project = _project(
        tmp_path, monkeypatch, profile=GOOD_PROFILE + "ledger: defects.yaml\n"
    )
    (project / "app" / "defects.yaml").write_text("defects: []\n")
    root = project / ".qaas"
    _run_with_ticket(root)
    result = runner.invoke(cli.app, ["score", "run-typo", "--root", str(root)])
    _refused_cleanly(result)
    assert "no run 'run-typo'" in result.output
    assert not (root / "scores" / "run-typo.json").exists()


def test_escalations_for_a_named_run_that_does_not_exist_is_refused(runner, tmp_path, monkeypatch):
    """It said "nothing is waiting on you" about a run that was never there."""
    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    root = project / ".qaas"
    _run_with_ticket(root)
    result = runner.invoke(cli.app, ["escalations", "--run", "bogus", "--root", str(root)])
    _refused_cleanly(result)
    assert "nothing is waiting" not in result.output
    assert "no run 'bogus'" in result.output


# -- markup in messages -------------------------------------------------------


def test_the_ui_install_hint_keeps_its_brackets(runner, tmp_path, monkeypatch):
    """Rich read `[ui]` as a style tag and dropped it, so the hint said
    `pip install 'qaas-python'` -- the package the user already had."""
    import qaas.ui.serve
    from qaas.ui import MissingUIExtra

    def missing(*_a, **_k):
        raise MissingUIExtra()

    monkeypatch.setattr(qaas.ui.serve, "build", missing)
    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    root = project / ".qaas"
    _run_with_ticket(root)
    result = runner.invoke(cli.app, ["dashboard", "--root", str(root), "--no-open"])
    _refused_cleanly(result)
    assert "qaas-python[ui]" in result.output, result.output


# -- validate's wording -------------------------------------------------------


def test_the_budget_problem_is_one_sentence(runner, tmp_path, monkeypatch):
    """It read "...exceed the mode's cap, so its cap, so the run stops...". """
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "system.yaml").write_text(
        "project: t\nrun_modes:\n  tight:\n    trigger: manual\n"
        "    agents: [MAPPER, API]\n    max_budget_usd: 1\n"
    )
    (config / "overrides.yaml").write_text(
        "agents:\n  MAPPER: {max_budget_usd: 5}\n  API: {max_budget_usd: 5}\n"
    )
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["validate", "--config", str(config)])
    flat = " ".join(result.output.split())
    assert "exceed the mode's cap, so the run stops before it reaches API" in flat, result.output
    assert "its cap, so" not in flat


# -- tickets ------------------------------------------------------------------


def test_a_ticket_is_matched_whatever_case_it_was_typed_in(runner, tmp_path, monkeypatch):
    """`qaas answer qaas-31` upper-cased the key; `run --ticket qaas-31` did
    not, and reported that no run held it."""
    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    root = project / ".qaas"
    _run_with_ticket(root, key="QAAS-1")
    result = runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--ticket", "qaas-1", "--root", str(root), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "working from" in result.output and "run-a" in result.output


def test_from_board_with_no_target_is_refused_rather_than_unscoped(runner, tmp_path, monkeypatch):
    """With no target, `repo_label` is None and the search ran with no label --
    which both backends read as "every ticket" -- so another repository's card
    sitting in that status was picked up."""
    from qaas.adapters.tracker import build_tracker

    project = _project(tmp_path, monkeypatch)
    root = project / ".qaas"
    tracker = build_tracker("local", root)
    issue = tracker.create_issue(
        project="QAAS", title="someone else's", body="x", labels=["repo-elsewhere"],
        severity="major", envelope_id="env-x",
    )
    tracker.transition(issue.key, "in_review", by="a human", comment="dragged")

    result = runner.invoke(
        cli.app,
        ["run", "--mode", "fix-cycle", "--from-board", "in_review", "--root", str(root), "--dry-run"],
    )
    _refused_cleanly(result)
    assert "needs a target" in result.output
    assert issue.key not in result.output


def test_from_board_refuses_a_target_that_cannot_be_a_label(monkeypatch, tmp_path):
    import qaas.adapters.tracker

    def never(*_a, **_k):
        raise AssertionError("searched the tracker with no label to scope it")

    monkeypatch.setattr(qaas.adapters.tracker, "build_tracker", never)
    with pytest.raises(typer.Exit):
        cli._tickets_in_status(SimpleNamespace(target="!!!", tracker="local"), "ready", tmp_path)


def test_a_hyphenated_project_key_is_answerable(runner, tmp_path, monkeypatch):
    """`CORVID-SEC-12` is a key in a project whose own key has a hyphen -- the
    security project -- and the reader's pattern could not match it, so those
    escalations were listed as "not answerable"."""
    assert cli._TICKET_IN_REASON.match("CORVID-SEC-12: fix is outside write_paths").group(1) == "CORVID-SEC-12"
    assert cli._TICKET_IN_REASON.match("QAAS-31: x").group(1) == "QAAS-31"
    assert cli._TICKET_IN_REASON.match("REPRODUCER fan-out capped: x") is None

    from qaas.store import RunStore

    project = _project(tmp_path, monkeypatch, profile=GOOD_PROFILE)
    root = project / ".qaas"
    _run_with_ticket(root, key="CORVID-SEC-12")
    RunStore("run-a", root=root, create=False).log(
        "escalation", agent="REVIEWER", reason="CORVID-SEC-12: the fix lies outside write_paths",
    )
    queue = runner.invoke(cli.app, ["escalations", "--root", str(root)])
    assert "qaas answer CORVID-SEC-12" in queue.output, queue.output
    assert "not answerable" not in queue.output

    answered = runner.invoke(
        cli.app,
        ["answer", "corvid-sec-12", "-d", "hold", "-n", "a human makes this edit", "--root", str(root)],
    )
    assert answered.exit_code == 0, answered.output
    decisions = list(RunStore("run-a", root=root, create=False).ledger("human_decision"))
    assert [d.detail["ticket_key"] for d in decisions] == ["CORVID-SEC-12"]


# -- --version and a bare `qaas` ---------------------------------------------


def test_version_prints_the_installed_distribution(runner):
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"qaas {importlib.metadata.version('qaas-python')}"


def test_bare_qaas_prints_help_not_missing_command(runner):
    result = runner.invoke(cli.app, [])
    assert "Missing command" not in result.output
    assert "Usage" in result.output and "run" in result.output


# -- git not installed --------------------------------------------------------


def test_init_doctor_and_run_repo_survive_a_machine_without_git(runner, tmp_path, monkeypatch):
    """A plain directory needs no git at all. `_default_branch` and `git clone`
    were made safe; this holds the rest of `init`, `doctor` and `run --repo`."""
    real_run = subprocess.run

    def no_git(argv, *a, **k):
        if argv and argv[0] == "git":
            raise FileNotFoundError(2, "No such file or directory", "git")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", no_git)
    work = tmp_path / "work"
    (work / "r").mkdir(parents=True)
    (work / "r" / "app.py").write_text("x = 1\n")
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(work)

    init = runner.invoke(cli.app, ["init", "r"])
    assert init.exit_code == 0, init.output
    assert "default_branch: main" in (work / ".qaas" / "config" / "targets" / "r.yaml").read_text()

    doctor = runner.invoke(cli.app, ["doctor"])
    assert doctor.exception is None or isinstance(doctor.exception, SystemExit), repr(doctor.exception)
    assert "ready" in doctor.output

    rehearsal = runner.invoke(cli.app, ["run", "--repo", "r", "--mode", "pr-check", "--dry-run"])
    assert rehearsal.exit_code == 0, rehearsal.output
