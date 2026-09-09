# Guardrails and safety

This file covers what stops a `qaas` agent doing damage: where enforcement
actually lives (not where you would guess), what each gate checks, the two real
security holes that were found and closed, and how to watch a refusal happen on
your own machine in about ten seconds.

---

## 1. The central fact: `can_use_tool` is not enough

Start with the module docstring of `src/qaas/guardrails.py:1-29`. It is the most
important paragraph in the repository, and it exists because this code once had
the bug it describes.

```python
"""The §8.1 write-permission matrix, enforced in code.

Every agent gets a `can_use_tool` callback built from its policy. The callback
sees the tool name and its arguments before the tool runs, which is the only
place a limit like "FORGE may write, but only under qa/repro" can actually be
imposed. A prompt asking an agent not to do something is a request; this is a
decision.

Enforcement runs in the **PreToolUse hook**, not in `can_use_tool`. This is not
a stylistic choice and it is easy to get wrong: an `allowed_tools` entry that
names a whole tool auto-approves it *before* `can_use_tool` is consulted, so a
policy implemented only in that callback is silently never applied. The SDK warns
about this shadowing, and an early version of this file had exactly that bug —
FORGE's sandbox check was dead code. The hook sees every call regardless.

`can_use_tool` is kept as a second layer, for anything that falls outside the
allowlist and so reaches the callback normally.

Three belts, then:

  * The PreToolUse hook — every built-in tool call, gated on this agent's policy.
  * `can_use_tool` — the same decision, for calls not auto-approved.
  * The MCP servers — their own domain rules (the tracker refuses an agent that
    may not file; vcs refuses a branch outside the agent's patterns).

Denials return a reason rather than killing the turn: an agent that learns it
cannot write to a path should adapt, and the reason is what lets it. Every
denial lands in the run ledger, which is the audit trail §8 asks for.
"""
```

Unpack the trap, because it is a genuinely counter-intuitive failure:

1. `build_options` hands the SDK an `allowed_tools` list
   (`src/qaas/registry.py:415`), built from the agent's YAML.
2. FORGE declares `builtin_tools: [Read, Grep, Glob, Write, Edit, Bash]`, so
   `"Write"` is on that list.
3. `Write` being on the allowlist means the SDK **auto-approves it**. The
   permission callback is for calls that were *not* already approved.
4. So `can_use_tool`, which contained the "only under `qa/repro`" check, was
   never called for `Write`.

The check was written, tested at the unit level, and dead in production. Nothing
was noisy about it: no exception, no warning in the run, no denial in the ledger
— just an agent quietly able to write anywhere.

The fix is that the same decision runs as a `PreToolUse` **hook**, which fires
for every tool call regardless of the allowlist. `src/qaas/registry.py:249-264`
says the same thing from the wiring side:

```python
    """Observability, plus one thing `can_use_tool` structurally cannot do.

    Permissions are enforced twice, on purpose. `can_use_tool` is primary, but
    the SDK can shadow it (see `CanUseToolShadowedWarning`), so `Guardrail.
    pre_tool_use` re-runs the same `check()` as a hook. Both call one decision
    function, so they cannot disagree — the duplication is in the wiring, not
    in the policy.
```

Both entry points are three lines each, and both end at `check()`.

`src/qaas/guardrails.py:127-138`:

```python
    async def can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Second layer. Reached only for calls the allowlist did not auto-approve."""
        decision = self.check(tool_name, input_data)
        if decision.allowed:
            return PermissionResultAllow(updated_input=input_data)
        self._record(tool_name, input_data, decision.reason, via="can_use_tool")
        return PermissionResultDeny(message=decision.reason)
```

`src/qaas/guardrails.py:140-170`:

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

Two differences worth noticing. The hook logs a `tool_call` line for *every*
call, allowed or not — that is where the run's tool traffic in the ledger comes
from. And `_record` stamps `via="hook"` or `via="can_use_tool"`, so an audit can
tell which layer caught something.

There is a regression test for the wiring alone, because if it is lost nothing
else works — `tests/test_guardrails.py:269-283`:

```python
def test_the_registry_wires_the_hook_to_pretooluse(tmp_path):
    """Regression guard: if this wiring is lost, nothing is enforced at all."""
    ...
    assert guard.pre_tool_use in hooks[PRE_TOOL_USE][0].hooks
```

---

## 2. `check()` and `Decision`

Everything funnels into one pure function. `src/qaas/guardrails.py:97-101`:

```python
@dataclass
class Decision:
    allowed: bool
    reason: str = ""
```

That is the whole type. A boolean and a sentence. The sentence is not decoration
— it is what the agent is handed so it can adapt, and it is what lands in the
ledger for a human reading the run later.

`src/qaas/guardrails.py:182-209`:

```python
    def check(self, tool_name: str, input_data: dict[str, Any]) -> Decision:
        """Pure policy evaluation. Separated from the callback so it is testable."""
        if tool_name.startswith("mcp__"):
            return self._check_mcp(tool_name)
        if tool_name in READ_TOOLS:
            return self._check_declared(tool_name)
        if tool_name in WRITE_TOOLS:
            # Policy before allowlist: "you are read-only" is the true reason and
            # the useful one. "not in your allowlist" would be technically correct
            # and would send the agent looking for the wrong fix.
            if not self._allowed_roots:
                return Decision(
                    False,
                    f"{self.agent.name} is read-only. Report what you found; "
                    "fixing is another agent's job (§2: the finder never fixes).",
                )
            declared = self._check_declared(tool_name)
            return declared if not declared.allowed else self._check_write(input_data)
        if tool_name == "Bash":
            declared = self._check_declared(tool_name)
            return declared if not declared.allowed else self._check_bash(input_data)
        if tool_name in {"WebFetch", "WebSearch"}:
            return Decision(
                False,
                f"{self.agent.name} has no network research remit. "
                "Findings come from the code and the running app, not the web.",
            )
        return self._check_declared(tool_name)
```

The dispatch order, in words:

| # | condition | goes to |
|---|---|---|
| 1 | name starts `mcp__` | `_check_mcp` — is this server on the agent's list? |
| 2 | in `READ_TOOLS` | `_check_declared` — reading is always fine if declared |
| 3 | in `WRITE_TOOLS` | read-only check, then `_check_declared`, then `_check_write` |
| 4 | `Bash` | `_check_declared`, then `_check_bash` |
| 5 | `WebFetch` / `WebSearch` | always denied |
| 6 | anything else | `_check_declared` |

The comment in branch 3 is the kind of detail that separates a guardrail people
work with from one they fight. Denying a discovery agent's `Write` with *"not in
your allowlist"* is true and useless — the agent goes looking for a config
change. Denying it with *"CONDUIT is read-only… the finder never fixes"* tells it
to go and report the finding, which is what you actually want it to do.

The tool-class constants, `src/qaas/guardrails.py:54-68`:

```python
ALWAYS_GRANTED = frozenset({"ToolSearch", "Skill", "TodoWrite", "Task", "Agent"})

# Tools that read. Always safe, for every agent.
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"} | set(ALWAYS_GRANTED)
...
HARNESS_TOOLS = {"ToolSearch", "TodoWrite", "Task", "Agent", "Skill", "SlashCommand"}

# Tools that write to the filesystem. Gated on policy.write_paths.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
```

---

## 3. Where the guardrail is anchored

`Guardrail.__init__`, `src/qaas/guardrails.py:106-123`:

```python
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.agent = ctx.agent
        self.policy = ctx.agent.policy
        # Every write path in a policy is relative to the *application under
        # test*, never to the qaas project. This was `ctx.repo_root`, filled
        # from `Path.cwd()`, which anchored the whole allowlist on wherever the
        # operator happened to be standing -- harmless only while the target was
        # a subdirectory of the qaas checkout. With `qaas run --repo <url>` the
        # target is a clone under `.qaas/targets/`, and an allowlist anchored on
        # the cwd would deny every legitimate write and permit a sandbox that
        # sits inside qaas's own source.
        self.root = ctx.target_root.resolve()
        self._allowed_roots = [
            (self.root / p).resolve() for p in self.policy.write_paths
        ]
        # Every MCP server the agent declared, as an allowlist prefix.
        self._mcp_prefixes = tuple(f"mcp__{s}__" for s in self.agent.mcp_servers)
```

`write_paths` are resolved **once**, against the target root, at construction.
Note the last clause of that comment: a cwd-anchored allowlist would *permit a
sandbox sitting inside qaas's own source*. The QA tool would have been writable
by the agents it runs.

---

## 4. The gates

### Policy shape

`src/qaas/config.py:27-50`:

```python
class Policy(BaseModel):
    """One agent's slice of the §8.1 write-permission matrix.

    Default is read-only. Anything an agent may write, it says so here, and
    guardrails.py enforces it against the actual tool call arguments.
    """

    model_config = ConfigDict(extra="forbid")

    write_paths: list[str] = Field(default_factory=list)
    branch_patterns: list[str] = Field(default_factory=list)
    may_open_pr: bool = False
    may_create_tickets: bool = False
    may_transition_tickets: bool = False
    max_tickets_per_run: int = 0
    max_diff_files: int | None = None
    max_diff_lines: int | None = None
    protected_paths: list[str] = Field(default_factory=list)
```

**Default is read-only.** `policy: {}` — which is literally what
`src/qaas/defaults/config/agents/conduit.yaml:14` says — is a complete,
locked-down policy. Capability is opt-in, one YAML key at a time.

What each agent gets:

| agent | write_paths | branch_patterns | other |
|---|---|---|---|
| CARTOGRAPHER | — | — | read-only |
| CONDUIT | — | — | read-only (`policy: {}`) |
| SURFACE | — | — | read-only |
| FORGE | `qa/repro` | `qa/repro/*` | — |
| CLERK | — | — | `may_create_tickets`, `max_tickets_per_run: 10` |
| PROOF | — | — | `may_transition_tickets` (transition only, never create) |
| MENDER | `api/app`, `web/src`, `qa/repro` | `fix/*` | `may_open_pr`, `max_diff_files: 5`, forbidden classes |
| ARBITER | — | — | read-only; *"your judgement is the deliverable"* |

### Path scoping — `_check_write`

`src/qaas/guardrails.py:237-284`. The interesting parts:

```python
        target = Path(raw)
        resolved = (target if target.is_absolute() else self.root / target).resolve()

        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError:
            relative = resolved.as_posix()
```

The path is **resolved first**, then checked. `..`, an absolute path, and a
symlink pointing out of the sandbox all fail the same containment test, because
by the time the test runs they are the same thing.

Then, in order:

```python
        # The autonomy envelope (§8.2) comes first. A path inside the sandbox but
        # in a forbidden class must still be refused, and the reason must name
        # the class so the agent escalates rather than looking for a way round.
        forbidden = self._forbidden_class(relative)
        if forbidden:
            return Decision(
                False,
                f"{relative} is outside {self.agent.name}'s autonomy envelope: it is "
                f"{forbidden}. Changes here need human approval (§8.2). Describe the "
                "change you would make and escalate instead of making it.",
            )
```

Ordering matters: forbidden classes are checked *before* the sandbox test, so
`api/app/auth.py` — inside MENDER's `api/app` sandbox — is still refused for
being auth code.

### Forbidden path classes

`_forbidden_class`, `src/qaas/guardrails.py:286-291`:

```python
    def _forbidden_class(self, relative: str) -> str | None:
        """Which §8.2 class this path falls into, if any."""
        for pattern in self.policy.forbidden_paths:
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern):
                return _describe_forbidden(pattern)
        return None
```

The patterns are MENDER's, in
`src/qaas/defaults/config/agents/mender.yaml:42-54`:

```yaml
  forbidden_paths:
    - "*migrations/*"
    - "*migration*"
    - "*auth.py"
    - "*auth*"
    - "*payment*"
    - "*billing*"
    - "*secret*"
    - "*.tf"
    - "*infra/*"
    - "*docker-compose*"
    - "Dockerfile*"
    - "*.github/*"
```

`_describe_forbidden` (`guardrails.py:380-402`) turns a pattern back into English
so the refusal names the class rather than the glob:

```python
_FORBIDDEN_DESCRIPTIONS = [
    ("migration", "a database migration"),
    ("auth", "authentication or authorization code"),
    ("payment", "a payment path"),
    ...
```

Why these classes and not others — `config.py:46-49`:

```python
    #: Path globs this agent may never modify, whatever else its policy allows.
    #: §8.2 names the classes: migrations, auth, payment paths and infra config.
    #: These are the changes whose blast radius a review cannot reliably bound,
    #: so they stop at a human even when everything else in the envelope holds.
```

### The diff budget

`_check_diff_budget`, `src/qaas/guardrails.py:299-322`:

```python
    def _check_diff_budget(self, relative: str) -> Decision:
        """Cap how much one agent may change in a single run (§8.2).

        Counted per distinct file touched, not per call: an agent editing the
        same file six times has made one file's worth of change, and counting
        calls would refuse a perfectly ordinary iteration.
        """
        max_files = self.policy.max_diff_files
        if max_files is None:
            return Decision(True)

        touched = self.ctx.touched_files
        if relative in touched:
            return Decision(True)
        if len(touched) >= max_files:
            return Decision(
                False,
                f"{self.agent.name} has already changed {len(touched)} files, which is "
                f"its limit of {max_files} (§8.2). A fix this wide is outside the "
                "autonomy envelope: stop, and escalate with what you have found. "
                f"Already touched: {', '.join(sorted(touched))}.",
            )
        touched.add(relative)
        return Decision(True)
```

`ctx.touched_files` is a `set[str]` on the `ToolContext`
(`src/qaas/mcp/context.py:39-41`), rebuilt per agent invocation. Per-file, not
per-call — the distinction is the difference between a limit and an annoyance.

Note the denial names every file already touched. An agent hitting this limit
needs to write an escalation, and it needs the list to write it.

### Branch scoping and `FORBIDDEN_BASH`

`_check_bash`, `src/qaas/guardrails.py:324-360`, runs four checks in order:

```python
        for pattern, why in FORBIDDEN_BASH:
            if re.search(pattern, command):
                return Decision(False, f"command refused: {why}")

        if GIT_WRITE.search(command) and not self.policy.branch_patterns:
            return Decision(
                False,
                f"{self.agent.name} may not modify git state. "
                "It has no branch patterns in its policy.",
            )

        if self.policy.branch_patterns:
            branch = _branch_from_command(command)
            if branch and not any(
                fnmatch.fnmatch(branch, pat) for pat in self.policy.branch_patterns
            ):
                return Decision(
                    False,
                    f"branch '{branch}' is outside {self.agent.name}'s patterns "
                    f"({', '.join(self.policy.branch_patterns)}).",
                )
```

`FORBIDDEN_BASH` itself, `src/qaas/guardrails.py:70-86`:

```python
# Bash command prefixes that are never allowed, whatever the agent.
# Merging to main, force-pushing and recursive deletes are outside every
# agent's remit in this system: merge is always human (§8.4), and nothing here
# needs to delete a tree.
FORBIDDEN_BASH = [
    (r"\bgit\s+push\b.*(--force|-f\b)", "force-push is never permitted"),
    (r"\bgit\s+push\b.*\b(main|master)\b", "pushing to main is never permitted"),
    (r"\bgit\s+merge\b", "merging is a human decision (§8.4)"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset discards work outside the sandbox"),
    (r"\bgit\s+checkout\s+(main|master)\b", "agents work on their own branches only"),
    (r"\brm\s+-[a-zA-Z]*[rf]", "recursive or forced delete is not permitted"),
    (r"\bsudo\b", "privilege escalation is not permitted"),
    (r"\b(shutdown|reboot|mkfs|dd)\b", "destructive system command"),
    (r">\s*/dev/(sd|nvme|disk)", "writing to a block device"),
    (r"\bdocker\s+system\s+prune", "prune would destroy state other runs depend on"),
    (r"\bgh\s+pr\s+merge\b", "merging a pull request is a human decision (§8.4)"),
]
```

`_MUTATES_FILE` (`guardrails.py:91-94`) carries its own bug story:

```python
# Shell constructs that rewrite a file in place. `>` is not a word character, so
# this deliberately does not use \b anchors — an earlier version did and silently
# matched nothing.
_MUTATES_FILE = re.compile(r"(>>?|\btee\b|\bsed\s+-i|\btruncate\b|\bdd\b)")
```

`\b>` never matches anything, because `\b` is a boundary between a word and a
non-word character and `>` is not a word character. The pattern compiled, ran on
every command, and matched nothing.

### No merge path exists anywhere

`FORBIDDEN_BASH` blocks `git merge` and `gh pr merge` at the shell. The other
half is that the `vcs` MCP server has no merge tool to call. Its docstring,
`src/qaas/mcp/vcs.py:11-19`:

```python
  * `main` and `master` are refused for everyone, always, even if a policy were
    misconfigured to name them. Merging is a human act (§8.4).
  * Force-push is not a tool. There is nothing to refuse because there is
    nothing to call.
  ...
  * Pushing and opening a PR ride one permission (`policy.may_open_pr`), because
    publishing a branch to a shared remote exposes the same work the PR would.
    There is no merge tool: §8.4 makes merging a human act.
```

The tool list on that server (`vcs.py`) is `current_branch`, `create_branch`,
`write_file`, `commit`, `diff`, `list_branches`, `push`, `open_pr`, `pr_diff`,
`list_changed_files`. No merge, no force-push, and `push`'s own description says
so:

```python
        "Publish a branch to the remote. Requires may_open_pr in your policy; main, "
        "master, trunk, develop and release* are refused as the branch to push. "
        "There is no force-push.",
```

Plus a fourth belt that survives a misconfigured policy, `vcs.py:37-39`:

```python
# Refused for every agent regardless of policy. A policy that names one of these
# is a misconfiguration, and this is the layer that survives it.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "release"})
```

The test that keeps it that way, `tests/test_guardrails.py:376-381`:

```python
def test_nothing_in_the_system_can_merge(guard_for):
    """§8.4: merge is always human. Not policy — absence of capability."""
    for agent in ["MENDER", "ARBITER", "PROOF", "FORGE"]:
        g = guard_for(agent)
        assert not g.check("Bash", {"command": "git merge fix/x"}).allowed
        assert not g.check("Bash", {"command": "gh pr merge 42 --squash"}).allowed
```

### The immutable test

A fixer that edits the test defining its own success has no acceptance criterion.
Two enforcement points.

Through `Write`/`Edit` (`guardrails.py:268-275`):

```python
        protected = self._protected_path(relative)
        if protected:
            return Decision(
                False,
                f"{relative} is the test that defines success for this ticket and may "
                "not be edited (§10: a fixer that edits the test patches the symptom). "
                "If you believe the test itself is wrong, that is an escalation.",
            )
```

And through the shell, because `sed -i` and `>` are also ways to rewrite a file
(`guardrails.py:351-358`):

```python
        if self.policy.protected_paths:
            for protected in self.policy.protected_paths:
                if protected in command and _MUTATES_FILE.search(command):
                    return Decision(
                        False,
                        f"'{protected}' is protected: it defines what a fix must achieve "
                        "and may not be edited (§10, symptom fixes).",
                    )
```

`protected_paths` is set per ticket at run time (the shipped MENDER YAML declares
none), and `vcs.py:126-131` refuses the same paths on the MCP side. The skill
`test-first-fix` states the rule for the case where it is *not* populated:

> When the run lists that path in your policy's `protected_paths`, the write is
> refused outright … When it is not listed, the rule holds anyway. A fixer that
> can edit its own acceptance criterion has no acceptance criterion.

### The ticket rate limit

Not in `guardrails.py` — in the tracker server, because that is where the write
happens. `src/qaas/mcp/tracker.py:139-160`:

```python
    async def create_issue(args: dict[str, Any]) -> dict[str, Any]:
        if not policy.may_create_tickets:
            return deny(
                "create_issue",
                f"{ctx.agent.name} may not create tickets (§8.1: CLERK only). "
                "Emit your finding as an envelope; CLERK files it.",
            )

        cap = policy.max_tickets_per_run
        if ctx.count("tickets") >= cap:
            ctx.store.log(
                "escalation", agent=ctx.agent.name,
                reason="ticket cap reached", cap=cap, tool="create_issue",
            )
            return deny(
                "create_issue",
                f"Ticket cap reached ({cap} for this run). Filing is disabled for the rest "
                "of the run. Hitting the cap means something upstream is wrong, and forty "
                "more tickets will not fix it: this is an escalation, not a filing problem. "
                "Summarise what remains unfiled in your final message and stop.",
            )
```

Two caps meet here: CLERK's own `max_tickets_per_run: 10`
(`clerk.yaml:16`) and the system-wide `thresholds.max_tickets_per_run: 10`
(`system.yaml:15`). The conductor takes the smaller
(`src/qaas/conductor.py:329`):

```python
        cap = min(spec.policy.max_tickets_per_run, self.config.thresholds.max_tickets_per_run)
```

Hitting the cap is logged as an `escalation`, not a `denial`. A run that wanted
to file forty tickets has a problem upstream of filing.

The same module carries a rule that is not a rate limit but belongs in the same
list — security findings never reach a public project. `tracker.py:68-75`:

```python
def is_restricted(envelope: DefectEnvelope) -> bool:
    """Whether this finding may only be filed into the restricted project.

    Two independent triggers, because either alone is enough to make a public
    ticket a disclosure: the reporter flagged security impact, or the defect is
    classified as a vulnerability.
    """
    return envelope.impact.security_relevant or envelope.defect_class == DefectClass.VULNERABILITY
```

If no restricted project is configured, filing is refused outright rather than
downgraded — *"there is no undo"* (`tracker.py:177-186`).

---

## 5. `ALWAYS_GRANTED`, and why denying `ToolSearch` would break everything

`src/qaas/guardrails.py:48-65`:

```python
# Harness plumbing granted to every agent, independent of its config. These are
# not capability grants: ToolSearch only loads the schemas of servers the agent
# already has, Skill only loads instructions, and neither can reach anything the
# allowlist does not already permit. `build_allowed_tools` adds the same set, and
# both read this constant so the allowlist and the guardrail cannot drift apart —
# a mismatch here silently disables every skill in the system.
ALWAYS_GRANTED = frozenset({"ToolSearch", "Skill", "TodoWrite", "Task", "Agent"})
...
# Harness plumbing, not capability. These grant an agent nothing it was not
# already granted — ToolSearch only loads the schema of a tool that is already
# on its allowlist, and Skill only opens a skill file. Denying ToolSearch is
# worse than useless: MCP tools arrive deferred, so an agent that cannot call it
# cannot reach the servers it was given, and burns its whole turn budget
# discovering that. This system did exactly that once.
HARNESS_TOOLS = {"ToolSearch", "TodoWrite", "Task", "Agent", "Skill", "SlashCommand"}
```

**Why `ToolSearch` cannot be denied.** MCP tool schemas arrive *deferred* — the
agent is told the names, not the parameters. `ToolSearch` is how it fetches a
schema so it can actually call the tool. Deny it and an agent given six MCP
servers can reach none of them: it knows `mcp__envelope__emit_envelope` exists,
cannot learn its arguments, and spends its whole turn budget failing to call it.
There is no error message that says "you needed `ToolSearch`". It just does not
work. *This system did exactly that once.*

**Why it is not a capability grant.** `ToolSearch` can only return schemas for
tools already on the agent's allowlist. It reveals nothing and permits nothing.
Same for `Skill`: it opens a procedure file.

**Why the constant is shared.** `build_allowed_tools`,
`src/qaas/registry.py:213-226`:

```python
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
```

If the allowlist granted `Skill` and the guardrail did not, every skill
invocation would be denied — silently, from the agent's point of view, as a
refusal it cannot interpret. One `frozenset`, imported by both, removes the
possibility.

---

## 6. Denials are returned, not fatal

Look again at what a denial produces. The hook returns
`permissionDecision: "deny"` with a `permissionDecisionReason`; `can_use_tool`
returns `PermissionResultDeny(message=…)`; the MCP servers return `err()`
(`src/qaas/mcp/context.py:59-66`):

```python
def err(text: str) -> dict[str, Any]:
    """A failed MCP tool result.

    Tool errors are returned, not raised: the agent should read the reason and
    correct itself rather than have the turn die.
    """
```

Nothing raises. The turn continues. The agent reads *"FORGE… is outside FORGE's
sandbox (qa/repro)"* and writes to `qa/repro` instead — which is the outcome you
wanted.

And every refusal is recorded (`guardrails.py:172-180`):

```python
    def _record(self, tool_name: str, input_data: dict[str, Any], reason: str, *, via: str) -> None:
        self.ctx.store.log(
            "denial",
            agent=self.agent.name,
            tool=tool_name,
            reason=reason,
            via=via,
            args=_summarise(input_data),
        )
```

`_summarise` (`guardrails.py:412-421`) truncates values at 200 characters and
replaces `content`, `new_string` and `old_string` with `<N chars>` — enough to
audit, not a content dump into the ledger.

A real run bears out that this is normal traffic rather than an alarm.
`run-20260907T233304-ca7657` recorded 1373 `tool_call` lines and 59 `denial`
lines, and finished with 15 findings, 10 tickets and one VERIFIED fix. Denials
are the system steering, not the system failing.

---

## 7. Two real holes, found and closed

### (a) `setting_sources` was `["project"]`

**The hole.** `build_options` passes `cwd=str(ctx.target_root)` to the SDK
(`src/qaas/registry.py:423`) — the agent's working directory is the application
under test. `setting_sources=["project"]` tells the SDK to load *project*
settings, and the SDK resolves "project" against `cwd`.

So: `qaas run --repo https://github.com/someone/something` clones a repository
and then loads **that repository's** `.claude/settings.json`, its hooks, its
permission rules and its MCP servers into the process that holds the Anthropic
API key, `JIRA_API_TOKEN` and GitHub auth. A QA tool that executes the
configuration of the code it is inspecting is a supply-chain hole with the
credentials already in the room.

The commit that closed it (`e0b5c84`, *"Ship skills in the wheel, and stop
loading the target's settings"*) puts it plainly:

> THE SECURITY FIX. setting_sources is now an explicit []. The old comment
> defended "project" on reproducibility grounds, and that held while cwd was our
> own repository. cwd is the target now, and with `qaas run --repo <url>` it can
> be a repository cloned seconds earlier from a pasted URL: "project" would load
> that repository's settings, hooks, permission rules and MCP servers into a
> process holding Anthropic credentials, JIRA_API_TOKEN and GitHub auth.

**Why it was defensible before.** It genuinely was. While `cwd` was the qaas
checkout, "project" meant *our own* settings, which travel with the repo and make
runs reproducible. The reasoning was sound; the premise changed underneath it.
That is what makes this class of hole hard — nothing about the line was wrong on
the day it was written.

**Why it could not be fixed alone.** Skills were *found* through
`setting_sources=["project"]`, at `<cwd>/.claude/skills`. Setting it to `[]`
without moving them first would have deleted every procedure in the system with
no error at all. The same commit ships the 30 skills as a Claude Code plugin
inside the package. It also notes what had already been lost: anyone who
`pip install`ed got none of the skills, silently, because that directory existed
only in this checkout.

**The fix**, `src/qaas/registry.py:425-439`:

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

**The `[]` vs `None` trap is real.** `None` is not "load nothing" — the SDK
substitutes `["user", "project"]` when `setting_sources` is `None` and `skills`
is a list. Deleting the argument would reintroduce the hole *and* add
`~/.claude`.

**The test**, `tests/test_guardrails.py:477-495`:

```python
def test_the_sdk_subprocess_is_started_in_the_target(tmp_path):
    """`registry` hands `cwd` to the SDK. It is the target, and it travels with
    `setting_sources=[]` -- a cloned repository's `.claude/settings.json` must
    never load into a process holding this system's credentials."""
    ...
    assert options.cwd == str(target)
    assert options.setting_sources == []
```

### (b) MENDER's forbidden globs were written `*/x/*`

**The hole.** MENDER's forbidden classes used to be spelled `*/.github/*`,
`*/migrations/*`, `*/infra/*`. That leading `*/` requires at least one path
segment *before* the directory.

While the target was the bundled demo, paths arrived as
`target-app/.github/workflows/ci.yml` — there was a segment in front, so
`*/.github/*` matched, and every test passed.

Then the target root was split from the project root
(commit `59cc480`). Paths became target-relative. A repository's own CI config is
at `.github/workflows/ci.yml`, with nothing in front of it. `*/.github/*` matched
**nothing**. MENDER could rewrite the CI workflow of the repository it was
fixing, and could then have done anything CI can do.

The commit message:

> * MENDER's `forbidden_paths` were written `*/x/*`. Once paths stopped arriving
>   prefixed with `target-app/`, a repository's own root-level `.github/` —
>   where essentially every repository keeps its CI — matched nothing. All the
>   directory globs are now `*x/*`, which only ever widens a refusal.

**The fix** is one character per pattern, and the YAML now explains itself
(`src/qaas/defaults/config/agents/mender.yaml:34-54`):

```yaml
  # §8.2: these classes stop at a human however small the change looks. Their
  # blast radius is not something a review can reliably bound.
  #
  # Matched against a path relative to the *target's* root, so the directory
  # patterns are `*x/*` and not `*/x/*`: with a leading slash required,
  # `.github/workflows/ci.yml` -- a repository's CI at its own root, which is
  # where almost every repository keeps it -- matched nothing. That hole was
  # invisible while paths arrived prefixed with `target-app/`.
  forbidden_paths:
    - "*migrations/*"
    ...
    - "*.github/*"
```

`*x/*` matches both `.github/workflows/ci.yml` and
`vendor/thing/.github/workflows/ci.yml`. It only ever widens a refusal.

**The test**, `tests/test_guardrails.py:458-474`:

```python
def test_a_forbidden_class_at_the_root_of_a_target_is_still_refused(tmp_path):
    """`.github/` sits at a repository's root, which is exactly where these globs
    used to miss it: paths arrived prefixed with `target-app/`, so `*/.github/*`
    matched, and the day the prefix went away it silently stopped matching."""
    target = tmp_path / "clone"
    target.mkdir()
    g = _mender_guard(tmp_path, target)
    for path in (
        ".github/workflows/ci.yml",
        "migrations/001_add_index.sql",
        "infra/main.tf",
        "docker-compose.yml",
    ):
        d = g.check("Write", {"file_path": path, "content": "..."})
        assert not d.allowed, f"{path} was not caught by a forbidden class"
        assert "escalate" in d.reason.lower(), f"{path}: a refusal must say what to do instead"
```

Note the second assertion. It is not enough that the write is refused; the
refusal must tell the agent to escalate, or the agent goes looking for a way
round.

### What the two have in common

Neither was a wrong line of code. Both were **correct code whose premise
changed** — `cwd` stopped being our repo; paths stopped carrying a prefix. Both
were invisible: no exception, no failing test, no denial in the ledger. Both were
found by a refactor that forced someone to re-read an assumption, and both are
now pinned by a test that fails if the assumption comes back.

---

## 8. Watch it work

You do not need an API key or a running target to exercise the guardrail. The
fixture pattern is `tests/test_guardrails.py:28-42`; here it is as a standalone
script, run against a throwaway empty directory standing in for a target repo:

```console
$ .venv/bin/python - <<'PY'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from support import CONFIG_SEARCH
from qaas.config import load_config
from qaas.guardrails import Guardrail
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore

tmp = Path(tempfile.mkdtemp())
target = tmp / "clone"; target.mkdir()
cfg = load_config(search=CONFIG_SEARCH)

def guard(name):
    return Guardrail(ToolContext(
        store=RunStore.new(root=tmp / name), maps=SystemMapStore(tmp / name),
        config=cfg, agent=cfg.agents[name], target_root=target))

def show(g, tool, args):
    d = g.check(tool, args)
    verb = "ALLOW" if d.allowed else "DENY "
    print(f"{verb} {g.agent.name:12} {tool:6} {args.get('file_path') or args.get('command')}")
    if not d.allowed:
        print(f"      -> {d.reason}")

g = guard("CONDUIT")
show(g, "Read",  {"file_path": "api/app/routes/orders.py"})
show(g, "Write", {"file_path": "api/app/routes/orders.py", "content": "..."})

g = guard("FORGE")
show(g, "Write", {"file_path": "qa/repro/test_orders_limit.py", "content": "..."})
show(g, "Write", {"file_path": "api/app/routes/orders.py", "content": "..."})
show(g, "Bash",  {"command": "git checkout -b qa/repro/orders-limit"})
show(g, "Bash",  {"command": "git checkout -b fix/orders-limit"})

g = guard("MENDER")
show(g, "Write", {"file_path": "api/app/routes/orders.py", "content": "..."})
show(g, "Write", {"file_path": ".github/workflows/ci.yml", "content": "..."})
show(g, "Write", {"file_path": "migrations/001_add_index.sql", "content": "..."})
show(g, "Bash",  {"command": "git push --force origin fix/orders-limit"})
show(g, "Bash",  {"command": "git merge main"})
show(g, "Bash",  {"command": "gh pr merge 12"})
PY
ALLOW CONDUIT      Read   api/app/routes/orders.py
DENY  CONDUIT      Write  api/app/routes/orders.py
      -> CONDUIT is read-only. Report what you found; fixing is another agent's job (§2: the finder never fixes).
ALLOW FORGE        Write  qa/repro/test_orders_limit.py
DENY  FORGE        Write  api/app/routes/orders.py
      -> write refused: /…/clone/api/app/routes/orders.py is outside FORGE's sandbox (qa/repro).
ALLOW FORGE        Bash   git checkout -b qa/repro/orders-limit
DENY  FORGE        Bash   git checkout -b fix/orders-limit
      -> branch 'fix/orders-limit' is outside FORGE's patterns (qa/repro/*).
ALLOW MENDER       Write  api/app/routes/orders.py
DENY  MENDER       Write  .github/workflows/ci.yml
      -> .github/workflows/ci.yml is outside MENDER's autonomy envelope: it is CI configuration. Changes here need human approval (§8.2). Describe the change you would make and escalate instead of making it.
DENY  MENDER       Write  migrations/001_add_index.sql
      -> migrations/001_add_index.sql is outside MENDER's autonomy envelope: it is a database migration. Changes here need human approval (§8.2). Describe the change you would make and escalate instead of making it.
DENY  MENDER       Bash   git push --force origin fix/orders-limit
      -> command refused: force-push is never permitted
DENY  MENDER       Bash   git merge main
      -> command refused: merging is a human decision (§8.4)
DENY  MENDER       Bash   gh pr merge 12
      -> command refused: merging a pull request is a human decision (§8.4)
```

(The one elided path is a real `tempfile.mkdtemp()` directory; everything else is
verbatim.)

Every refusal names the rule and says what to do instead. That is not politeness
— it is what makes the agent's next action correct rather than another attempt at
the same wall.

### The diff budget, and a denial reaching the ledger

```console
$ .venv/bin/python - <<'PY'
import asyncio, sys, tempfile
from pathlib import Path
sys.path.insert(0, "tests")
from support import CONFIG_SEARCH
from qaas.config import load_config
from qaas.guardrails import Guardrail
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore

tmp = Path(tempfile.mkdtemp()); target = tmp / "clone"; target.mkdir()
cfg = load_config(search=CONFIG_SEARCH)
store = RunStore.new(root=tmp / "state")
ctx = ToolContext(store=store, maps=SystemMapStore(tmp / "state"), config=cfg,
                  agent=cfg.agents["MENDER"], target_root=target)
g = Guardrail(ctx)

print("max_diff_files =", g.policy.max_diff_files)
for i in range(1, 8):
    d = g.check("Write", {"file_path": f"api/app/f{i}.py", "content": "..."})
    print(f"  f{i}.py  {'ALLOW' if d.allowed else 'DENY'}")
    if not d.allowed:
        print("   ->", d.reason)
        break

print("re-edit f1.py:", g.check("Write", {"file_path": "api/app/f1.py", "content": "..."}).allowed)

asyncio.run(g.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "git merge main"}}, "tu_1", None))
for e in store.ledger("denial"):
    print("ledger denial:", e.agent, e.detail["tool"], "|", e.detail["reason"], "| via", e.detail["via"])
PY
max_diff_files = 5
  f1.py  ALLOW
  f2.py  ALLOW
  f3.py  ALLOW
  f4.py  ALLOW
  f5.py  ALLOW
  f6.py  DENY
   -> MENDER has already changed 5 files, which is its limit of 5 (§8.2). A fix this wide is outside the autonomy envelope: stop, and escalate with what you have found. Already touched: api/app/f1.py, api/app/f2.py, api/app/f3.py, api/app/f4.py, api/app/f5.py.
re-edit f1.py: True
ledger denial: MENDER Bash | command refused: merging is a human decision (§8.4) | via hook
```

Three things in one output: the budget stops at exactly 5 distinct files; the
sixth *edit of an already-touched file* is still allowed; and a call routed
through the real `pre_tool_use` hook produced a `denial` ledger line stamped
`via hook`.

### The test suite

```console
$ .venv/bin/python -m pytest tests/test_guardrails.py -q
..............................................................           [100%]
62 passed in 0.96s
```

Offline, free, under a second. Its docstring
(`tests/test_guardrails.py:1-5`) explains the weight it carries:

```python
"""M3 verification: the §8.1 permission matrix is enforced, not requested.

These are the tests that matter most in the project. If an agent can write
outside its sandbox or push to main, nothing else here is safe to run.
"""
```

---

## 9. Summary

| layer | file | catches |
|---|---|---|
| `PreToolUse` hook | `guardrails.py:140` | every built-in tool call — primary |
| `can_use_tool` | `guardrails.py:127` | calls the allowlist did not auto-approve |
| MCP servers | `tracker.py`, `vcs.py`, `envelope_server.py` | domain rules, MCP arguments the guardrail never sees |
| absence of capability | `vcs.py` | merge and force-push — nothing to call |
| `setting_sources=[]` | `registry.py:439` | the target repo's own settings, hooks and MCP servers |

The design rules, stated once:

- **Enforcement is Python; capability is YAML or a prompt.** A limit an agent
  must not exceed cannot live somewhere the agent can reason about.
- **Both permission paths call one `check()`.** The duplication is in the wiring,
  never in the policy.
- **Default is read-only.** Every capability is an explicit key in an agent's
  policy.
- **A denial is a message, not a crash.** It names the rule and says what to do
  instead, and it lands in the ledger.
- **Prefer no tool over a guarded tool.** There is no merge tool to refuse.
