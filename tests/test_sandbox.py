"""The OS sandbox around an agent's shell: what each agent is given, and when.

The settings themselves were checked against the bundled Claude Code by
`test_sandbox_e2e.py`; these pin the shape qaas hands it, so a refactor cannot
quietly reopen the escape hatch or drop a deny rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from support import CONFIG_SEARCH

from qaas import sandbox
from qaas.config import SandboxConfig, load_config
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore


@pytest.fixture
def cfg():
    return load_config(search=CONFIG_SEARCH)


def _ctx(cfg, agent: str, target: Path, state: Path, **sandbox_cfg) -> ToolContext:
    if sandbox_cfg:
        cfg = cfg.model_copy(update={"sandbox": SandboxConfig(**sandbox_cfg)})
    target.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        store=RunStore.new(root=state), maps=SystemMapStore(state), config=cfg,
        agent=cfg.agents[agent], target_root=target,
    )


def test_an_agent_without_a_shell_gets_no_sandbox(cfg, tmp_path):
    ctx = _ctx(cfg, "API", tmp_path / "t", tmp_path / ".qaas")
    assert sandbox.settings_for(ctx.agent, ctx, tmp_path) is None
    assert sandbox.status(ctx.agent, cfg) is None


@pytest.mark.parametrize("agent", ["FIXER", "REPRODUCER", "VERIFIER"])
def test_every_shell_agent_is_sandboxed_with_the_escape_hatch_closed(cfg, tmp_path, agent):
    """With `allowUnsandboxedCommands` left at its default, a refused command can
    be retried with `dangerouslyDisableSandbox` -- auto-approved, since Bash is
    allowlisted. Verified end to end in `test_sandbox_e2e.py`."""
    ctx = _ctx(cfg, agent, tmp_path / "t", tmp_path / ".qaas")
    settings = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")
    assert settings["enabled"] is True
    assert settings["allowUnsandboxedCommands"] is False
    assert settings["excludedCommands"] == []
    assert settings["failIfUnavailable"] is False  # `auto`
    assert settings["network"]["allowLocalBinding"] is True


def test_writes_stay_in_the_target_and_off_its_machinery(cfg, tmp_path):
    target, state = tmp_path / "t", tmp_path / ".qaas"
    ctx = _ctx(cfg, "FIXER", target, state)
    fs = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")["filesystem"]
    assert fs["allowWrite"] == [str(target.resolve()), str(tmp_path / "tmp")]
    for denied in (".git/hooks", ".git/config", ".claude", ".qaas", ".env"):
        assert str(target.resolve() / denied) in fs["denyWrite"], denied
    assert str(state.resolve()) in fs["denyWrite"]


def test_a_clone_under_the_state_root_is_not_denied_itself(cfg, tmp_path):
    """`qaas run --repo` clones into `.qaas/targets/`; denying the state root
    would deny the target. The state's own entries are denied one by one."""
    state = tmp_path / ".qaas"
    target = state / "targets" / "demo"
    ctx = _ctx(cfg, "FIXER", target, state)
    deny = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")["filesystem"]["denyWrite"]
    assert str(state.resolve()) not in deny
    assert str(state.resolve() / "runs") in deny
    assert str(state.resolve() / "memory.db") in deny


def test_credential_stores_are_unreadable(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "proj" / ".qaas"
    ctx = _ctx(cfg, "VERIFIER", tmp_path / "proj" / "app", state)
    deny = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")["filesystem"]["denyRead"]
    home = Path.home()
    for path in ("~/.ssh", "~/.config/gh", "~/.aws", "~/.npmrc", "~/.git-credentials"):
        assert str(home / path[2:]) in deny, path
    assert str(state.resolve() / ".env") in deny
    assert str(state.resolve().parent / ".env") in deny  # the project's, not the target's


def test_the_targets_own_env_stays_readable_when_it_is_the_project(cfg, tmp_path):
    """With `qaas init .` the project *is* the target, and its `.env` is the
    application's too -- a test suite may load it."""
    project = tmp_path / "proj"
    ctx = _ctx(cfg, "VERIFIER", project, project / ".qaas")
    deny = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")["filesystem"]["denyRead"]
    assert str(project.resolve() / ".env") not in deny


def test_the_network_reaches_loopback_the_target_and_nothing_else(cfg, tmp_path):
    ctx = _ctx(cfg, "REPRODUCER", tmp_path / "t", tmp_path / ".qaas", allowed_domains=["pypi.org"])
    domains = sandbox.settings_for(ctx.agent, ctx, tmp_path / "tmp")["network"]["allowedDomains"]
    for host in ("localhost", "127.0.0.1", "[::1]", "pypi.org"):
        assert host in domains
    profile = cfg.profile
    for url in (profile.environment.api_url, profile.environment.web_url):
        if url:
            from urllib.parse import urlsplit

            assert urlsplit(url).hostname in domains


def test_required_fails_closed_and_off_disables(cfg, tmp_path):
    required = _ctx(cfg, "FIXER", tmp_path / "t", tmp_path / ".qaas", mode="required")
    assert sandbox.settings_for(required.agent, required, tmp_path)["failIfUnavailable"] is True
    off = _ctx(cfg, "FIXER", tmp_path / "t2", tmp_path / ".qaas2", mode="off")
    assert sandbox.settings_for(off.agent, off, tmp_path) is None
    assert sandbox.status(off.agent, off.config).startswith("off")


@pytest.mark.parametrize(
    ("system", "which", "ok", "says"),
    [
        ("Linux", {"bwrap": "/usr/bin/bwrap", "socat": "/usr/bin/socat"}, True, "bubblewrap"),
        ("Linux", {"socat": "/usr/bin/socat"}, False, "install bubblewrap"),
        ("Windows", {}, False, "macOS and Linux only"),
    ],
)
def test_support_says_what_is_missing(monkeypatch, system, which, ok, says):
    monkeypatch.setattr(sandbox.platform, "system", lambda: system)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: which.get(name))
    available, how = sandbox.support()
    assert available is ok and says in how


def test_an_unavailable_sandbox_is_recorded_not_hidden(cfg, monkeypatch):
    monkeypatch.setattr(sandbox, "support", lambda: (False, "install bubblewrap and socat"))
    line = sandbox.status(cfg.agents["FIXER"], cfg)
    assert line.startswith("unavailable") and "bubblewrap" in line


def test_build_options_sandboxes_the_shell_and_makes_a_private_tmpdir(cfg, tmp_path):
    from qaas.registry import build_options

    ctx = _ctx(cfg, "FIXER", tmp_path / "t", tmp_path / ".qaas")
    options = build_options(ctx.agent, ctx)
    tmpdir = Path(options.env[sandbox.TMPDIR_ENV])
    try:
        assert options.sandbox and options.sandbox["enabled"] is True
        assert tmpdir.is_dir() and str(tmpdir) in options.sandbox["filesystem"]["allowWrite"]
    finally:
        sandbox.cleanup(options)
    assert not tmpdir.exists()

    api = _ctx(cfg, "API", tmp_path / "t3", tmp_path / ".qaas3")
    assert build_options(api.agent, api).sandbox is None


async def test_the_ledger_says_whether_the_shell_was_sandboxed(cfg, tmp_path, monkeypatch):
    from qaas import runner

    async def no_turn(**kwargs):
        return
        yield  # pragma: no cover - an async generator that yields nothing

    removed: list[str] = []
    real_cleanup = sandbox.cleanup

    def spy(options):
        removed.append(options.env.get(sandbox.TMPDIR_ENV))
        real_cleanup(options)

    monkeypatch.setattr(runner, "query", no_turn)
    monkeypatch.setattr(runner.sandbox, "cleanup", spy)
    ctx = _ctx(cfg, "VERIFIER", tmp_path / "t", tmp_path / ".qaas")
    await runner.run_agent(ctx.agent, ctx, "verify")
    started = next(iter(ctx.store.ledger("agent_started")))
    assert started.detail["sandbox"] == sandbox.status(ctx.agent, cfg)
    # The private temp directory is removed when the turn ends.
    assert removed and removed[0] and not Path(removed[0]).exists()


def test_validate_fails_when_a_required_sandbox_cannot_start(monkeypatch):
    from qaas import cli

    monkeypatch.setattr(sandbox, "support", lambda: (False, "install bubblewrap and socat"))
    cfg = load_config(search=CONFIG_SEARCH)
    line, fatal = cli._sandbox_line(cfg.model_copy(update={"sandbox": SandboxConfig(mode="required")}))
    assert fatal and "required" in line
    line, fatal = cli._sandbox_line(cfg)
    assert not fatal and "unsandboxed" in line
