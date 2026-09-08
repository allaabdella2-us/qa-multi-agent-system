"""Pinned facts about the installed Claude Agent SDK.

These were wrong in the published docs at the time of writing: the docs give
hook events as camelCase (`preToolUse`) and `HookMatcher(event=, handler=)`,
while the installed SDK uses PascalCase events and `HookMatcher(matcher=, hooks=)`.
Rather than trust either, this module reads the truth out of the installed
package at import time and fails loudly if it changes under us.

Guardrails do not depend on hooks working (they live in `can_use_tool`), so a
drift here degrades observability, not enforcement — but it should still be a
noisy failure rather than a silent one.
"""

from __future__ import annotations

import typing

from claude_agent_sdk import types as _sdk_types

_EVENTS: tuple[str, ...] = tuple(
    typing.get_args(arg)[0] for arg in typing.get_args(_sdk_types.HookEvent)
)

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
SUBAGENT_START = "SubagentStart"
STOP = "Stop"

_REQUIRED = (PRE_TOOL_USE, POST_TOOL_USE, STOP)


def check() -> None:
    """Raise if the SDK no longer exposes the hook events we register."""
    missing = [e for e in _REQUIRED if e not in _EVENTS]
    if missing:
        raise RuntimeError(
            f"claude-agent-sdk no longer exposes hook events {missing}; "
            f"it offers {list(_EVENTS)}. Update qaas.sdk_compat and guardrails."
        )


def mcp_tool_name(server: str, tool: str) -> str:
    """The name an MCP tool is exposed under: `mcp__<server>__<tool>`."""
    return f"mcp__{server}__{tool}"


def mcp_server_wildcard(server: str) -> str:
    """Allowlist pattern covering every tool on one server."""
    return f"mcp__{server}"


check()
