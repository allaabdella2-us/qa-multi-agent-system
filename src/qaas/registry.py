"""Turning an AgentSpec into a runnable agent.

One agent is one top-level `query()` with its own options: its own system prompt,
its own MCP servers, its own tool allowlist, its own budget. Not a subagent of a
shared parent — that would pool the cost into one number and blur the per-agent
allowlist that §5.3 depends on.
"""

from __future__ import annotations

import importlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from qaas.config import AgentSpec
from qaas.guardrails import ALWAYS_GRANTED, Guardrail
from qaas.mcp.context import ToolContext
from qaas.sdk_compat import POST_TOOL_USE, PRE_TOOL_USE, STOP, mcp_server_wildcard

PROMPTS_DIR = Path(__file__).parent / "prompts"
SHARED_PROMPT = "_shared.md"

#: `CONDUIT.md` -> `CONDUIT.append.md`. The suffix exists because the only other
#: way to add three house lines to a shipped prompt is to fork the whole file,
#: and a forked prompt stops receiving the next release's improvements to it --
#: silently, and in the one part of the system where silence is most expensive.
APPEND_SUFFIX = ".append.md"

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


def resolve_prompt_dirs(ctx: ToolContext | None = None) -> tuple[Path, ...]:
    """The prompt search path: overrides first, packaged last.

    Same shape as `skill_plugins` -- a ToolContext may carry a workspace, and
    anything without one asks the resolver. Prompts used to be read from
    `PROMPTS_DIR` unconditionally, which meant a `pip install` user could not
    change a single line of any prompt without editing site-packages.
    """
    from qaas.paths import Workspace

    ws = getattr(ctx, "workspace", None) or Workspace.resolve()
    return tuple(ws.prompt_dirs)


def _first_hit(dirs: Sequence[Path], relative: str) -> Path | None:
    for d in dirs:
        candidate = Path(d) / relative
        if candidate.is_file():
            return candidate
    return None


def append_name(prompt: str) -> str:
    """`CONDUIT.md` -> `CONDUIT.append.md`, keeping any subdirectory."""
    return str(PurePosixPath(prompt).with_suffix("")) + APPEND_SUFFIX


def append_paths(dirs: Sequence[Path], prompt: str) -> list[Path]:
    """Every `<AGENT>.append.md` on the search path, broadest layer first.

    Not first-hit-wins: appends accumulate rather than shadow, so an
    organisation-wide `QAAS_HOME` addendum and a project's own both apply. They
    are ordered lowest-precedence first so the nearest layer speaks last, which
    is both how a reader expects the specific to follow the general and how a
    model weights the end of a block.
    """
    name = append_name(prompt)
    found: list[Path] = []
    for d in reversed(list(dirs)):
        candidate = Path(d) / name
        if candidate.is_file():
            found.append(candidate)
    return found


def build_system_prompt(
    spec: AgentSpec, prompt_dirs: Sequence[Path] | None = None
) -> str:
    """The agent's prompt, its local addenda, and the house rules every agent shares.

    Kept as separate files so a change to the shared rules reaches every agent at
    once, rather than being copy-pasted into six prompts that then drift.

    Each file is resolved first-hit-wins **independently**: overriding
    `CONDUIT.md` keeps the house `_shared.md`, and replacing `_shared.md` keeps
    all eight agent prompts. Resolving the pair from one winning directory would
    make either override drag the other along.

    Order is agent, then addenda, then shared: the house rules are the last word,
    and an addendum that could displace them would be an enforcement hole opened
    from a text file.
    """
    dirs = tuple(prompt_dirs) if prompt_dirs is not None else resolve_prompt_dirs()
    own = _first_hit(dirs, spec.prompt)
    if own is None:
        where = ", ".join(str(d) for d in dirs) or "(no prompt directories)"
        raise FileNotFoundError(f"{spec.name} has no prompt '{spec.prompt}' in: {where}")
    shared = _first_hit(dirs, SHARED_PROMPT)
    if shared is None:
        where = ", ".join(str(d) for d in dirs) or "(no prompt directories)"
        raise FileNotFoundError(f"no {SHARED_PROMPT} in: {where}")

    blocks = [own.read_text().rstrip()]
    # An empty addendum contributes nothing rather than a stray blank block --
    # `touch CONDUIT.append.md` must not change a single byte of the prompt.
    blocks += [t for p in append_paths(dirs, spec.prompt) if (t := p.read_text().strip())]
    blocks.append(shared.read_text().strip())
    return "\n\n".join(blocks) + "\n"


class MissingServerEnv(RuntimeError):
    """A declared server references an environment variable that is not set."""


def expand_env(value: str, *, where: str) -> str:
    """Substitute `${VAR}` from the environment, loudly.

    The CLI would do this itself -- `--mcp-config` is parsed with
    `expandVars` on -- but two things argue for doing it here. An unset
    variable becomes an empty string down there, so a missing token surfaces
    much later as an unexplained auth failure rather than as the missing token
    it is. And it is an implementation detail of a vendored binary found by
    reading it, not a documented contract; `sdk_compat.py` exists because this
    project does not build on those.

    Expanding here is idempotent with respect to the CLI: a value with no `${`
    left in it is passed through unchanged.
    """
    def replace(match: "re.Match[str]") -> str:
        var = match.group(1)
        got = os.environ.get(var)
        if got is None:
            raise MissingServerEnv(
                f"{where} references ${{{var}}} and it is not set. "
                "Export it, or remove the reference -- a credential belongs in "
                "the environment, never in a config file."
            )
        return got

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def _declared_server(name: str, spec: Any) -> dict[str, Any]:
    """Turn a user's YAML declaration into the dict the SDK expects."""
    payload = spec.model_dump(exclude_none=True)
    where = f"MCP server '{name}'"
    for key in ("command", "url"):
        if key in payload:
            payload[key] = expand_env(str(payload[key]), where=where)
    if "args" in payload:
        payload["args"] = [expand_env(str(a), where=where) for a in payload["args"]]
    for key in ("env", "headers"):
        if key in payload:
            payload[key] = {k: expand_env(str(v), where=where) for k, v in payload[key].items()}
    return payload


def build_mcp_servers(spec: AgentSpec, ctx: ToolContext) -> dict[str, Any]:
    """Instantiate exactly the servers this agent declared, and no others.

    Config-declared servers resolve FIRST, so a project can override a built-in
    -- the bundled Playwright entry is hardcoded down to `--browser chromium`,
    and someone testing Firefox should not have to fork the package to say so.
    """
    declared = dict(getattr(ctx.config, "mcp_servers", {}) or {})
    servers: dict[str, Any] = {}
    for name in spec.mcp_servers:
        if name in declared:
            servers[name] = _declared_server(name, declared[name])
        elif name in SDK_SERVER_MODULES:
            module = importlib.import_module(SDK_SERVER_MODULES[name])
            servers[name] = module.build(ctx)
        elif name in STDIO_SERVERS:
            servers[name] = dict(STDIO_SERVERS[name])
        else:
            raise UnknownServer(
                f"{spec.name} declares MCP server '{name}', which is neither an "
                f"in-process server ({', '.join(sorted(SDK_SERVER_MODULES))}), a "
                f"known stdio server ({', '.join(sorted(STDIO_SERVERS))}), nor "
                f"declared under `mcp_servers:` in system.yaml "
                f"({', '.join(sorted(declared)) or 'nothing declared'})."
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


def skill_plugins(ctx: ToolContext) -> list[dict[str, str]]:
    """The plugin directories to hand the CLI, project first.

    Absolute paths: `--plugin-dir` takes the value verbatim, so a relative one
    would resolve against the agent's cwd -- the target repository -- and find
    nothing.
    """
    from qaas.paths import Workspace

    ws = getattr(ctx, "workspace", None) or Workspace.resolve()
    return [{"type": "local", "path": str(d.resolve())} for d in ws.plugin_dirs]


def qualified_skills(spec: AgentSpec, ctx: ToolContext) -> list[str]:
    """This agent's skills, namespaced by the plugin that provides each.

    The qualification is load-bearing. A skill name travels to the CLI down two
    channels that match differently: the SDK turns each into a `Skill(<name>)`
    entry on `--allowedTools`, matched **literally** against whatever the model
    invokes, while the `initialize` request filters system-prompt content with
    `name === entry || name.endsWith(":" + entry)`. Plugin skills register as
    `qaas:severity-rubric`, so a bare `severity-rubric` satisfies the second
    channel and not the first -- the skill loads, and its allow rule never
    matches. Passing the qualified name makes both agree.

    A skill no plugin provides is dropped rather than passed through. The Stop
    hook and `qaas validate` both report the real problem; inventing a name that
    can never resolve just moves the failure somewhere quieter.
    """
    from qaas.paths import Workspace

    ws = getattr(ctx, "workspace", None) or Workspace.resolve()
    return [q for q in (ws.qualify(name) for name in spec.skills) if q]


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
        # Through the workspace, not `PROMPTS_DIR`: a user's `.qaas/prompts/`
        # override has to reach the agent that actually runs, not just the one
        # `qaas prompts list` describes.
        system_prompt=build_system_prompt(spec, resolve_prompt_dirs(ctx)),
        model=spec.model,
        effort=spec.effort,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        mcp_servers=build_mcp_servers(spec, ctx),
        allowed_tools=build_allowed_tools(spec),
        can_use_tool=guard.can_use_tool,
        hooks=build_hooks(guard, ctx),
        # Skills arrive as a plugin, not through filesystem settings, and the
        # names are qualified because the SDK matches them down two channels
        # with different rules -- see `skill_plugins` and `qualified_skills`.
        plugins=skill_plugins(ctx),
        skills=qualified_skills(spec, ctx),
        cwd=str(ctx.target_root),
        env=env,
        # Load NOTHING from the filesystem. This was `["project"]`, defended on
        # reproducibility grounds -- project settings live in the repo, so they
        # travel with it. That reasoning held only while `cwd` was *our* repo.
        #
        # `cwd` is the target now, and with `qaas run --repo <url>` it can be a
        # repository cloned seconds earlier from a URL someone pasted. "project"
        # means: load that repository's `.claude/settings.json`, its hooks, its
        # permission rules and its MCP servers, into a process holding Anthropic
        # credentials, JIRA_API_TOKEN and GitHub auth. A QA tool that executes
        # the configuration of the code it is inspecting is a supply-chain hole.
        #
        # It must be an explicit `[]`, not None: `_apply_skills_defaults` in the
        # SDK substitutes ["user", "project"] whenever setting_sources is None
        # and skills is a list.
        setting_sources=[],
        permission_mode="default",
    )


def describe(
    spec: AgentSpec, prompt_dirs: Sequence[Path] | None = None
) -> dict[str, Any]:
    """A dry-run view of what this agent would be given. No API call.

    `prompt_dirs` so the dry run counts the prompt the real run would send. A
    dry run that silently reports the packaged prompt while the run sends an
    overridden one is worse than no dry run.
    """
    return {
        "agent": spec.name,
        "model": spec.model,
        "effort": spec.effort,
        "max_turns": spec.max_turns,
        "max_budget_usd": spec.max_budget_usd,
        "mcp_servers": list(spec.mcp_servers),
        "allowed_tools": build_allowed_tools(spec),
        "prompt_chars": len(build_system_prompt(spec, prompt_dirs)),
        "skills": list(spec.skills),
        "must_call": list(spec.must_call),
        "policy": spec.policy.model_dump(),
    }
