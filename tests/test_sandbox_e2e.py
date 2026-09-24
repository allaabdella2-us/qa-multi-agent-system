"""The OS sandbox, end to end: qaas's options, the bundled Claude Code, a real shell.

No API call and no cost. The bundled CLI is pointed at `fake_anthropic.FakeAPI`,
which plays the model and asks for scripted Bash commands; the CLI runs them
under the sandbox settings `qaas.sandbox` produced, through FIXER's real
options -- guardrail hooks, MCP servers and all.

The point of doing it this way rather than asserting on the settings dict: the
settings are only as good as what Claude Code does with them, and two of them
(`allowLocalBinding`, the private `CLAUDE_CODE_TMPDIR`) exist because a first
attempt blocked the happy path in ways no unit test could see.

Skipped where the OS cannot sandbox a shell (Linux without bubblewrap, Windows)
or no Claude Code binary is available.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from fake_anthropic import FakeAPI
from support import CONFIG_SEARCH

from qaas import cli, sandbox
from qaas.config import load_config
from qaas.mcp.context import ToolContext
from qaas.registry import build_options
from qaas.runner import run_agent
from qaas.store import RunStore, SystemMapStore

pytestmark = pytest.mark.skipif(
    not sandbox.support()[0] or cli._claude_cli() is None,
    reason="this machine cannot sandbox a shell, or there is no Claude Code binary",
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A target checkout, a directory outside it, and a home with a credential."""
    target, outside, home = tmp_path / "target", tmp_path / "outside", tmp_path / "home"
    for d in (target / "src", target / "qa" / "repro", outside, home / ".config" / "gh"):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("github.com:\n  oauth_token: gho_SECRET\n")
    # A script the parsed guardrail allows -- `python <file>` is its stated
    # residual -- that writes outside the checkout. Only the sandbox stops it.
    (target / "qa" / "repro" / "escape.py").write_text(
        f"open({str(outside / 'escaped.txt')!r}, 'w').write('x')\n"
    )
    monkeypatch.setenv("HOME", str(home))  # so the deny list names this home
    return target, outside, home


async def test_the_sandbox_holds_and_the_happy_path_still_works(world, tmp_path):
    target, outside, home = world
    python = sys.executable
    commands = [
        "echo ok > qa/repro/inside.txt && cat qa/repro/inside.txt",
        "python3 qa/repro/escape.py; echo rc=$?",
        f"cat {home}/.config/gh/hosts.yml; echo rc=$?",
        "curl -s -m 5 -o /dev/null -w 'local=%{http_code}' http://127.0.0.1:{port}/health",
        "printf 'def test_tmp(tmp_path):\\n    (tmp_path / \"a\").write_text(\"x\")\\n' "
        "> qa/repro/test_tmp.py",
        f"{python} -m pytest -q -p no:cacheprovider qa/repro/test_tmp.py 2>&1 | tail -1",
        "git push origin fix/CORVID-1",
    ]
    api = FakeAPI(commands)
    api.commands = [c.replace("{port}", str(api.port)) for c in api.commands]

    cfg = load_config(search=CONFIG_SEARCH)
    spec = cfg.agents["FIXER"].model_copy(deep=True)
    spec.policy.write_paths = ["src", "qa/repro"]
    state = tmp_path / "state"
    ctx = ToolContext(
        store=RunStore.new(root=state), maps=SystemMapStore(state), config=cfg,
        agent=spec, target_root=target,
    )
    options = build_options(spec, ctx)
    options.env.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api.port}",
        "ANTHROPIC_API_KEY": "sk-ant-fake",
        "ANTHROPIC_AUTH_TOKEN": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "HOME": str(home),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    })
    options.max_turns = len(commands) + 2

    await run_agent(spec, ctx, "probe the sandbox", options=options)
    results = api.result_texts()

    # Ordinary work inside the checkout goes through.
    assert (target / "qa" / "repro" / "inside.txt").read_text() == "ok\n"
    assert any(r.strip().endswith("ok") for r in results), results
    # A script the guardrail cannot read is stopped by the kernel.
    assert not (outside / "escaped.txt").exists(), results
    # Credential stores are unreadable to code the target ships.
    assert not any("gho_SECRET" in r for r in results), results
    # The running app on localhost is reachable, and pytest's tmp_path works.
    assert any("local=200" in r for r in results), results
    assert any("1 passed" in r for r in results), results
    # The PreToolUse guardrail still sees every Bash call under the sandbox.
    denials = [e.detail for e in ctx.store.ledger("denial")]
    assert any("mcp__vcs__push" in d.get("reason", "") for d in denials), denials
    # And the run recorded that its shell was sandboxed.
    started = next(iter(ctx.store.ledger("agent_started")))
    assert started.detail.get("sandbox", "").startswith("on")
