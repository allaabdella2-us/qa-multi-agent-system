"""Prove the CLI really loads the skills we declare.

This is the one failure in the system with no symptom. `registry.py` used to set
`setting_sources=["project"]`, which the SDK resolves against `options.cwd`, so
skills were found at `<cwd>/.claude/skills` -- a directory that exists only in
this checkout. A pip user got none of the 30 skills, no error was raised, and
the agents still produced findings. Past runs show 135 `Skill` invocations
across the eight agents, so what silently disappears is the severity rubric, the
dedupe strategy, the review order: every procedure the system has.

Three things were established by testing the CLI rather than reading about it,
and each one is a way to get this wrong:

  * `--plugin-dir` at a directory of bare `<skill>/SKILL.md` folders loads
    NOTHING, silently.
  * `skills/<name>/SKILL.md` with no manifest loads, but namespaced by the
    directory name -- so renaming a directory renames every skill.
  * `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md` loads as
    `<manifest name>:<skill>`. That is the layout we ship.

`marked llm` because it spawns the CLI, but it sends no prompt: `connect()`
exchanges the `initialize` control request and `get_server_info()` returns that
captured response. No model call, nothing billed.
"""

from __future__ import annotations

import json

import pytest
from support import CONFIG_SEARCH, PACKAGED_PLUGIN

from qaas.config import load_config
from qaas.paths import Workspace, plugin_name

pytestmark = pytest.mark.llm


def test_the_shipped_plugin_has_the_layout_the_cli_requires():
    """Offline. The three-way distinction above, pinned."""
    manifest = PACKAGED_PLUGIN / ".claude-plugin" / "plugin.json"
    assert manifest.is_file(), "no manifest: skills would be namespaced by directory name"
    assert json.loads(manifest.read_text()).get("name") == "qaas"
    assert (PACKAGED_PLUGIN / "skills").is_dir(), "skills must sit under skills/, not at the root"
    assert list((PACKAGED_PLUGIN / "skills").glob("*/SKILL.md")), "no skills in the plugin"
    assert plugin_name(PACKAGED_PLUGIN) == "qaas"


async def test_every_declared_skill_is_actually_loaded_by_the_cli():
    """The assertion that would have caught the original bug.

    Free: connect() spawns the CLI and exchanges `initialize`; no prompt is sent.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    ws = Workspace.resolve()
    cfg = load_config(search=CONFIG_SEARCH)
    declared = sorted({s for spec in cfg.agents.values() for s in spec.skills})
    qualified = [ws.qualify(s) for s in declared]
    assert all(qualified), f"unqualifiable: {[d for d, q in zip(declared, qualified) if not q]}"

    options = ClaudeAgentOptions(
        cwd=str(ws.project or PACKAGED_PLUGIN.parent),
        max_turns=1,
        setting_sources=[],
        plugins=[{"type": "local", "path": str(d.resolve())} for d in ws.plugin_dirs],
        skills=qualified,
    )
    async with ClaudeSDKClient(options=options) as client:
        info = await client.get_server_info()

    assert info, "no initialize response -- cannot tell what loaded"
    loaded = {c.get("name") for c in info.get("commands", []) if isinstance(c, dict)}
    missing = [q for q in qualified if q not in loaded]
    assert not missing, (
        f"{len(missing)} of {len(qualified)} declared skills were NOT loaded by the CLI: "
        f"{missing[:5]}. The agents would run without them and say nothing."
    )
