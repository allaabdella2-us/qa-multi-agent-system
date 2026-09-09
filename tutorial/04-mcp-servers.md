# The seven in-process servers, and adding your own

This file covers how `qaas` gives agents their tools: seven MCP servers that run inside the Python process, one stdio subprocess, the shared `ToolContext` they all close over, and how to declare a server of your own without touching the package.

---

## What an MCP server is here

Everything an agent does that is specific to this system — emitting a finding, running a test suite, filing a ticket, cutting a branch — goes through an MCP tool, not through Bash or a file write. Tools are the API surface between a model and this codebase, and they are where the rules live.

Seven of the eight servers are built with `create_sdk_mcp_server()` and run **in the same Python process** as the conductor. There is no subprocess, no socket, no serialisation boundary. `src/qaas/mcp/context.py:1-7` states why:

```python
"""Shared state the in-process MCP servers close over.

Every tool call lands in this process, so the servers can reach the run store,
the config and the target app directly. That is the point of building them
in-process: validation, guardrails and persistence happen where the state
already lives, with no serialisation boundary to reason about.
"""
```

The eighth, Playwright, is an off-the-shelf stdio subprocess — because it drives a real browser and there is nothing to be gained by reimplementing it.

---

## `ToolContext` — the one object every server closes over

`src/qaas/mcp/context.py:19-48`:

```python
@dataclass
class ToolContext:
    """One agent's view of the run. Rebuilt per agent invocation."""

    store: RunStore
    maps: SystemMapStore
    config: SystemConfig
    agent: AgentSpec

    #: The application under test, on disk. Named `repo_root` once, and that
    #: name was the bug: it read as "the qaas checkout", it was filled with
    #: `Path.cwd()`, and every consumer -- the write-path allowlist, the test
    #: runner's cwd, the vcs sandbox, the SDK subprocess cwd -- actually wanted
    #: the target. That only coincided while the target sat inside the qaas
    #: checkout, which is true of the bundled demo and of nothing else.
    #: Comes from `SystemConfig.target_root()`, i.e. the profile.
    target_root: Path
    map_version: str | None = None
    counters: dict[str, int] = field(default_factory=dict)

    #: Files this agent has modified in this invocation. Backs the §8.2 diff
    #: budget, which is counted per distinct file rather than per tool call.
    touched_files: set[str] = field(default_factory=set)

    def bump(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def count(self, key: str) -> int:
        return self.counters.get(key, 0)
```

Read the fields as a list of what a tool is allowed to know:

| field | what it gives a tool |
|---|---|
| `store` | the run's ledger, envelopes, artifacts, results |
| `maps` | the versioned system map, pinned per run |
| `config` | thresholds, tracker/vcs backend, run modes |
| `agent` | **who is calling** — name, policy, servers, `must_call` |
| `target_root` | the application under test, on disk |
| `map_version` | the map version pinned for this run |
| `counters` | per-run caps: findings emitted, tickets filed |
| `touched_files` | the §8.2 diff budget, counted per distinct file |

`agent` is the one that makes the third belt possible. A tool handler can ask `ctx.agent.name` and `ctx.agent.policy` and refuse on the spot, with a reason that names the policy. That is why `put_system_map` can say "Only CARTOGRAPHER may publish the system map" — it knows.

`counters` is the other quiet one. `ctx.bump("envelopes")` and `ctx.count("tickets")` are how per-run caps are enforced inside the tool that would exceed them, rather than noticed afterwards by the conductor.

The context is **rebuilt per agent invocation**. FORGE runs once per finding, and each invocation gets a fresh `ToolContext` — which is also a trap that has been hit: `_record_task` in `src/qaas/runner.py:100-103` numbers artifacts off what is already on disk rather than off `ctx.counters`, precisely because an in-memory counter restarts at 1 on every dispatch.

---

## `ok()` / `err()` — errors are returned, not raised

`src/qaas/mcp/context.py:51-65`:

```python
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
```

Nine lines that set the tone for every server in the directory. An exception kills the turn and wastes the budget. A returned `err()` reaches the model as text it can act on — so every refusal in this codebase is written as an instruction, not a status code:

```python
        if ctx.agent.name != "FORGE":
            return err("Only FORGE records reproduction verdicts.")
```

```python
            return err(
                f"Finding cap reached ({cap} for this run). Emitting more is disabled. "
                "If you genuinely have more real defects than this, that is an escalation, "
                "not a filing problem: summarise what remains in your final message and stop."
            )
```

The second one is the house style: say what happened, say what it means, say what to do instead. An agent that hits a cap and is told "escalate, do not retry" does something useful. An agent told "429" retries.

`ok()`'s `**structured` keyword goes into `structuredContent`, and that is not cosmetic either — the `PostToolUse` hook reads `structuredContent.fileable` off an `emit_envelope` result to warn an agent that its finding was held. A tool that only returned prose could not be inspected that way.

There is also a third helper, `handlers()` (`src/qaas/mcp/context.py:68-70`), which maps tool name to handler so the offline test suite can call a tool directly without standing up an MCP transport. That is how the 649 offline tests run with no network and no API key.

---

## The seven servers

| server | module | tools | what it is for |
|---|---|---|---|
| `envelope` | `mcp/envelope_server.py` | `emit_envelope`, `get_system_map`, `put_system_map`, `put_artifact`, `list_envelopes`, `record_reproduction`, `record_verdict`, `record_review` | "the only way a finding leaves an agent" |
| `defect_memory` | `mcp/defect_memory.py` | `search_similar`, `fingerprint`, `record`, `get_occurrences`, `mark_resolved` | "the thing that stops duplicate storms" — SQLite, outlives the run |
| `tracker` | `mcp/tracker.py` | `create_issue`, `transition`, `link`, `search` | "every ticket the system files passes through here" |
| `test_runner` | `mcp/test_runner.py` | `run_suite`, `run_single`, `run_n_times`, `affected_tests`, `get_coverage` | "structured outcomes, never scraped CLI text" |
| `env_control` | `mcp/env_control.py` | `spin_up`, `seed`, `reset`, `set_flag`, `get_flags`, `set_clock`, `impersonate`, `status`, `tear_down` | "the environment an agent can actually name" |
| `contract_diff` | `mcp/contract_diff.py` | `diff_openapi`, `classify_breaking`, `find_consumers`, `generate_contract_test` | "what changed for the people calling you" |
| `vcs` | `mcp/vcs.py` | `current_branch`, `create_branch`, `write_file`, `commit`, `diff`, `list_branches`, `push`, `open_pr`, `pr_diff`, `list_changed_files` | "the §8.1 write matrix, enforced where the write happens" |

Plus one stdio server: `playwright`, declared in `registry.STDIO_SERVERS` and used only by SURFACE.

Every module follows the same two-function shape — `build_tools(ctx) -> list` and `build(ctx)`:

```python
def build(ctx: ToolContext):
    """Construct the envelope MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="envelope", version="1.0.0", tools=build_tools(ctx))
```

The split exists so tests can call `build_tools(ctx)` and invoke the handlers directly. Every server does this identically: `src/qaas/mcp/envelope_server.py:461-463`, `vcs.py:504-506`, `tracker.py:410-412`, and so on.

### Domain rules are the third belt

`CLAUDE.md` describes three belts: the `PreToolUse` hook, `can_use_tool`, and the servers' own domain rules. The third one is what these modules mostly contain. `tracker.py`'s docstring is explicit that this is a design decision, not a duplication:

```python
"""The `tracker` MCP server — every ticket the system files passes through here.

The rules below are code, not prompt text, and that is deliberate. §8.1 says
only CLERK creates and only CLERK and PROOF transition; §4.12 caps tickets per
run and requires that hitting the cap escalates instead of filing; §10 lists
"security findings leak into public tickets" as a named failure mode. A prompt
can be argued with, misread, or dropped from a truncated context. A refusal
returned from the tool cannot.
...
"""
```

And the rule itself, `src/qaas/mcp/tracker.py:139-144`:

```python
    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_create_tickets:
            return deny(
                "create_issue",
                f"{ctx.agent.name} may not create tickets (§8.1: CLERK only). "
                "Emit your finding as an envelope; CLERK files it.",
            )
```

`vcs.py` does the same for paths — `_path_refusal` (`src/qaas/mcp/vcs.py:102-145`) resolves symlinks and `..` *before* the containment test, so an absolute path, a traversal and a symlink out of the sandbox are all caught by one check.

---

## One server end to end: `envelope`

`src/qaas/mcp/envelope_server.py` is the one to read first, because it is the only server every agent has.

### The docstring sets the contract

```python
"""The `envelope` MCP server — the only way a finding leaves an agent.

Validation happens here rather than in a prompt. An agent that emits a malformed
envelope gets the field-level errors back and can correct them; an agent that
emits a finding with no evidence gets refused. Neither is negotiable by argument.
"""
```

### The schema is the prompt

`EMIT_SCHEMA` (`src/qaas/mcp/envelope_server.py:19-99`) is a JSON Schema, and its `description` fields are where the standards live. A sample:

```python
        "title": {"type": "string", "maxLength": 90, "description": "One line naming the defect, not the symptom."},
        "summary": {"type": "string", "description": "2-4 sentences: what breaks, when, for whom."},
        "severity": {"type": "string", "enum": ["blocker", "critical", "major", "minor", "trivial"]},
        "confidence": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "How sure you are a maintainer would accept this. Below 0.6 goes to human review.",
        },
```

`maxLength: 90` on `title` is enforced by the schema, not asked for in a prompt. `"naming the defect, not the symptom"` is a standard the model reads at the moment it writes the field, which is the only moment it matters.

The `reproduction` block carries the sharpest one:

```python
        "reproduction": {
            "type": "object",
            "description": (
                "The steps you took and the state you observed. You cannot mark a "
                "finding reproduced — FORGE verifies that independently, which is "
                "the point of a separate triage agent."
            ),
```

### The handler

`src/qaas/mcp/envelope_server.py:109-164`. The decorator carries name, description and schema:

```python
    @tool(
        "emit_envelope",
        "Report one defect. This is the only way a finding leaves your session. "
        "Rejected envelopes come back with the reason; fix the fields and retry.",
        EMIT_SCHEMA,
    )
    async def emit_envelope(args: dict[str, Any]) -> dict[str, Any]:
        cap = ctx.config.thresholds.max_findings_per_agent_run
        if ctx.count("envelopes") >= cap:
            ctx.store.log("escalation", agent=ctx.agent.name, reason="finding cap reached", cap=cap)
            return err(
                f"Finding cap reached ({cap} for this run). Emitting more is disabled. "
                "If you genuinely have more real defects than this, that is an escalation, "
                "not a filing problem: summarise what remains in your final message and stop."
            )
```

Cap first, from `ctx.count`. Then the part where the tool overrules the model:

```python
        payload = {k: v for k, v in args.items() if k != "similar_to"}
        payload["run_id"] = ctx.store.run_id
        payload["discovered_by"] = ctx.agent.name

        # A discovery agent does not get to certify its own finding as
        # reproduced (§2: the finder never grades its own homework). Whatever it
        # claims here, the status is reset and FORGE decides independently.
        # Without this the whole triage gate is bypassed by an agent simply
        # asserting it already reproduced the defect — which is exactly what
        # happened on the first full pipeline run, and FORGE was skipped.
        environment = (args.get("reproduction") or {}).get("environment", {})
        steps = (args.get("reproduction") or {}).get("steps", [])
        payload["reproduction"] = {
            "status": "unattempted",
            "steps": steps,
            "environment": environment,
        }
```

`run_id` and `discovered_by` come from the context, not from the arguments — an agent cannot claim to be another agent. And `status` is hard-set to `"unattempted"` no matter what the model sent. The comment records that this is not hypothetical: on the first full pipeline run an agent asserted its own finding was reproduced and the entire triage phase was skipped.

Then validation, persistence, and a result the hook can read:

```python
        try:
            envelope = DefectEnvelope.model_validate(payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8]
            )
            return err(f"Envelope rejected. Fix these fields and call again: {problems}")

        fileable, reason = envelope.is_fileable(ctx.config.thresholds.min_confidence_to_file)
        ctx.store.put_envelope(envelope)
        n = ctx.bump("envelopes")

        note = "" if fileable else f" Held from filing: {reason}. It still counts toward your cap."
        return ok(
            f"Recorded {envelope.severity.value} {envelope.domain.value} finding "
            f"'{envelope.title}' ({n}/{cap}).{note}",
            envelope_id=envelope.id,
            fingerprint=envelope.fingerprint(),
            fileable=fileable,
        )
```

Note the shape of the failure path: Pydantic's field-level errors are flattened into one line, capped at eight, and returned as `err()` with "fix these fields and call again". The agent gets the diagnosis and the instruction together.

Note also that a **held** envelope returns `ok()`, not `err()` — nothing went wrong with the call, the finding just failed `is_fileable()`. `fileable=False` rides along in `structuredContent`, and the `PostToolUse` hook in `src/qaas/registry.py:285-297` picks it up and tells the agent immediately. That handshake between a tool result and a hook is why `ok()` takes `**structured` at all.

### Role gates in the same file

Four of the eight tools are single-agent:

```python
        if ctx.agent.name != "CARTOGRAPHER":
            return err("Only CARTOGRAPHER may publish the system map.")
```
```python
        if ctx.agent.name != "FORGE":
            return err("Only FORGE records reproduction verdicts.")
```
```python
        if ctx.agent.name != "PROOF":
            return err("Only PROOF records verification verdicts.")
```
```python
        if ctx.agent.name != "ARBITER":
            return err("Only ARBITER records review decisions.")
```

And two tools refuse *empty* work, which is a different kind of gate. `record_verdict` (`src/qaas/mcp/envelope_server.py:362-368`):

```python
        if verdict == "VERIFIED" and not args.get("ran"):
            # A verdict that closes a ticket must say what backed it. Without
            # this an empty VERIFIED is indistinguishable from a thorough one.
            return err(
                "VERIFIED requires `ran` — name the original failing test and the "
                "regression tests you executed. A verdict nobody can audit is not a verdict."
            )
```

`record_review` (`src/qaas/mcp/envelope_server.py:426-435`) does the same for rubber-stamp approvals:

```python
        if len(reasoning) < 40:
            return err(
                f"{decision} needs reasoning a person can act on — at least a "
                "sentence naming what you checked and what you concluded."
            )
        if decision == "REQUEST_CHANGES" and not args.get("concerns"):
            return err(
                "REQUEST_CHANGES must list concerns. MENDER gets them verbatim and "
                "cannot act on a verdict with no specifics."
            )
```

A 40-character floor is crude and it works: it is the shape a rubber stamp takes.

---

## How a server reaches an agent

### The two registries

`src/qaas/registry.py:33-51`:

```python
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
```

`SDK_SERVER_MODULES` maps a config name to an import path, so the module is imported lazily — only for agents that actually declared it.

### `build_mcp_servers` — the resolution order

`src/qaas/registry.py:185-210`:

```python
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
```

Four steps, in order:

1. **config-declared** (`system.yaml`'s `mcp_servers:`) — first, so a project can override a built-in name
2. **built-in SDK server** — imported lazily, `module.build(ctx)`
3. **built-in stdio server** — a copy of the dict, so a mutation cannot leak between agents
4. **`UnknownServer`** — with the full list of what *was* available

Step 1 being first is the interesting one. The bundled Playwright entry hardcodes `--browser chromium`; declaring `playwright` yourself in `system.yaml` replaces it wholesale, no fork required.

The error in step 4 lists every candidate from all three sources. An error that says "unknown server" and stops is an error you have to go read the source to act on.

### The agent declares, the allowlist grants wholesale

An agent's YAML names servers by string. `src/qaas/defaults/config/agents/forge.yaml`:

```yaml
mcp_servers: [envelope, test_runner, env_control, vcs]
```

and `build_allowed_tools` (`src/qaas/registry.py:213-226`) grants each one as a wildcard:

```python
def build_allowed_tools(spec: AgentSpec) -> list[str]:
    """The allowlist handed to the SDK.

    Servers are allowed wholesale — the server itself enforces which of its tools
    this agent may use, and it has the context to explain a refusal properly.
    """
```

`mcp_server_wildcard(s)` returns `f"mcp__{s}"` (`src/qaas/sdk_compat.py:47-49`). So the allowlist is per-*server*, and per-*tool* enforcement is the server's job — which it can do better, because it has `ctx.agent` and can name the policy in its refusal.

The guardrail checks the same thing from the other side (`src/qaas/guardrails.py:227-234`):

```python
    def _check_mcp(self, tool_name: str) -> Decision:
        if tool_name.startswith(self._mcp_prefixes):
            return Decision(True)
        server = tool_name.split("__")[1] if "__" in tool_name else "?"
        return Decision(
            False,
            f"{self.agent.name} is not connected to the '{server}' server. "
            f"Its servers are: {', '.join(self.agent.mcp_servers) or 'none'}.",
        )
```

---

## Declaring your own server

You do not need to fork the package. `system.yaml` takes an `mcp_servers:` block, and `SystemConfig.mcp_servers` (`src/qaas/config.py:187-190`) is clear about what declaring does and does not do:

```python
    #: Servers this project declares, on top of the built-in ones. Declaring a
    #: server here grants nothing; an agent receives it only by naming it in its
    #: own `mcp_servers:` list.
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
```

Two shapes are accepted. `StdioServerSpec` (`src/qaas/config.py:116-130`):

```python
class StdioServerSpec(BaseModel):
    """A user-declared MCP server run as a subprocess.

    Pure data: `command` and `args` are passed to the CLI, which spawns it. No
    shell, ever -- `command` is a program and `args` is a list, so a string like
    `"foo && rm -rf /"` is a program name that does not exist rather than two
    commands.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
```

and `UrlServerSpec` (`src/qaas/config.py:133-140`):

```python
class UrlServerSpec(BaseModel):
    """A user-declared MCP server reached over HTTP or SSE."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["http", "sse"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
```

A worked example — add a hosted HTTP server and a stdio one, then give the HTTP one to CONDUIT:

```yaml
# system.yaml
mcp_servers:
  sentry:
    type: http
    url: https://mcp.sentry.dev/mcp
    headers:
      Authorization: "Bearer ${SENTRY_TOKEN}"
  ripgrep:
    type: stdio
    command: npx
    args: ["-y", "mcp-ripgrep@latest"]
```

```yaml
# config/agents/conduit.yaml
mcp_servers: [envelope, contract_diff, env_control, defect_memory, sentry]
```

`qaas validate` then prints every subprocess the run would spawn and every URL it would reach:

```
$ qaas validate --config <that config dir>
                   Declared MCP servers
┏━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ name    ┃ kind  ┃ what it runs               ┃ used by ┃
┡━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ ripgrep │ stdio │ npx -y mcp-ripgrep@latest  │ nobody  │
│ sentry  │ http  │ https://mcp.sentry.dev/mcp │ CONDUIT │
└─────────┴───────┴────────────────────────────┴─────────┘
These run with this process's environment. A server's tools are allowed
wholesale once an agent names it.
```

`ripgrep` shows `nobody` because no agent named it. Declaring is not granting.

The comment above that table (`src/qaas/cli.py:543-550`) explains why the command bothers:

```python
    # Show every subprocess a run would spawn, and every URL it would reach.
    #
    # Declaring a server grants nothing on its own -- an agent receives one only
    # by naming it in its own `mcp_servers:` list -- but once it does, the tools
    # of that server are allowed wholesale: `build_allowed_tools` grants
    # `mcp__<server>` and the guardrail's only question is whether the agent
    # declared it. qaas cannot police what a third-party server's tools do. So
    # the least this command can do is print what will run, before it runs.
```

### `${VAR}` expansion

Credentials never go in a config file. `_declared_server` (`src/qaas/registry.py:170-182`) runs `expand_env` over `command`, `url`, every element of `args`, and every value in `env` and `headers`. `expand_env` (`src/qaas/registry.py:142-167`):

```python
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
```

An unset variable raises `MissingServerEnv` with the server name and the variable name. Downstream, an empty string would have become a `Bearer ` header and an authentication failure fifteen minutes into a paid run.

### There is deliberately no Python server type

You cannot declare an in-process server. `src/qaas/config.py:143-149`:

```python
#: What a user may declare. Deliberately no in-process Python type: that would
#: mean `importlib.import_module` on a name from a config file, executing
#: arbitrary module-level code inside the process holding this user's Anthropic
#: credentials, Jira token and GitHub auth. A subprocess is a subprocess; an
#: import is a foothold. If someone needs a Python server they can wrap it in a
#: stdio entry point and it costs them one line.
McpServerSpec = StdioServerSpec | UrlServerSpec
```

The built-ins are imported by path from `SDK_SERVER_MODULES`, a dict in the source. A config file cannot add to it. If you want a Python MCP server, write it as a script and declare it as `type: stdio` — one line of config, and it runs in its own process.

This is the same reasoning as `setting_sources=[]` in `build_options`: the QA tool must not execute configuration belonging to the code it is inspecting.

---

## The six-server cap

`src/qaas/config.py:20-22`:

```python
# §5.3 is explicit that no agent gets more than six MCP servers, because tool
# selection accuracy falls off past roughly 5-7. Enforced, not just documented.
MAX_MCP_SERVERS_PER_AGENT = 6
```

Enforced in `AgentSpec._tool_budget` (`src/qaas/config.py:93-110`):

```python
    @model_validator(mode="after")
    def _tool_budget(self) -> "AgentSpec":
        if len(self.mcp_servers) > MAX_MCP_SERVERS_PER_AGENT:
            raise ValueError(
                f"{self.name} declares {len(self.mcp_servers)} MCP servers; "
                f"the cap is {MAX_MCP_SERVERS_PER_AGENT} (§5.3). "
                "An agent needing more is a signal to split it."
            )
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError(f"{self.name} lists a duplicate MCP server")
```

Where the roster sits today, from `qaas validate`'s `servers` column:

| agent | servers | count |
|---|---|---|
| CARTOGRAPHER | envelope | 1/6 |
| SURFACE | envelope, env_control, playwright | 3/6 |
| CLERK | envelope, tracker, defect_memory | 3/6 |
| CONDUIT | envelope, contract_diff, env_control, defect_memory | 4/6 |
| FORGE | envelope, test_runner, env_control, vcs | 4/6 |
| ARBITER | envelope, vcs, contract_diff, test_runner | 4/6 |
| PROOF | envelope, test_runner, env_control, tracker, vcs | 5/6 |
| **MENDER** | envelope, test_runner, env_control, vcs, tracker, contract_diff | **6/6** |

MENDER is at the cap. It writes code (`vcs`), proves the fix (`test_runner`), stands the app up (`env_control`), moves the ticket (`tracker`), checks it did not break the contract (`contract_diff`) and reads the finding (`envelope`). There is no seventh, and adding one means splitting the agent.

Try it and the config refuses to load:

```
$ qaas validate --config <mender with 7 servers>
config invalid: 1 validation error for SystemConfig
agents.MENDER
  Value error, MENDER declares 7 MCP servers; the cap is 6 (§5.3). An agent
needing more is a signal to split it. [type=value_error, ...]
```

### Names are checked at load, not mid-run

`AgentSpec` cannot see the rest of the config, so a second validator on `SystemConfig` closes the gap (`src/qaas/config.py:200-224`):

```python
    @model_validator(mode="after")
    def _agents_name_real_servers(self) -> "SystemConfig":
        """Every server an agent names must resolve to something.

        `AgentSpec` cannot check this -- it has no view of the rest of the
        config -- so an unresolvable name used to surface as `UnknownServer`
        part-way through a paid run. Here it is a load-time error, which is what
        `qaas validate` is for.
        ...
        """
```

Which turns a mid-run `UnknownServer` into a free failure before anything is dispatched:

```
$ qaas validate --config <conduit naming an undeclared 'sentry'>
config invalid: 1 validation error for SystemConfig
  Value error, CONDUIT names MCP server(s) nothing provides: sentry. Built in:
contract_diff, defect_memory, env_control, envelope, playwright, test_runner,
tracker, vcs. Declared in system.yaml: none.
```

Note the import is inside the function, with a comment saying why: `registry` imports `config`, so a module-level import would be a cycle.

---

## Checklist for adding a built-in server

If you are adding a server to the package rather than declaring one in config:

1. Write `src/qaas/mcp/<name>.py` with `build_tools(ctx) -> list` and `build(ctx)`, using `@tool(...)`, `ok()` and `err()`.
2. Enforce your domain rules in the handlers, reading `ctx.agent.name` and `ctx.agent.policy`. Return the reason; never raise.
3. Add one line to `SDK_SERVER_MODULES` in `src/qaas/registry.py`.
4. Name it in the `mcp_servers:` list of any agent that needs it — checking they stay at or under 6.
5. Run `qaas validate`, then `pytest`. Handlers are testable directly via `handlers(build_tools(ctx))`, with no transport.

Nothing in `conductor.py`, `runner.py` or `guardrails.py` changes. If you find yourself editing one of those to add a server, something is wrong.
