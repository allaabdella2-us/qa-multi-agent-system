"""Where the CLI reads and writes: the project, found by walking up.

Config, `.env`, clones and prompts all walked up to the project; run state did
not. `--root` defaulted to `Path(".qaas")` relative to the working directory, so
in `proj/app` `qaas runs` said "no runs yet" about a project full of them, and a
run started there wrote a fresh, unignored `app/.qaas/`. The same family: `qaas
map` created `.qaas/system-map/` wherever it was typed, `prompts eject` outside
a project wrote a prompt nothing would read, `dashboard --config` rendered one
config and wrote overrides against another, and `qaas init .` in a repository
with its own `config/system.yaml` copied *their* file in as ours.

Offline and free: a real `Router` is replaced, and the quota probe is a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from qaas import cli
from qaas.paths import Workspace


@pytest.fixture(autouse=True)
def _no_api(monkeypatch):
    monkeypatch.setattr(cli, "_quota_preflight", lambda: None)
    # A path rather than None: `validate` reports a missing binary as a problem,
    # and the probe that would actually run it is stubbed above.
    monkeypatch.setattr(cli, "_claude_cli", lambda: "/fake/claude")


@pytest.fixture
def runner():
    return CliRunner()


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    targets = project / ".qaas" / "config" / "targets"
    targets.mkdir(parents=True)
    (project / "app").mkdir()
    (targets / "app.yaml").write_text("name: app\nroot: app\n", encoding="utf-8")
    monkeypatch.setenv("QAAS_TARGET", "app")
    return project


def _a_run(root: Path, run_id: str = "run-a") -> None:
    from qaas.store import RunStore

    store = RunStore(run_id, root=root, create=True)
    store.log("run_started", mode="pr-check", agents=["API"], wall_clock_s=900)
    store.log("run_finished", run_id=run_id, cost_usd=0.0)


# -- the state root walks up --------------------------------------------------


def test_runs_are_found_from_a_subdirectory_of_the_project(runner, tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _a_run(project / ".qaas")
    monkeypatch.chdir(project / "app")

    assert cli._state_root(None) == (project / ".qaas").resolve()
    listed = runner.invoke(cli.app, ["runs"])
    assert listed.exit_code == 0, listed.output
    assert "run-a" in listed.output and "no runs yet" not in listed.output
    shown = runner.invoke(cli.app, ["show", "run-a"])
    assert shown.exit_code == 0, shown.output
    assert not (project / "app" / ".qaas").exists()


def test_an_explicit_root_still_wins(runner, tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    _a_run(project / ".qaas")
    elsewhere = tmp_path / "elsewhere"
    _a_run(elsewhere, "run-elsewhere")
    monkeypatch.chdir(project / "app")

    result = runner.invoke(cli.app, ["runs", "--root", str(elsewhere)])
    assert "run-elsewhere" in result.output and "run-a" not in result.output


def test_a_run_started_in_a_subdirectory_writes_to_the_project(runner, tmp_path, monkeypatch):
    """It wrote an unignored `app/.qaas/` beside the project's own."""
    import qaas.router

    seen: dict[str, Path] = {}

    class _Report:
        run_id = "run-fake"
        quota_exhausted = False
        resume_command = None
        failed: list[str] = []
        stopped_early = None

        def summary(self):
            return {"run_id": self.run_id}

    class _Router:
        def __init__(self, cfg, *, root, **_k):
            seen["root"] = Path(root)

        async def run(self, mode, *, run_id=None):
            return _Report()

    monkeypatch.setattr(qaas.router, "Router", _Router)
    monkeypatch.setattr(cli, "_ensure_board", lambda cfg, **_k: None)
    project = _project(tmp_path, monkeypatch)
    monkeypatch.chdir(project / "app")

    result = runner.invoke(cli.app, ["run", "--mode", "pr-check"])
    assert result.exit_code == 0, result.output
    assert seen["root"] == (project / ".qaas").resolve()
    assert not (project / "app" / ".qaas").exists()


# -- readers do not write -----------------------------------------------------


def test_map_creates_nothing_where_it_is_typed(runner, tmp_path, monkeypatch):
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["map"])
    assert result.exit_code == 1, result.output
    assert "no system map yet" in result.output
    assert list(tmp_path.iterdir()) == []

    explicit = tmp_path / "state"
    runner.invoke(cli.app, ["map", "--root", str(explicit)])
    assert not explicit.exists()


# -- prompts eject writes where prompts are read ------------------------------


def test_eject_outside_a_project_is_refused_rather_than_written_nowhere(runner, tmp_path, monkeypatch):
    """`./.qaas/prompts/` alone does not make a project, so nothing ever read
    the file eject wrote there, and nothing said so."""
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["prompts", "eject", "API"])
    assert result.exit_code == 1, result.output
    assert "qaas init" in result.output
    assert not (tmp_path / ".qaas").exists()


def test_eject_inside_a_project_writes_the_file_every_agent_then_reads(runner, tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    monkeypatch.chdir(project / "app")
    result = runner.invoke(cli.app, ["prompts", "eject", "API"])
    assert result.exit_code == 0, result.output
    ejected = (project / ".qaas" / "prompts" / "API.md").resolve()
    assert ejected.is_file()
    assert Workspace.resolve().prompt_file("API.md").resolve() == ejected


def test_eject_with_qaas_home_and_no_project_writes_under_the_home(runner, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("QAAS_HOME", str(home))
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["prompts", "eject", "API"])
    assert result.exit_code == 0, result.output
    assert Workspace.resolve().prompt_file("API.md").resolve() == (home / "prompts" / "API.md").resolve()


# -- the dashboard honours --config -------------------------------------------


def test_the_dashboard_writes_against_the_config_it_was_given(tmp_path, monkeypatch):
    project = _project(tmp_path, monkeypatch)
    monkeypatch.chdir(project)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    dirs = cli._dashboard_kwargs(explicit)["config_dirs"]
    assert dirs[0] == explicit.resolve(), dirs


# -- init in a repository that has a config/ of its own -----------------------


def test_init_does_not_adopt_a_repositorys_own_config_as_ours(runner, tmp_path, monkeypatch):
    """Its `config/system.yaml` (`database: ...`) was copied into
    `.qaas/config/system.yaml`, and from then on `validate` failed and `run`
    died on `extra_forbidden`."""
    repo = tmp_path / "theirs"
    (repo / "config" / "targets").mkdir(parents=True)
    (repo / "config" / "system.yaml").write_text("database:\n  host: db\n")
    (repo / "config" / "targets" / "prod.yaml").write_text("replicas: 3\n")
    (repo / "main.py").write_text("x = 1\n")
    monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(repo)

    init = runner.invoke(cli.app, ["init", "."])
    assert init.exit_code == 0, init.output
    ours = (repo / ".qaas" / "config" / "system.yaml").read_text()
    assert "run_modes" in ours and "database" not in ours

    # And their `config/` is not a config layer afterwards: `prod.yaml` is not
    # a target profile, and reading it as one would fail every command.
    assert (repo / "config").resolve() not in Workspace.resolve().config_dirs
    validate = runner.invoke(cli.app, ["validate"])
    assert validate.exit_code == 0, validate.output
    targets = runner.invoke(cli.app, ["targets"])
    assert "prod" not in targets.output
