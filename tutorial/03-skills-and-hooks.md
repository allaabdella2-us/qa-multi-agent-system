# Where skills and hooks are wired in

This file covers the two mechanisms that shape an agent from *outside* its prompt: the 30 skills that carry procedure, and the three lifecycle hooks that enforce the output contract.

They are unrelated in purpose and adjacent in code — both are assembled in `src/qaas/registry.py`, both are handed to the SDK by `build_options`, and both have a history of failing silently. That last property is why each has more comments than code.

---

## Part 1 — Skills

### Where they live

```
src/qaas/plugin/
├── .claude-plugin/
│   └── plugin.json
└── skills/
    ├── a11y-audit/SKILL.md
    ├── adversarial-review/SKILL.md
    ├── api-surface-extraction/SKILL.md
    ...                          (30 of them)
    └── verification-protocol/SKILL.md
```

Thirty skills, each one directory with one `SKILL.md`. The manifest is four fields:

```json
{
  "name": "qaas",
  "description": "Procedures for the qaas QA agents: how to triage, reproduce, review and verify.",
  "version": "0.1.0",
  "author": {
    "name": "Alla Abdella"
  },
  "homepage": "https://github.com/allaabdella2-us/qa-multi-agent-system"
}
```

That `"name": "qaas"` is not decoration. It is the namespace every skill registers under, and the rest of this section is about why that matters.

A skill itself is a plain Markdown file with YAML frontmatter. `src/qaas/plugin/skills/severity-rubric/SKILL.md` opens:

```yaml
---
name: severity-rubric
description: >
  Score a defect's severity against the house rubric (blocker, critical, major,
  minor, trivial). TRIGGER - read BEFORE writing any severity value into an
  envelope, a ticket, or a verdict, and whenever the task mentions severity,
  priority, impact, 'how bad is this', triage scoring, or which findings matter
  most. Do NOT score from intuition or from how interesting the bug was to find;
  this skill is the only authority on severity in this system. SKIP only when no
  severity field will be written.
---
```

The `description` is written as a trigger condition, not a summary, because that is what the model matches against when deciding whether to load it. `TRIGGER` / `Do NOT` / `SKIP only when` is the house pattern across all 30.

An agent names the skills it wants in its YAML. From `src/qaas/defaults/config/agents/clerk.yaml`:

```yaml
skills: [severity-rubric, dedupe-strategy, ticket-writer, routing-rules, ownership-resolution]
```

`AgentSpec.skills` (`src/qaas/config.py:83-85`) carries the comment that explains the division of labour:

```python
    # Procedure lives in skills, role and standards live in the prompt. A skill
    # named here is preloaded; the agent can still reach others through Skill.
    skills: list[str] = Field(default_factory=list)
```

### Why a plugin and not `.claude/skills/`

The layout above is not a style choice. `packaged_plugin()` in `src/qaas/paths.py:84-98` records what was actually tested:

```python
def packaged_plugin() -> Path:
    """The skills plugin that ships in the wheel.

    Skills reach an agent as a Claude Code *plugin* (`--plugin-dir`), not through
    filesystem settings, so they travel in the package instead of depending on a
    `.claude/skills/` directory in whatever repository the user happens to be in.

    The layout is not optional and was established by testing the CLI rather
    than by reading about it. A directory of bare `<skill>/SKILL.md` folders
    loads NOTHING -- silently. A directory containing `skills/<name>/SKILL.md`
    loads, but takes its namespace from the directory name. Only
    `.claude-plugin/plugin.json` + `skills/<name>/SKILL.md` gives a stable
    namespace, and it comes from the manifest's `name`.
    """
    return package_root() / "plugin"
```

Before this, skills were found through `setting_sources=["project"]` — which resolves against the agent's **cwd**, and the agent's cwd is the target repository. So a user running `qaas` against their own app got zero skills, with no error: findings still came out, every procedure missing. That is the failure mode the whole plugin arrangement exists to remove.

`setting_sources` is now an explicit empty list, and `src/qaas/registry.py:426-439` explains why it did not simply go back to `["project"]`:

```python
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
```

Two separate traps in one comment: loading the target's settings is a supply-chain hole, and `None` is not the same as `[]` because the SDK fills in a default.

### `skill_plugins()` — which directories the CLI is told about

`src/qaas/registry.py:353-363`:

```python
def skill_plugins(ctx: ToolContext) -> list[dict[str, str]]:
    """The plugin directories to hand the CLI, project first.

    Absolute paths: `--plugin-dir` takes the value verbatim, so a relative one
    would resolve against the agent's cwd -- the target repository -- and find
    nothing.
    """
    from qaas.paths import Workspace

    ws = getattr(ctx, "workspace", None) or Workspace.resolve()
    return [{"type": "local", "path": str(d.resolve())} for d in ws.plugin_dirs]
```

`ws.plugin_dirs` is built in `Workspace.resolve` at `src/qaas/paths.py:230-237`:

```python
        # Plugin directories, highest precedence first. A project's own plugin
        # shadows the packaged one for any skill it provides.
        plugin_dirs = _existing(
            home_path / "plugin" if home_path else None,
            proj_state / "plugin" if proj_state else None,
            packaged_plugin(),
        )
        skill_dirs = _existing(*(d / "skills" for d in plugin_dirs))
```

Three layers, project first, packaged last:

| layer | directory | set by |
|---|---|---|
| org-wide | `$QAAS_HOME/plugin` | `QAAS_HOME` env var |
| project | `<project>/.qaas/plugin` | your repo |
| packaged | `src/qaas/plugin` | the wheel |

`_existing` (`src/qaas/paths.py:165-174`) drops the ones that do not exist and de-duplicates, so a fresh `pip install` with no project resolves to exactly one entry. On this checkout:

```
$ python -c "from qaas.paths import Workspace as W; print('plugin_dirs:', [str(p) for p in W.resolve().plugin_dirs])"
plugin_dirs: ['/Users/.../qa-multi-agent-system/src/qaas/plugin']
```

To override one skill, drop `<project>/.qaas/plugin/skills/severity-rubric/SKILL.md` — plus a `.claude-plugin/plugin.json` in that directory if you want a namespace of your own. You keep the other 29 from the wheel. Same layering rule as `config/agents/*.yaml` and as prompts.

### `qualified_skills()` — why the name is `qaas:severity-rubric`

This is the subtle one. `src/qaas/registry.py:366-385`:

```python
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
```

Read the two channels carefully, because the asymmetry is the entire bug:

| channel | what it receives | how it matches | bare name `severity-rubric` |
|---|---|---|---|
| `--allowedTools` | `Skill(severity-rubric)` | **literal string equality** against the invoked name, which is `qaas:severity-rubric` | ✗ never matches |
| `initialize` filter | `severity-rubric` | `name === entry \|\| name.endsWith(":" + entry)` | ✓ matches |

So a bare name gets you the worst possible state: the skill's content is loaded into the system prompt, the model reads the description, decides to use it, invokes `qaas:severity-rubric` — and the allow rule for `Skill(severity-rubric)` does not match it. Passing the qualified name satisfies both, because `qaas:severity-rubric` is literally equal for the first and ends with `:severity-rubric` for the second.

The qualification itself is `Workspace.qualify` (`src/qaas/paths.py:297-304`):

```python
    def qualify(self, skill: str) -> str | None:
        """`severity-rubric` -> `qaas:severity-rubric`, from whichever plugin
        provides it. None when nothing does -- which is a configuration error
        worth reporting, not a name to pass on and hope."""
        for plugin_dir in self.plugin_dirs:
            if (plugin_dir / "skills" / skill / "SKILL.md").is_file():
                return f"{plugin_name(plugin_dir)}:{skill}"
        return None
```

The prefix comes from `plugin_name` (`src/qaas/paths.py:101-118`), which reads the manifest's `name` and falls back to the directory name if the manifest is missing or broken. That is why `plugin.json` matters: it is where `qaas:` comes from.

Verified against the installed package:

```
$ python -c "from qaas.paths import Workspace; ws=Workspace.resolve(); print(ws.qualify('severity-rubric'), ws.qualify('nope'))"
qaas:severity-rubric None
```

Both are then handed to the SDK together, `src/qaas/registry.py:418-422`:

```python
        # Skills arrive as a plugin, not through filesystem settings, and the
        # names are qualified because the SDK matches them down two channels
        # with different rules -- see `skill_plugins` and `qualified_skills`.
        plugins=skill_plugins(ctx),
        skills=qualified_skills(spec, ctx),
```

### Two places that catch a skill that did not load

**Statically, before a run.** `qaas validate` checks every skill an agent names actually exists on disk (`src/qaas/cli.py:491-493`):

```python
        for skill in spec.skills:
            if _skill_path(skill) is None:
                problems.append(f"{name}: names skill '{skill}' with no SKILL.md")
```

`_skill_path` (`src/qaas/cli.py:45-49`) walks the same `skill_dirs` the runtime does, so validation and the run cannot disagree about where skills live. Skills on disk that no agent uses are a *note*, not a problem — see the comment at `src/qaas/cli.py:516-520`, which explains that a two-agent roster would otherwise see twenty "orphans" and a failing `qaas validate` on a fresh install.

**At runtime, from the CLI's own init message.** `_check_skills_loaded` in `src/qaas/runner.py:43-75`:

```python
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
```

Three deliberate behaviours here:

- It compares with `c == name or c.endswith(f":{name}")` — the *second* channel's rule — because it is checking what the CLI reported loading, not what the allowlist will match.
- `if not loaded: return` is a refusal to cry wolf. If the init message has a shape this code does not recognise, silence beats a false alarm.
- A mismatch is logged (`skills_missing`) and emitted to the console. It does not raise. A degraded agent still produces work worth keeping.

---

## Part 2 — Hooks

### Three events, read out of the installed SDK

`src/qaas/sdk_compat.py:20-39` does not trust the docs:

```python
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
```

`check()` runs at import time (the last line of the module). The docstring records what went wrong: the published docs gave hook events as camelCase (`preToolUse`) and `HookMatcher(event=, handler=)`, while the installed SDK uses PascalCase events and `HookMatcher(matcher=, hooks=)`. Neither was trusted; the module reads the truth out of `claude_agent_sdk.types.HookEvent` and fails loudly if it changes.

### The wiring

Everything lands in one return statement, `src/qaas/registry.py:330-333`:

```python
    return {
        PRE_TOOL_USE: [HookMatcher(matcher=None, hooks=[guard.pre_tool_use, on_pre_tool_record])],
        POST_TOOL_USE: [HookMatcher(matcher=None, hooks=[on_post_tool])],
        STOP: [HookMatcher(matcher=None, hooks=[on_stop])],
    }
```

`matcher=None` means every tool, not a subset. `PreToolUse` gets two handlers in order: enforcement first, then bookkeeping.

### `PreToolUse` — the primary enforcement point

This is the most important sentence in the codebase, and it is at the top of `src/qaas/guardrails.py:9-14`:

```
Enforcement runs in the **PreToolUse hook**, not in `can_use_tool`. This is not
a stylistic choice and it is easy to get wrong: an `allowed_tools` entry that
names a whole tool auto-approves it *before* `can_use_tool` is consulted, so a
policy implemented only in that callback is silently never applied. The SDK warns
about this shadowing, and an early version of this file had exactly that bug —
FORGE's sandbox check was dead code. The hook sees every call regardless.
```

FORGE is allowed `Write` and `Edit` (`builtin_tools: [Read, Grep, Glob, Write, Edit, Bash]`), and its policy restricts writes to `qa/repro`. Because `Write` was on the allowlist by name, it was auto-approved and `can_use_tool` was never consulted — so the `qa/repro` restriction was code that could not run.

The fix is not to remove the allowlist entry. It is to enforce in the hook, which fires for every call regardless of allowlisting. `src/qaas/guardrails.py:140-170`:

```python
    async def pre_tool_use(
        self,
        payload: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        """Primary enforcement. Runs for every tool call, shadowing or not."""
        tool_name = _hook_field(payload, "tool_name") or ""
        input_data = _hook_field(payload, "tool_input") or {}
        if not isinstance(input_data, dict):
            input_data = {}

        decision = self.check(tool_name, input_data)
        self.ctx.store.log(
            "tool_call",
            agent=self.agent.name,
            tool=tool_name,
            tool_use_id=tool_use_id,
            allowed=decision.allowed,
        )
        if decision.allowed:
            return {}

        self._record(tool_name, input_data, decision.reason, via="hook")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }
```

`can_use_tool` (`src/qaas/guardrails.py:127-138`) is kept as a second layer for calls the allowlist did *not* auto-approve. Both call the same `self.check()`, so they cannot disagree — the duplication is in the wiring, not in the policy. The denial is logged with `via="hook"` or `via="can_use_tool"` so the ledger says which belt caught it.

The second `PreToolUse` handler is pure bookkeeping (`src/qaas/registry.py:267-272`):

```python
    async def on_pre_tool_record(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        """Count what was called. Enforcement and logging are guard.pre_tool_use."""
        record.record(_field(input_data, "tool_name"))
        return {}
```

Which brings us to what it is recording into.

### `TurnRecord` — because nothing else counts tool calls

`src/qaas/registry.py:229-246`:

```python
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
```

Eighteen lines, no cleverness. A set of names, two counters, and one query — `missing(required)` — which is exactly what the Stop hook asks.

### `PostToolUse` — telling the agent an envelope was held

`src/qaas/registry.py:274-298`:

```python
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
```

`emit_envelope` returns *success* for a held finding — the envelope was recorded, it just failed `is_fileable()`. That is the right result shape (nothing was wrong with the call), and it is also why the agent would otherwise never notice. The hook reads `structuredContent.fileable` from the tool result and injects a `systemMessage` immediately, while there is still budget to call `put_artifact` and emit again.

Note the running count in the message. "That finding was held" is easy to shrug off; "3 so far this run" is not.

The `_structured` helper (`src/qaas/registry.py:337-343`) pulls `structuredContent` out of whatever wraps the response, and `_field` (`src/qaas/registry.py:346-350`) exists because "hook inputs arrive as dicts or dataclasses depending on SDK version".

### `Stop` — the one thing `can_use_tool` structurally cannot do

`build_hooks`'s own docstring (`src/qaas/registry.py:252-264`) states the argument:

```
    What hooks add on top is enforcement of the *output contract*. `can_use_tool`
    only ever sees calls that happen, so it can never notice the call that did
    not. The Stop hook can, and it fires while the agent still has a turn left to
    fix it — unlike the conductor, which only finds out afterwards.
```

The handler, `src/qaas/registry.py:300-328`:

```python
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
```

Four things worth reading twice:

1. **`stop_hook_active` is checked first.** Blocking twice does not produce a compliant agent; it produces a loop that spends the budget. One block, then the run is allowed to end and the failure is recorded as `contract_unmet`.
2. **`t.rsplit("__", 1)[-1]`** turns `mcp__envelope__record_reproduction` into `record_reproduction`. The agent knows the tool by that name.
3. **The reason ends with an escape hatch.** "call it with the outcome you did reach and say why" — a FORGE that genuinely cannot reproduce a finding is *supposed* to call `record_reproduction` with `not_reproducible`. A block with no legitimate exit invites lying.
4. **Both branches log.** `stop_blocked` and `contract_unmet` are distinct ledger events, so `qaas trace` shows whether the block worked.

### What `must_call` looks like in config

The contract is one line of YAML per agent. `src/qaas/defaults/config/agents/cartographer.yaml`:

```yaml
# The map is the deliverable. An agent that finishes without publishing one has
# not done the job, and the Stop hook says so while it can still act on that.
must_call: [mcp__envelope__put_system_map]
```

Current roster:

| agent | `must_call` | why |
|---|---|---|
| CARTOGRAPHER | `put_system_map` | the map is the deliverable |
| CONDUIT | *(none)* | finding nothing is a valid discovery outcome |
| SURFACE | *(none)* | same |
| FORGE | `record_reproduction` | one finding in, one verdict out |
| CLERK | `search_similar` | filing nothing is fine; filing without dedupe is not |
| MENDER | `open_pr` | a fix that never becomes a PR is invisible |
| ARBITER | `record_review` | the conductor routes on this decision |
| PROOF | `record_verdict`, `transition` | a verdict that leaves the ticket in review forever |

CONDUIT's YAML states the omission explicitly, which is the right way to record a deliberate blank:

```yaml
# No must_call: finding nothing is a valid and useful outcome for a discovery
# agent, and requiring an emission would manufacture findings to satisfy it.
```

`must_call` is validated at config load, not at run time. `AgentSpec._tool_budget` (`src/qaas/config.py:103-109`) refuses an entry naming a server the agent is not connected to:

```python
        for tool in self.must_call:
            server = tool.split("__")[1] if tool.startswith("mcp__") else None
            if server and server not in self.mcp_servers:
                raise ValueError(
                    f"{self.name} must_call names '{tool}' but is not connected to "
                    f"the '{server}' server; it could never satisfy that."
                )
```

An unsatisfiable contract is a config bug, and it is caught for free.

---

## Seeing it without spending money

`qaas validate` renders the `must call` column straight from config:

```
$ qaas validate
                                     Agents
┏━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ agent     ┃ layer     ┃ model     ┃ servers ┃ skills ┃ must call ┃ writes    ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━┩
│ ARBITER   │ remediat… │ claude-o… │ 4/6     │ 4      │ record_r… │ read-only │
│ CARTOGRA… │ control   │ claude-s… │ 1/6     │ 4      │ put_syst… │ read-only │
│ CLERK     │ triage    │ claude-s… │ 3/6     │ 5      │ search_s… │ tickets<… │
│ CONDUIT   │ discovery │ claude-o… │ 4/6     │ 5      │ -         │ read-only │
│ FORGE     │ triage    │ claude-o… │ 4/6     │ 4      │ record_r… │ paths:qa… │
...
config ok
```

And `qaas run --dry-run` shows the tool allowlist each agent will receive, including the `ALWAYS_GRANTED` harness tools that `build_allowed_tools` adds:

```
$ qaas run --mode pr-check --dry-run
target: corvid (compose)
pr-check — 5 agents, budget $16.00, concurrency 2
  CARTOGRAPHER   claude-sonnet-5    effort=medium  turns<=60  $2.00
    tools: Read, Grep, Glob, Agent, Skill, Task, TodoWrite, ToolSearch,
mcp__envelope
    prompt: 4817 chars
```

`Skill` and `ToolSearch` appear in every agent's list because of `ALWAYS_GRANTED` in `src/qaas/guardrails.py:54`:

```python
ALWAYS_GRANTED = frozenset({"ToolSearch", "Skill", "TodoWrite", "Task", "Agent"})
```

One constant, read by both `build_allowed_tools` (`src/qaas/registry.py:222-226`) and the guardrail, with the comment above it spelling out the stake: *"a mismatch here silently disables every skill in the system."* And `HARNESS_TOOLS` a few lines down carries the other half of the lesson — denying `ToolSearch` means an agent cannot reach any MCP tool at all, because MCP tools arrive deferred. That happened once.

---

## The rule to take away

Skills and hooks divide cleanly, and the division is the same one `CLAUDE.md` states for the whole system:

> New agent capability goes in a skill or a prompt, not in Python. New *enforcement* goes in Python, never in a prompt.

A skill is how an agent is *taught* to do something. A hook is how it is *made* to. If you find yourself writing "you must always call X" into a prompt, that belongs in `must_call` and the Stop hook. If you find yourself writing a step-by-step procedure into Python, that belongs in a `SKILL.md`.
