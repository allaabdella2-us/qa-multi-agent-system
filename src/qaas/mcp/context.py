"""Shared state the in-process MCP servers close over.

Every tool call lands in this process, so the servers can reach the run store,
the config and the target app directly. That is the point of building them
in-process: validation, guardrails and persistence happen where the state
already lives, with no serialisation boundary to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qaas.config import AgentSpec, SystemConfig
from qaas.store import RunStore, SystemMapStore


@dataclass
class ToolContext:
    """One agent's view of the run. Rebuilt per agent invocation."""

    store: RunStore
    maps: SystemMapStore
    config: SystemConfig
    agent: AgentSpec
    repo_root: Path
    map_version: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    #: Files this agent has modified in this invocation. Backs the §8.2 diff
    #: budget, which is counted per distinct file rather than per tool call.
    touched_files: set[str] = field(default_factory=set)

    @property
    def target_app(self) -> Path:
        return self.repo_root / self.config.target_app

    def bump(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def count(self, key: str) -> int:
        return self.counters.get(key, 0)


def ok(text: str, **structured: Any) -> dict[str, Any]:
    """A successful MCP tool result."""
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured:
        result["structuredContent"] = structured
    return result


def err(text: str) -> dict[str, Any]:
    """A failed MCP tool result.

    Tool errors are returned, not raised: the agent should read the reason and
    correct itself rather than have the turn die.
    """
    return {"content": [{"type": "text", "text": text}], "isError": True}


def handlers(tools: list) -> dict[str, Any]:
    """Map tool name -> handler. Used by tests to exercise a server directly."""
    return {t.name: t.handler for t in tools}
