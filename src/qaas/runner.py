"""Running one agent: build its options, stream its turn, record what it cost.

Every invocation is its own `query()`. What comes back that matters is not the
agent's prose — that is a summary for the log — but what it wrote through its
tools, plus the cost and turn count the ledger needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from qaas.config import AgentSpec
from qaas.mcp.context import ToolContext
from qaas.registry import build_options
from qaas.store import AgentResult


@dataclass
class RunOutcome:
    """What one agent invocation produced, beyond its side effects."""

    result: AgentResult
    final_text: str = ""
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.result.subtype == "success" and self.result.error is None


async def run_agent(
    spec: AgentSpec,
    ctx: ToolContext,
    task: str,
    *,
    options: ClaudeAgentOptions | None = None,
    max_budget_usd: float | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> RunOutcome:
    """Invoke one agent and record the outcome.

    Failures are captured, not raised. One agent falling over should cost the run
    that agent's findings, not the whole run — the conductor decides whether to
    retry, skip, or escalate.
    """
    options = options or build_options(spec, ctx)
    if max_budget_usd is not None:
        options.max_budget_usd = max_budget_usd
    started = time.monotonic()
    ctx.store.log("agent_started", agent=spec.name, model=spec.model, task_chars=len(task))

    before = {e.id for e in ctx.store.envelopes()}
    final_text = ""
    tool_calls = 0
    subtype = "success"
    error: str | None = None
    cost = 0.0
    turns = 0

    def emit(kind: str, **detail: Any) -> None:
        if on_event:
            on_event(kind, detail)

    try:
        async for message in query(prompt=task, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        final_text = block.text
                    elif isinstance(block, ToolUseBlock):
                        tool_calls += 1
                        emit("tool", agent=spec.name, tool=block.name)
            elif isinstance(message, ResultMessage):
                subtype = message.subtype or "success"
                cost = message.total_cost_usd or 0.0
                turns = message.num_turns or 0
                if message.is_error:
                    error = _error_text(message)
                if isinstance(message.result, str):
                    final_text = message.result
    except Exception as exc:  # noqa: BLE001 — the conductor decides what a failure means
        subtype = "failure"
        error = f"{type(exc).__name__}: {exc}"
        ctx.store.log("agent_error", agent=spec.name, error=error)

    produced = [e.id for e in ctx.store.envelopes() if e.id not in before]
    result = AgentResult(
        agent=spec.name,
        subtype=subtype,
        cost_usd=cost,
        num_turns=turns,
        duration_s=round(time.monotonic() - started, 2),
        envelope_ids=produced,
        error=error,
    )
    ctx.store.put_result(result)
    emit("finished", agent=spec.name, cost=cost, envelopes=len(produced), subtype=subtype)
    return RunOutcome(result=result, final_text=final_text.strip(), tool_calls=tool_calls)


def _error_text(message: ResultMessage) -> str:
    """A usable error string out of whichever field this SDK version populated."""
    for attr in ("errors", "terminal_reason", "stop_reason", "api_error_status"):
        value = getattr(message, attr, None)
        if value:
            return f"{attr}={value}"
    return f"result subtype={message.subtype}"
