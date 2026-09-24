"""Pinned facts about the installed Claude Agent SDK.

These were wrong in the published docs at the time of writing: the docs give
hook events as camelCase (`preToolUse`) and `HookMatcher(event=, handler=)`,
while the installed SDK uses PascalCase events and `HookMatcher(matcher=, hooks=)`.
Rather than trust either, this module reads the truth out of the installed
package at import time and fails loudly if it changes under us.

This matters more than observability. Primary enforcement lives in the
`PreToolUse` hook (`guardrails.Guardrail.pre_tool_use`), because an
`allowed_tools` entry auto-approves a tool before `can_use_tool` is consulted --
and the `Stop` hook is what holds each agent to its `must_call` contract. A hook
event renamed under us would switch both off without an error, so a drift here
must fail loudly at import.
"""

from __future__ import annotations

import typing

from claude_agent_sdk import types as _sdk_types


def _literal_values(annotation: object) -> tuple[str, ...]:
    """Every string in a `Literal[...]`, however it is nested in unions.

    `HookEvent` was a Union of single-value Literals, and this read `[0]` of
    each -- so the day the SDK writes it as one `Literal["A", "B", ...]`, an
    ordinary refactor, the import raised a bare IndexError instead of the
    helpful error below, taking every `qaas` command down with it.
    """
    if isinstance(annotation, str):
        return (annotation,)
    values: list[str] = []
    for arg in typing.get_args(annotation):
        values.extend(_literal_values(arg))
    return tuple(values)


_EVENTS: tuple[str, ...] = _literal_values(_sdk_types.HookEvent)

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
