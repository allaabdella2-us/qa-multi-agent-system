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

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from qaas.mcp.context import ToolContext

# Harness plumbing granted to every agent, independent of its config. These are
# not capability grants: ToolSearch only loads the schemas of servers the agent
# already has, Skill only loads instructions, and neither can reach anything the
# allowlist does not already permit. `build_allowed_tools` adds the same set, and
# both read this constant so the allowlist and the guardrail cannot drift apart —
# a mismatch here silently disables every skill in the system.
ALWAYS_GRANTED = frozenset({"ToolSearch", "Skill", "TodoWrite", "Task", "Agent"})

# Tools that read. Always safe, for every agent.
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"} | set(ALWAYS_GRANTED)

# Harness plumbing, not capability. These grant an agent nothing it was not
# already granted — ToolSearch only loads the schema of a tool that is already
# on its allowlist, and Skill only opens a skill file. Denying ToolSearch is
# worse than useless: MCP tools arrive deferred, so an agent that cannot call it
# cannot reach the servers it was given, and burns its whole turn budget
# discovering that. This system did exactly that once.
HARNESS_TOOLS = {"ToolSearch", "TodoWrite", "Task", "Agent", "Skill", "SlashCommand"}

# Tools that write to the filesystem. Gated on policy.write_paths.
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

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

# Bash that mutates git state. Gated on the agent having branch patterns at all.
GIT_WRITE = re.compile(r"\bgit\s+(commit|push|branch|checkout\s+-b|switch\s+-c|tag|apply|am|rebase)\b")

# Shell constructs that rewrite a file in place. `>` is not a word character, so
# this deliberately does not use \b anchors — an earlier version did and silently
# matched nothing.
_MUTATES_FILE = re.compile(r"(>>?|\btee\b|\bsed\s+-i|\btruncate\b|\bdd\b)")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


class Guardrail:
    """One agent's enforcement of its own policy."""

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

    # -- entry point ------------------------------------------------------

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
        # A refused call produces TWO ledger entries, and that is deliberate.
        # `tool_call` is the universal record -- every call this agent made, in
        # order, allowed or not -- and it is what a timeline reads. `denial`
        # below carries the reason and a summary of the arguments, and is the
        # only place tool arguments are recorded at all.
        #
        # It looks like double-counting and has been reported as such. Dropping
        # either one loses something real: without the `tool_call` the refusal
        # vanishes from the call sequence, and without the `denial` nobody can
        # say why. A reader tallying refusals should count `denial`, not both.
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

    def _record(self, tool_name: str, input_data: dict[str, Any], reason: str, *, via: str) -> None:
        self.ctx.store.log(
            "denial",
            agent=self.agent.name,
            tool=tool_name,
            reason=reason,
            via=via,
            args=_summarise(input_data),
        )

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

    # -- individual gates -------------------------------------------------

    def _check_declared(self, tool_name: str) -> Decision:
        """Defence in depth: the tool must be one this agent declared.

        The SDK is already told the allowlist, so reaching here means something
        upstream drifted. Better to deny and log than to trust the setup.
        """
        if tool_name in self.agent.builtin_tools or tool_name in HARNESS_TOOLS:
            return Decision(True)
        return Decision(
            False,
            f"{tool_name} is not in {self.agent.name}'s tool allowlist "
            f"({', '.join(self.agent.builtin_tools) or 'none'}).",
        )

    def _check_mcp(self, tool_name: str) -> Decision:
        if tool_name.startswith(self._mcp_prefixes):
            return Decision(True)
        server = tool_name.split("__")[1] if "__" in tool_name else "?"
        return Decision(
            False,
            f"{self.agent.name} is not connected to the '{server}' server. "
            f"Its servers are: {', '.join(self.agent.mcp_servers) or 'none'}.",
        )

    def _check_write(self, input_data: dict[str, Any]) -> Decision:
        raw = input_data.get("file_path") or input_data.get("path") or input_data.get("notebook_path")
        if not raw:
            return Decision(False, "write refused: no file path in the call")
        if not self._allowed_roots:
            return Decision(
                False,
                f"{self.agent.name} is read-only. Report what you found; "
                "fixing is another agent's job (§2: the finder never fixes).",
            )

        target = Path(raw)
        resolved = (target if target.is_absolute() else self.root / target).resolve()

        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError:
            relative = resolved.as_posix()

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

        protected = self._protected_path(relative)
        if protected:
            return Decision(
                False,
                f"{relative} is the test that defines success for this ticket and may "
                "not be edited (§10: a fixer that edits the test patches the symptom). "
                "If you believe the test itself is wrong, that is an escalation.",
            )

        for allowed in self._allowed_roots:
            if resolved == allowed or resolved.is_relative_to(allowed):
                return self._check_diff_budget(relative)
        return Decision(
            False,
            f"write refused: {resolved} is outside {self.agent.name}'s sandbox "
            f"({', '.join(self.policy.write_paths)}).",
        )

    def _forbidden_class(self, relative: str) -> str | None:
        """Which §8.2 class this path falls into, if any."""
        for pattern in self.policy.forbidden_paths:
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern):
                return _describe_forbidden(pattern)
        return None

    def _protected_path(self, relative: str) -> bool:
        return any(
            fnmatch.fnmatch(relative, p) or relative.endswith(p)
            for p in self.policy.protected_paths
        )

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

    def _check_bash(self, input_data: dict[str, Any]) -> Decision:
        command = str(input_data.get("command", ""))
        if not command.strip():
            return Decision(False, "empty command")

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

        if self.policy.protected_paths:
            for protected in self.policy.protected_paths:
                if protected in command and _MUTATES_FILE.search(command):
                    return Decision(
                        False,
                        f"'{protected}' is protected: it defines what a fix must achieve "
                        "and may not be edited (§10, symptom fixes).",
                    )

        return Decision(True)


def _branch_from_command(command: str) -> str | None:
    """Best-effort branch name out of a git command, for policy matching."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for i, token in enumerate(parts):
        if token in {"-b", "-c"} and i + 1 < len(parts):
            return parts[i + 1]
        if token in {"branch", "switch"} and i + 1 < len(parts):
            candidate = parts[i + 1]
            if not candidate.startswith("-"):
                return candidate
    return None


# Human-readable names for the forbidden classes, so a denial explains itself.
_FORBIDDEN_DESCRIPTIONS = [
    ("migration", "a database migration"),
    ("auth", "authentication or authorization code"),
    ("payment", "a payment path"),
    ("billing", "a billing path"),
    ("secret", "secret material"),
    ("infra", "infrastructure configuration"),
    ("terraform", "infrastructure configuration"),
    (".tf", "infrastructure configuration"),
    ("docker", "container or deployment configuration"),
    ("k8s", "container or deployment configuration"),
    ("kube", "container or deployment configuration"),
    (".github", "CI configuration"),
    ("workflow", "CI configuration"),
]


def _describe_forbidden(pattern: str) -> str:
    lowered = pattern.lower()
    for needle, description in _FORBIDDEN_DESCRIPTIONS:
        if needle in lowered:
            return description
    return f"matched by the forbidden pattern `{pattern}`"


def _hook_field(payload: Any, name: str) -> Any:
    """Hook payloads arrive as dicts or dataclasses depending on SDK version."""
    if isinstance(payload, dict):
        return payload.get(name)
    return getattr(payload, name, None)


def _summarise(input_data: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    """Ledger-sized view of a tool call: enough to audit, not a content dump."""
    out: dict[str, Any] = {}
    for key, value in input_data.items():
        if key in {"content", "new_string", "old_string"}:
            out[key] = f"<{len(str(value))} chars>"
        else:
            text = str(value)
            out[key] = text if len(text) <= limit else text[:limit] + "…"
    return out
