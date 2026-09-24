"""What stands between `qaas run` / `qaas sweep` and a dispatch.

Three holes, all found by running the 0.0.2 wheel:

  * with no target profile at all, `run` skipped its readiness block (it lived
    inside `if cfg.profile:`) and dispatched with `target_root` = the cwd;
  * `sweep`, the cron entry point, had none of `run`'s pre-flight -- no
    readiness gate, no quota probe, no board -- so a target root that did not
    exist, or sat inside an enclosing git checkout, reached `Router.run`;
  * `--dry-run` claimed to prove "every agent's options assemble" and never
    called `build_options`, so no MCP server was ever built before a paid run.

Offline and free: a real `Router` is an assertion here, and `_quota_preflight`
(which runs `claude -p ok`, an API call) is always a stub.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from qaas import cli
from support import PACKAGED_CONFIG


@pytest.fixture
def calls(monkeypatch):
    """Records which pre-flight steps ran; refuses any real dispatch."""
    import qaas.router

    seen: dict[str, int] = {"router": 0, "quota": 0, "board": 0}

    class _Refuse:
        def __init__(self, *a, **k):
            seen["router"] += 1
            raise AssertionError("a test reached a real dispatch")

    def quota():
        seen["quota"] += 1
        return seen.get("quota_answer")  # type: ignore[return-value]

    def board(cfg, **_k):
        seen["board"] += 1
        return None

    monkeypatch.setattr(qaas.router, "Router", _Refuse)
    monkeypatch.setattr(cli, "_quota_preflight", quota)
    monkeypatch.setattr(cli, "_ensure_board", board)
    monkeypatch.setattr(cli, "_claude_cli", lambda: None)
    return seen


@pytest.fixture
def runner():
    return CliRunner()


def _project(tmp_path: Path, monkeypatch, *, root: str | None = "app", name: str = "app") -> Path:
    """A scratch project whose profile, if any, points at `root`."""
    project = tmp_path / "proj"
    targets = project / ".qaas" / "config" / "targets"
    targets.mkdir(parents=True)
    (project / "app").mkdir()
    if root is not None:
        (targets / f"{name}.yaml").write_text(f"name: {name}\nroot: {root}\n", encoding="utf-8")
        monkeypatch.setenv("QAAS_TARGET", name)
    else:
        monkeypatch.delenv("QAAS_TARGET", raising=False)
    monkeypatch.chdir(project)
    return project


# -- no target profile --------------------------------------------------------


def test_a_real_run_with_no_target_profile_is_refused(runner, tmp_path, monkeypatch, calls):
    """MANUAL.md promises "no target profile loaded"; the run instead pointed
    every sandbox at the working directory and dispatched."""
    _project(tmp_path, monkeypatch, root=None)
    result = runner.invoke(cli.app, ["run", "--mode", "pr-check"])
    assert result.exit_code == 1, result.output
    assert "no target profile loaded" in result.output
    assert "qaas init" in result.output
    assert calls["router"] == 0 and calls["quota"] == 0


def test_a_rehearsal_with_no_profile_still_renders_and_says_so(runner, tmp_path, monkeypatch, calls):
    _project(tmp_path, monkeypatch, root=None)
    result = runner.invoke(cli.app, ["run", "--mode", "pr-check", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "a real run would refuse" in result.output
    assert "MAPPER" in result.output


# -- sweep has run's pre-flight -----------------------------------------------


def test_sweep_refuses_a_target_root_that_does_not_exist(runner, tmp_path, monkeypatch, calls):
    _project(tmp_path, monkeypatch, root="nowhere")
    result = runner.invoke(cli.app, ["sweep", "--mode", "pr-check"])
    assert result.exit_code == 1, result.output
    assert "not usable" in result.output and "does not exist" in result.output
    assert calls["router"] == 0


def test_sweep_refuses_a_target_inside_an_enclosing_repository(runner, tmp_path, monkeypatch, calls):
    """`QAAS_TARGET=sub`, a root inside a larger git checkout: every branch and
    commit an agent made would land on the enclosing repository."""
    project = _project(tmp_path, monkeypatch, root="sub")
    (project / "sub").mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    result = runner.invoke(cli.app, ["sweep", "--mode", "pr-check"])
    assert result.exit_code == 1, result.output
    # Whitespace-folded: Rich wraps at 80 columns, and a long temp path (CI's
    # `/tmp/pytest-of-runner/...`) put the line break inside the phrase.
    assert "not its own" in " ".join(result.output.split())
    assert calls["router"] == 0


def test_sweep_probes_the_quota_and_provisions_the_board_before_dispatch(
    runner, tmp_path, monkeypatch, calls
):
    _project(tmp_path, monkeypatch)
    calls["quota_answer"] = "You've hit your session limit · resets 5:20pm"
    result = runner.invoke(cli.app, ["sweep", "--mode", "pr-check"])
    assert result.exit_code == 1, result.output
    assert "not accepting work" in result.output
    assert calls["board"] == 1 and calls["quota"] == 1
    assert calls["router"] == 0


def test_run_and_sweep_share_one_preflight(runner, tmp_path, monkeypatch, calls):
    """Two copies of the gate is how one of them came to have none of it."""
    seen: list[str] = []
    real = cli._preflight_target

    def spy(cfg, mode, *, dry_run):
        seen.append(mode)
        return real(cfg, mode, dry_run=dry_run)

    monkeypatch.setattr(cli, "_preflight_target", spy)
    _project(tmp_path, monkeypatch, root="nowhere")
    runner.invoke(cli.app, ["run", "--mode", "pr-check"])
    runner.invoke(cli.app, ["sweep", "--mode", "nightly"])
    assert seen == ["pr-check", "nightly"]


# -- --dry-run builds the real options ----------------------------------------


def test_the_dry_run_calls_build_options_for_every_agent(runner, tmp_path, monkeypatch, calls):
    import qaas.registry

    built: list[str] = []
    real = qaas.registry.build_options

    def spy(spec, ctx, **kw):
        built.append(spec.name)
        # A throwaway store, never the project's: a rehearsal leaves nothing.
        assert not Path(ctx.store.root).is_relative_to(tmp_path / "proj")
        return real(spec, ctx, **kw)

    monkeypatch.setattr(qaas.registry, "build_options", spy)
    project = _project(tmp_path, monkeypatch)
    result = runner.invoke(cli.app, ["run", "--mode", "pr-check", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert built == ["MAPPER", "ARCHITECT", "API", "BROWSER", "DBA", "AUDITOR", "REPRODUCER", "TRIAGE"]
    assert "options assembled for 8 agent(s)" in " ".join(result.output.split())
    assert not (project / ".qaas" / "runs").exists()
    assert calls["quota"] == 0 and calls["board"] == 0


@pytest.mark.parametrize("tracker,vcs", [("local", "local"), ("jira", "github")])
def test_the_dry_run_assembles_options_without_network_or_subprocess(
    tmp_path, monkeypatch, tracker, vcs
):
    """`build_options` must stay free: no socket, no child process, for either
    backend pair. Jira is constructed (that is what validates its credentials)
    but never contacted; the GitHub backend never runs `gh` or `git`."""
    from qaas.config import load_config

    _project(tmp_path, monkeypatch)
    for name, value in {
        "JIRA_BASE_URL": "https://jira.example.invalid", "JIRA_EMAIL": "bot@example.invalid",
        "JIRA_API_TOKEN": "t" * 24, "JIRA_PROJECT_KEY": "QAAS",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = load_config().model_copy(update={"tracker": tracker, "vcs": vcs})

    def refuse(*_a, **_k):
        raise AssertionError("the dry run reached the network or spawned a process")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)

    assert cli._assemble_options(cfg, cfg.enabled_agents("full-loop")) == []


def test_a_dry_run_reports_options_that_cannot_assemble(runner, tmp_path, monkeypatch, calls):
    """A declared server naming an unset variable passed the rehearsal and
    failed the paid run, because the rehearsal never built a server."""
    project = _project(tmp_path, monkeypatch)
    config = project / ".qaas" / "config"
    system = yaml.safe_load((PACKAGED_CONFIG / "system.yaml").read_text())
    system["mcp_servers"] = {
        "extra": {"type": "http", "url": "https://mcp.example.invalid/${QAAS_TEST_UNSET_TOKEN}"}
    }
    (config / "system.yaml").write_text(yaml.safe_dump(system))
    mapper = yaml.safe_load((PACKAGED_CONFIG / "agents" / "mapper.yaml").read_text())
    mapper["mcp_servers"] = [*mapper["mcp_servers"], "extra"]
    (config / "agents").mkdir()
    (config / "agents" / "mapper.yaml").write_text(yaml.safe_dump(mapper))
    monkeypatch.delenv("QAAS_TEST_UNSET_TOKEN", raising=False)

    result = runner.invoke(cli.app, ["run", "--mode", "pr-check", "--dry-run"])
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "options failed to assemble" in flat
    assert "MAPPER" in flat and "QAAS_TEST_UNSET_TOKEN" in flat
