"""Turning an AgentSpec into a runnable agent.

One agent is one top-level `query()` with its own options: its own system prompt,
its own MCP servers, its own tool allowlist, its own budget. Not a subagent of a
shared parent — that would pool the cost into one number and blur the per-agent
allowlist that §5.3 depends on.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from qaas.config import AgentSpec
from qaas.guardrails import ALWAYS_GRANTED, Guardrail
from qaas.mcp.context import ToolContext
from qaas.sdk_compat import POST_TOOL_USE, PRE_TOOL_USE, STOP, mcp_server_wildcard

PROMPTS_DIR = Path(__file__).parent / "prompts"
SHARED_PROMPT = "_shared.md"

# name in config/agents/*.yaml -> the module providing `build(ctx)`.
# Off-the-shelf servers (playwright) are stdio subprocesses, handled separately.
SDK_SERVER_MODULES: dict[str, str] = {
    "envelope": "qaas.mcp.envelope_server",
    "defect_memory": "qaas.mcp.defect_memory",
    "tracker": "qaas.mcp.tracker",
    "test_runner": "qaas.mcp.test_runner",
    "env_control": "qaas.mcp.env_control",
    "contract_diff": "qaas.mcp.contract_diff",
    "vcs": "qaas.mcp.vcs",
}

STDIO_SERVERS: dict[str, dict[str, Any]] = {
    "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--isolated", "--browser", "chromium"],
    },
}


class UnknownServer(KeyError):
    """A config names an MCP server nothing provides."""


def build_system_prompt(spec: AgentSpec, prompts_dir: Path = PROMPTS_DIR) -> str:
    """The agent's prompt, plus the house rules every agent shares.

    Kept as two files so a change to the shared rules reaches every agent at
    once, rather than being copy-pasted into six prompts that then drift.
    """
    own = spec.prompt_path(prompts_dir)
    if not own.exists():
        raise FileNotFoundError(f"{spec.name} has no prompt at {own}")
    shared = (prompts_dir / SHARED_PROMPT).read_text()
    return f"{own.read_text().rstrip()}\n\n{shared.strip()}\n"


def build_mcp_servers(spec: AgentSpec, ctx: ToolContext) -> dict[str, Any]:
    """Instantiate exactly the servers this agent declared, and no others."""
    servers: dict[str, Any] = {}
    for name in spec.mcp_servers:
        if name in SDK_SERVER_MODULES:
            module = importlib.import_module(SDK_SERVER_MODULES[name])
            servers[name] = module.build(ctx)
        elif name in STDIO_SERVERS:
            servers[name] = dict(STDIO_SERVERS[name])
        else:
            raise UnknownServer(
                f"{spec.name} declares MCP server '{name}', which is neither an "
                f"in-process server ({', '.join(sorted(SDK_SERVER_MODULES))}) nor a "
                f"known stdio server ({', '.join(sorted(STDIO_SERVERS))})."
            )
    return servers


def build_allowed_tools(spec: AgentSpec) -> list[str]:
    """The allowlist handed to the SDK.

    Servers are allowed wholesale — the server itself enforces which of its tools
    this agent may use, and it has the context to explain a refusal properly.
    """
    # ALWAYS_GRANTED is shared with the guardrail so the two cannot disagree.
    # Neither ToolSearch nor Skill is a capability grant — they load schemas and
    # instructions for things the agent already has.
    return [
        *spec.builtin_tools,
        *sorted(ALWAYS_GRANTED - set(spec.builtin_tools)),
        *(mcp_server_wildcard(s) for s in spec.mcp_servers),
    ]


class TurnRecord:
    """What an agent has actually done this turn.

    The Stop hook needs to know which tools were called; nothing else in the SDK
    tracks that for us, so we count them as they go past.
    """

    def __init__(self) -> None:
        self.called: set[str] = set()
        self.held_envelopes: int = 0
        self.stop_blocks: int = 0

    def record(self, tool_name: str | None) -> None:
        if tool_name:
            self.called.add(tool_name)

    def missing(self, required: list[str]) -> list[str]:
        return [t for t in required if t not in self.called]


def build_hooks(
    guard: Guardrail, ctx: ToolContext, record: TurnRecord | None = None
) -> dict[str, list[HookMatcher]]:
    """Observability, plus one thing `can_use_tool` structurally cannot do.

    Permissions are enforced twice, on purpose. `can_use_tool` is primary, but
    the SDK can shadow it (see `CanUseToolShadowedWarning`), so `Guardrail.
    pre_tool_use` re-runs the same `check()` as a hook. Both call one decision
    function, so they cannot disagree — the duplication is in the wiring, not
    in the policy.

    What hooks add on top is enforcement of the *output contract*. `can_use_tool`
    only ever sees calls that happen, so it can never notice the call that did
    not. The Stop hook can, and it fires while the agent still has a turn left to
    fix it — unlike the conductor, which only finds out afterwards.
    """
    record = record or TurnRecord()

    async def on_pre_tool_record(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Count what was called. Enforcement and logging are guard.pre_tool_use."""
        record.record(_field(input_data, "tool_name"))
        return {}

    async def on_post_tool(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool = _field(input_data, "tool_name")
        response = _field(input_data, "tool_response")

        if isinstance(response, dict) and response.get("isError"):
            ctx.store.log("tool_error", agent=ctx.agent.name, tool=tool, tool_use_id=tool_use_id)
            return {}

        # An envelope that was accepted but held tells the agent nothing unless
        # someone says so now. Discovering at the end that none of your findings
        # counted is too late to attach the missing evidence.
        if tool and tool.endswith("__emit_envelope"):
            structured = _structured(response)
            if structured and structured.get("fileable") is False:
                record.held_envelopes += 1
                return {
                    "systemMessage": (
                        f"That finding was recorded but is held from filing "
                        f"({record.held_envelopes} so far this run). It needs an artifact or a "
                        "failing test as evidence, and confidence at or above "
                        f"{ctx.config.thresholds.min_confidence_to_file}. Attach evidence with "
                        "put_artifact and emit it again, or leave it held deliberately."
                    )
                }
        return {}

    async def on_stop(input_data: Any, tool_use_id: str | None, context: Any) -> dict[str, Any]:
        # `stop_hook_active` is true when this hook already blocked once. Without
        # honouring it, an agent that genuinely cannot satisfy its contract loops
        # until it burns the budget.
        if _field(input_data, "stop_hook_active"):
            ctx.store.log(
                "contract_unmet",
                agent=ctx.agent.name,
                missing=record.missing(ctx.agent.must_call),
                note="allowed to stop after one block",
            )
            return {}

        missing = record.missing(ctx.agent.must_call)
        if not missing:
            return {}

        record.stop_blocks += 1
        ctx.store.log("stop_blocked", agent=ctx.agent.name, missing=missing)
        names = ", ".join(t.rsplit("__", 1)[-1] for t in missing)
        return {
            "decision": "block",
            "reason": (
                f"You have not called: {names}. That is {ctx.agent.name}'s deliverable for "
                "this task, not an optional extra — without it this invocation produced "
                "nothing the rest of the system can use. Either call it now, or if you "
                "genuinely cannot, call it with the outcome you did reach and say why."
            ),
        }

    return {
        PRE_TOOL_USE: [HookMatcher(matcher=None, hooks=[guard.pre_tool_use, on_pre_tool_record])],
        POST_TOOL_USE: [HookMatcher(matcher=None, hooks=[on_post_tool])],
        STOP: [HookMatcher(matcher=None, hooks=[on_stop])],
    }


def _structured(response: Any) -> dict[str, Any] | None:
    """The structuredContent block of an MCP tool result, whatever wraps it."""
    if isinstance(response, dict):
        inner = response.get("structuredContent")
        if isinstance(inner, dict):
            return inner
    return None


def _field(payload: Any, name: str) -> Any:
    """Hook inputs arrive as dicts or dataclasses depending on SDK version."""
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def build_options(
    spec: AgentSpec,
    ctx: ToolContext,
    *,
    extra_env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Everything one agent needs, assembled from its spec."""
    guard = Guardrail(ctx)

    env = {
        # Opus delegates readily. An unbounded subagent tree is the fastest route
        # to a surprise bill, so cap depth and width regardless of what it decides.
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "3",
    }
    env.update(extra_env or {})

    return ClaudeAgentOptions(
        system_prompt=build_system_prompt(spec),
        model=spec.model,
        effort=spec.effort,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        mcp_servers=build_mcp_servers(spec, ctx),
        allowed_tools=build_allowed_tools(spec),
        can_use_tool=guard.can_use_tool,
        hooks=build_hooks(guard, ctx),
        skills=list(spec.skills),
        cwd=str(ctx.repo_root),
        env=env,
        # "project" only, and deliberately not "user" or "local". Filesystem
        # skills are discoverable only through project settings, and project
        # settings live in the repo, so they travel with it and a run stays
        # reproducible on another machine. It is `~/.claude` that would poison a
        # run with one operator's local configuration.
        setting_sources=["project"],
        permission_mode="default",
    )


def describe(spec: AgentSpec) -> dict[str, Any]:
    """A dry-run view of what this agent would be given. No API call."""
    return {
        "agent": spec.name,
        "model": spec.model,
        "effort": spec.effort,
        "max_turns": spec.max_turns,
        "max_budget_usd": spec.max_budget_usd,
        "mcp_servers": list(spec.mcp_servers),
        "allowed_tools": build_allowed_tools(spec),
        "prompt_chars": len(build_system_prompt(spec)),
        "skills": list(spec.skills),
        "must_call": list(spec.must_call),
        "policy": spec.policy.model_dump(),
    }
