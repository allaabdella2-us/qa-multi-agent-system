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
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from qaas import sandbox
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


def _check_skills_loaded(spec: AgentSpec, ctx: ToolContext, message: Any, emit) -> None:
    """Say something when the skills an agent declared did not load.

    This exists because the failure has no symptom. Skills used to be found
    through `setting_sources=["project"]`, resolved against the agent's cwd, so
    a user whose repository had no `.claude/skills/` got none of them -- no
    error, no warning, findings still produced, every procedure missing. It was
    invisible for the life of the project and only surfaced when someone tried
    to install the package.

    The CLI's init message lists what it loaded. Comparing it against what was
    asked for costs nothing and makes the next regression loud. A mismatch is
    recorded and reported rather than raised: an agent with three of its four
    skills is degraded, not broken, and killing the run would lose the work.
    """
    declared = list(spec.skills)
    if not declared:
        return
    data = getattr(message, "data", None) or {}
    loaded = {str(n) for n in (data.get("slash_commands") or [])}
    if not loaded:
        return  # nothing reported; do not cry wolf about a shape we do not know
    missing = [
        name for name in declared
        if not any(c == name or c.endswith(f":{name}") for c in loaded)
    ]
    if not missing:
        return
    ctx.store.log(
        "skills_missing", agent=spec.name, declared=declared, missing=missing,
        cwd=str(data.get("cwd") or ""),
    )
    emit("skills_missing", agent=spec.name, missing=missing)


#: How much of the task goes inline in the ledger line. Enough to tell two
#: REPRODUCER invocations apart at a glance; the artifact holds the rest.
TASK_PREVIEW_CHARS = 300


def _record_task(ctx: ToolContext, agent: str, task: str) -> dict[str, Any]:
    """Persist the instruction an agent was actually given, and reference it.

    `agent_started` recorded `task_chars=len(task)` -- the *length* of the
    prompt. So the one thing needed to explain why an agent did what it did, or
    to replay it, was the one thing the ledger threw away; REPRODUCER runs once per
    finding and its five lines were distinguishable only by character count.

    The task goes to the artifact store rather than inline because a task is
    kilobytes and `qaas trace` has to stay readable. A preview stays on the line
    so the common case needs no second lookup.

    Never raises: an unwritable artifact store must not stop the agent from
    running. Provenance degrades to the preview.
    """
    detail: dict[str, Any] = {"task_preview": task[:TASK_PREVIEW_CHARS]}
    try:
        # Numbered off what is already on disk, not off a ToolContext counter:
        # the router builds a fresh context per dispatch, so an in-memory
        # counter would restart at 1 and each REPRODUCER invocation would overwrite
        # the previous one's task. This is the bug `put_result` already had.
        existing = len(list((ctx.store.dir / "artifacts").glob(f"task-{agent}-*.md")))
        detail["task_uri"] = ctx.store.put_artifact(f"task-{agent}-{existing + 1:02d}.md", task)
    except OSError:
        pass
    return detail


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
    that agent's findings, not the whole run — the router escalates and carries
    on without it. It does not retry: see `router.py`'s module docstring for why
    that is a decision rather than an omission.
    """
    # Building the options can fail on its own — a missing prompt file
    # (FileNotFoundError), an MCP server the config names but nothing provides
    # (UnknownServer), a declared server whose ${VAR} is unset
    # (MissingServerEnv). Outside the try below, those propagated out of a
    # function whose contract is "failures are captured, not raised": the
    # router's `_gather` calls `asyncio.gather` without `return_exceptions`
    # and `run()` catches only BudgetExceeded, so one such agent aborted the
    # whole run with no `run_finished` line — while its sibling discovery
    # agents, which gather does not cancel, kept going and kept spending with
    # nothing recording their cost.
    try:
        options = options or build_options(spec, ctx)
    except Exception as exc:  # noqa: BLE001 — same contract as the query below
        error = f"{type(exc).__name__}: {exc}"
        ctx.store.log("agent_error", agent=spec.name, error=error)
        result = AgentResult(agent=spec.name, subtype="failure", error=error)
        ctx.store.put_result(result)
        return RunOutcome(result=result)

    if max_budget_usd is not None:
        options.max_budget_usd = max_budget_usd
    # Recorded per invocation, because "was this shell sandboxed" is a property
    # of the machine the run happened on, and `auto` runs unsandboxed -- and
    # says so here -- where the OS cannot.
    shell = sandbox.status(spec, ctx.config)
    ctx.store.log(
        "agent_started", agent=spec.name, model=spec.model, task_chars=len(task),
        **({"sandbox": shell} if shell else {}),
        **_record_task(ctx, spec.name, task),
    )
    try:
        return await _stream(spec, ctx, task, options, max_budget_usd, on_event)
    finally:
        # The private temp directory `build_options` made for a sandboxed
        # shell. Removed however the turn ended -- a cancellation included.
        sandbox.cleanup(options)


async def _stream(
    spec: AgentSpec,
    ctx: ToolContext,
    task: str,
    options: ClaudeAgentOptions,
    max_budget_usd: float | None,
    on_event: Callable[[str, dict[str, Any]], None] | None,
) -> RunOutcome:
    """Stream one agent's turn and record what it cost and produced."""
    started = time.monotonic()
    before = {e.id for e in ctx.store.envelopes()}
    final_text = ""
    tool_calls = 0
    subtype = "success"
    error: str | None = None
    cost = 0.0
    cost_estimated = False
    turns = 0

    def emit(kind: str, **detail: Any) -> None:
        if on_event:
            on_event(kind, detail)

    try:
        async for message in query(prompt=task, options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                _check_skills_loaded(spec, ctx, message, emit)
            elif isinstance(message, AssistantMessage):
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
    except Exception as exc:  # noqa: BLE001 — the router decides what a failure means
        subtype = "failure"
        error = f"{type(exc).__name__}: {exc}"
        ctx.store.log("agent_error", agent=spec.name, error=error)
        # `cost` is only ever assigned inside the ResultMessage branch, so a
        # stream that dropped at turn 60 of FIXER's 80 recorded $0.00 -- and
        # `budget.spend(0.0)` told the governor nothing had been spent. The money
        # is gone from the account either way, and on a resumed run the
        # under-count compounds, because `already_spent` is read back from these
        # same records. A pessimistic estimate is the right default for a
        # governor; zero is the one answer that is certainly wrong. It is flagged
        # rather than presented as measured.
        if cost == 0.0 and tool_calls > 0 and max_budget_usd is not None:
            cost = max_budget_usd
            cost_estimated = True

    # Filtered on `discovered_by`, not only on "new since I started". Discovery
    # agents run concurrently against one store, so the unfiltered diff credited
    # every envelope a *sibling* emitted to whichever agent happened to finish
    # after it -- which put another agent's findings in this one's
    # `envelope_ids`, in `agent_finished`, and in any per-agent tally built on
    # them. `envelope_server` already stamps the field; nothing read it.
    produced = [
        e.id for e in ctx.store.envelopes()
        if e.id not in before and e.discovered_by == spec.name
    ]
    result = AgentResult(
        agent=spec.name,
        subtype=subtype,
        cost_usd=cost,
        cost_estimated=cost_estimated,
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
