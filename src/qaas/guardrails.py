"""The §8.1 write-permission matrix, enforced in code.

Every agent gets a `can_use_tool` callback built from its policy. The callback
sees the tool name and its arguments before the tool runs, which is the only
place a limit like "REPRODUCER may write, but only under qa/repro" can actually be
imposed. A prompt asking an agent not to do something is a request; this is a
decision.

Enforcement runs in the **PreToolUse hook**, not in `can_use_tool`. This is not
a stylistic choice and it is easy to get wrong: an `allowed_tools` entry that
names a whole tool auto-approves it *before* `can_use_tool` is consulted, so a
policy implemented only in that callback is silently never applied. The SDK warns
about this shadowing, and an early version of this file had exactly that bug —
REPRODUCER's sandbox check was dead code. The hook sees every call regardless.

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
#
# Derived from `ALWAYS_GRANTED` rather than restated. The comment on that
# constant claims `build_allowed_tools` and the guardrail "both read this
# constant so the allowlist and the guardrail cannot drift apart" -- and they
# did drift: `registry.build_allowed_tools` read `ALWAYS_GRANTED`, while
# `_check_declared` read this set, which had an extra `SlashCommand` in it. So
# the guardrail approved a tool the allowlist never granted. Harmless as it
# happened, and precisely the mismatch the comment says cannot occur.
HARNESS_TOOLS = set(ALWAYS_GRANTED)

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
    # `-[a-zA-Z]*[rf]` caught `-rf` and `-r`, and missed `-R`, `--recursive` and
    # `--force` — three spellings of the same command, none of them exotic.
    (
        r"\brm\s+(-[a-zA-Z]*[rfR]|--(recursive|force)\b)",
        "recursive or forced delete is not permitted",
    ),
    (r"\bsudo\b", "privilege escalation is not permitted"),
    (r"\b(shutdown|reboot|mkfs|dd)\b", "destructive system command"),
    (r">\s*/dev/(sd|nvme|disk)", "writing to a block device"),
    (r"\bdocker\s+system\s+prune", "prune would destroy state other runs depend on"),
    (r"\bgh\s+pr\s+merge\b", "merging a pull request is a human decision (§8.4)"),
]

# Bash that mutates git state. Gated on the agent having branch patterns at all.
# `checkout`, `restore`, `clean`, `stash` and `rm` join the list because they
# change the working tree even when they move no branch -- `git checkout --
# api/app/auth.py` reverts a file a read-only agent may not touch, and neither
# this nor `_writes_of` had heard of it.
GIT_WRITE = re.compile(
    r"\bgit\s+(commit|push|branch|checkout|restore|clean|stash|rm|switch|tag|apply|am|rebase)\b"
)

# Shell constructs that rewrite a file in place. `>` is not a word character, so
# this deliberately does not use \b anchors — an earlier version did and silently
# matched nothing.
_MUTATES_FILE = re.compile(
    r"(>>?|\btee\b|\bsed\s+(-i|--in-place)|\btruncate\b|\bdd\b|\brm\b|\bmv\b|\bgit\s+(rm|checkout|restore)\b)"
)

# -- what a shell command writes --------------------------------------------
#
# `_check_bash` used to consult only FORBIDDEN_BASH, the branch patterns and a
# substring test against `protected_paths`. Every other rule in the §8.1 matrix
# — `write_paths`, `forbidden_paths`, the §8.2 diff budget — was enforced for
# `Write`/`Edit` and bypassed entirely by a shell command:
#
#     FIXER     Write api/app/auth.py             -> denied
#     FIXER     sed -i '' s/x/y/ api/app/auth.py  -> allowed
#     VERIFIER  tee /etc/hosts < x                -> allowed
#
# VERIFIER is the verification gate and has `Bash` with no `write_paths`, so it was
# "read-only" only against `Write` — §2's finder/fixer separation gone.
#
# Reading a shell command is best-effort by nature: a write can always hide one
# level of indirection further than a parser follows. The rule that makes that
# acceptable is below — a command that mutates and whose destination cannot be
# resolved is *refused*, not guessed at, which leaves the enforced tools
# (`Write`/`Edit`) as the only way to do the thing.

#: A command whose every non-flag argument is a destination.
_WRITES_ALL_ARGS = frozenset({"tee", "touch", "truncate"})
#: A command whose last argument is the destination.
_WRITES_LAST_ARG = frozenset({"cp", "mv", "install", "ln", "rsync"})
#: A command whose every non-flag argument is destroyed. Deletion is a write --
#: the §8.1 matrix is about what an agent may *change*, and removing a file
#: changes it more completely than editing it does. `rm` without `-r`/`-f` sat
#: outside FORBIDDEN_BASH and outside `_writes_of`, so `rm api/app/auth.py`
#: cleared every gate that `Write api/app/auth.py` fails.
_DELETES_ALL_ARGS = frozenset({"rm", "unlink", "shred"})
#: Interpreters that write wherever an inline script tells them to.
_SCRIPT_RUNNERS = frozenset({"python", "python3", "perl", "ruby", "node", "bash", "sh", "zsh", "php"})
_INLINE_SCRIPT_FLAGS = frozenset({"-c", "-e"})
#: Wrappers that run another command. `parts[0]` is the wrapper, so the mutation
#: test has to be applied to what it wraps -- `env sed -i`, `timeout 5 sed -i`
#: and `nice sed -i` all reached protected paths while `sed -i` alone did not.
_WRAPPERS = frozenset({"env", "timeout", "nice", "nohup", "command", "stdbuf", "setsid", "ionice"})
#: Commands that run *something else, chosen at runtime*. There is no argument
#: list to resolve, so these are refused outright rather than guessed at.
_INDIRECT = frozenset({"xargs", "eval", "exec", "source", "."})
#: Flags that take a value, so the value is not mistaken for a path. `-t` is
#: NOT here: for the `cp`/`mv`/`install` family it names the *destination*, and
#: dropping it as a flag value sent `cp -t api/app /tmp/evil.py` through
#: unchecked. It is handled explicitly in `_writes_of` instead.
_FLAG_TAKES_VALUE = frozenset({"-s", "-m", "-o", "--suffix", "--size", "--mode"})
#: Spellings of "rewrite this file in place", long and short.
_TARGET_DIR_FLAGS = ("-t", "--target-directory")

#: Redirect targets that are not files anyone can be harmed through.
_DEV_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})

# `> out`, `>>out`, `2> err`, `>| out` — but not `2>&1`, whose `&1` is a file
# descriptor. `>|` is bash's noclobber override and is a perfectly ordinary
# write; without the `\|?` the target of `echo x >| f` was never captured.
_REDIRECT_RE = re.compile(r">>?\|?\s*(?!&)([^\s;|&<>]+)")
#: Command substitution. Whatever is inside runs, and this module cannot see
#: through it -- `echo $(rm api/app/auth.py)` deleted a file while presenting as
#: an `echo`. Its presence alone makes a segment undeterminable.
_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{[^}]*[|;&]")
#: Shell operators that end one command and begin the next, as `shlex` with
#: `punctuation_chars=True` tokenises them.
_OPERATORS = frozenset({";", "&", "|", "&&", "||", "\n", "|&"})


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


#: What a diff-budget refusal says, so the router can recognise one in the
#: ledger without matching on prose that may be reworded. The guardrail can
#: only refuse the write; it cannot end the turn, and the agent reading
#: "stop, and escalate with what you have found" has no channel to escalate on.
#: So the router reads this back and escalates on the agent's behalf.
DIFF_BUDGET_REFUSAL = "(§8.2)"


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
        # A glob cannot be resolved to a directory to contain things in, so the
        # two kinds are kept apart and tested differently. `mcp/vcs.py` already
        # honoured globs in `write_paths` and this did not; now that both go
        # through `_check_path`, the more permissive of the two would have
        # silently become the rule for everything.
        self._allowed_roots = [
            (self.root / p).resolve() for p in self.policy.write_paths if not _is_glob(p)
        ]
        self._allowed_globs = [p for p in self.policy.write_paths if _is_glob(p)]
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
            if not self.policy.write_paths:
                return self._read_only_decision()
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

    def _read_only_decision(self) -> Decision:
        """One wording, wherever an agent with no write paths tries to write.

        The reason an agent reads must not depend on which door it reached for:
        it is the same policy, and a different sentence per tool reads as a
        different rule and invites it to go looking for the permissive one.
        """
        return Decision(
            False,
            f"{self.agent.name} is read-only. Report what you found; "
            "fixing is another agent's job (§2: the finder never fixes).",
        )

    def _check_write(self, input_data: dict[str, Any]) -> Decision:
        raw = input_data.get("file_path") or input_data.get("path") or input_data.get("notebook_path")
        if not raw:
            return Decision(False, "write refused: no file path in the call")
        return self._check_path(str(raw))

    def _check_path(self, raw: str, *, count_against_budget: bool = True) -> Decision:
        """The §8.1/§8.2 matrix, applied to one path an agent wants to write.

        Split out of `_check_write` so it is the single place the rules live:
        `Write`/`Edit` reach it through `check`, a shell command reaches it
        through `_check_bash`, and `mcp/vcs.py` reaches it instead of keeping
        its own near-copy. Three doors, one answer.
        """
        if not self.policy.write_paths:
            return self._read_only_decision()

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

        # `count_against_budget=False` reads the matrix without spending
        # anything. `_check_diff_budget` *mutates* `touched_files`, and
        # `mcp/vcs.py:commit` validates every staging pathspec through here --
        # so committing `api/app` and `web/src`, which is what FIXER's own
        # `write_paths` are, charged two entries to a budget of five files
        # before a single line had changed. A fixer that touched four files
        # could not commit them.
        for allowed in self._allowed_roots:
            if resolved == allowed or resolved.is_relative_to(allowed):
                return self._check_diff_budget(relative) if count_against_budget else Decision(True)
        # Containment first, then the pattern. The root branch above proves
        # containment with `is_relative_to`; this one only ever matched a
        # string, and `relative` falls back to the *absolute* path when the
        # target is outside the root -- so with a policy of `*_test.py`,
        # `fnmatch("/etc/x_test.py", "*_test.py")` is True, because fnmatch's
        # `*` crosses `/`. A glob write path escaped the checkout entirely. No
        # shipped agent uses one today, which is exactly why it went unnoticed.
        if resolved == self.root or resolved.is_relative_to(self.root):
            for pattern in self._allowed_globs:
                if fnmatch.fnmatch(relative, pattern):
                    return (
                        self._check_diff_budget(relative)
                        if count_against_budget
                        else Decision(True)
                    )
        return Decision(
            False,
            f"write refused: {resolved} is outside {self.agent.name}'s sandbox "
            f"({', '.join(self.policy.write_paths)}).",
        )

    def _forbidden_class(self, relative: str) -> str | None:
        """Which §8.2 class this path falls into, if any.

        Case-folded on both sides. Every pattern in the shipped policies is
        lowercase (`*auth*`, `*migration*`, `*secret*`) and `fnmatch` on POSIX
        is case-sensitive -- while macOS, where most of this is developed and
        much of it is run, is not. So `api/app/Auth.py` named the same file as
        `api/app/auth.py` and matched none of the patterns guarding it. Folding
        here and not in the YAML because a cased variant per pattern is a list
        that will be incomplete again the next time someone adds a class.
        """
        lowered = relative.lower()
        name = Path(relative).name.lower()
        for pattern in self.policy.forbidden_paths:
            low = pattern.lower()
            if fnmatch.fnmatch(lowered, low) or fnmatch.fnmatch(name, low):
                return _describe_forbidden(pattern)
        return None

    def _protected_path(self, relative: str) -> bool:
        lowered = relative.lower()
        return any(
            fnmatch.fnmatch(lowered, p.lower()) or lowered.endswith(p.lower())
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
                f"its limit of {max_files} {DIFF_BUDGET_REFUSAL}. A fix this wide is outside the "
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
                if protected.lower() in command.lower() and _MUTATES_FILE.search(command):
                    return Decision(
                        False,
                        f"'{protected}' is protected: it defines what a fix must achieve "
                        "and may not be edited (§10, symptom fixes).",
                    )

        refspec = self._refspec_refusal(command)
        if refspec:
            return refspec

        return self._check_bash_writes(command)

    def _refspec_refusal(self, command: str) -> Decision | None:
        """`git push` parses its argument as a *refspec*, not as a branch name.

        `mcp/vcs.py` already learned this -- `_reject_refspec` is there because
        `git push origin qa/repro/x:main` published onto main past every
        branch-pattern and protected-name check. The shell door never learned
        it: `FORBIDDEN_BASH` matches only `--force`/`-f` and the literal words
        `main`/`master`, and `_branch_from_command` reads a name only after
        `-b`, `-c`, `branch` or `switch`, so a refspec returns None and the
        branch-pattern gate below is skipped entirely. Same rule, third door.
        """
        segments = _shell_segments(command)
        for parts in segments:
            peeled = _peel_wrappers(parts)
            if not peeled or Path(peeled[0]).name != "git":
                continue
            if "push" not in peeled[1:3]:
                continue
            for token in _non_flag_args(peeled)[2:]:  # after `push` and the remote
                if ":" in token or token.startswith("+"):
                    return Decision(
                        False,
                        f"refusing 'git push … {token}': that is a refspec, not a branch "
                        "name. A refspec can publish any local ref onto any remote ref, "
                        "which is how a sandboxed branch reaches main past every branch "
                        "pattern. Push the branch by its own name.",
                    )
        return None

    def _check_bash_writes(self, command: str) -> Decision:
        """Apply the write matrix to what the command would actually write.

        This is the half `_check_bash` never had. Every rule below already held
        for `Write` and `Edit`; reaching them through a shell is not a different
        permission, so it does not get a different answer.
        """
        if _tokenise(command) is None:
            return Decision(
                False,
                "refusing this command: it cannot be parsed as a shell command "
                "(unbalanced quote), so nothing can say what it writes. Use Write "
                "or Edit, which name the file they change.",
            )
        for index, segment in enumerate(_shell_segments(command)):
            paths, undeterminable = _writes_of(
                segment, command, piped_into=index in _piped_into(command)
            )
            if undeterminable:
                if not self.policy.write_paths:
                    return self._read_only_decision()
                return Decision(
                    False,
                    f"refusing '{shlex.join(segment)}': {undeterminable}, so this cannot "
                    "be checked against your write paths. Use Write or Edit, which name "
                    "the file they change.",
                )
            for raw in paths:
                decision = self._check_path(raw)
                if not decision.allowed:
                    return decision
        return Decision(True)


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _tokenise(command: str) -> list[str] | None:
    """The whole command as shell tokens, or None if it cannot be read.

    One quote-aware pass replaces what used to be two passes that disagreed: a
    regex split on `[;|&\\n]` followed by `shlex.split` per piece. The regex
    could not see quoting, so `echo 'hello; world' && ls` was cut into three
    nonsense fragments -- and, the other way round, `echo x >| f` was cut at the
    `|` of `>|`, leaving a dangling `>` whose target was never checked and a
    bare `f` that looked like a command.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # An unbalanced quote. We cannot read it, so we cannot clear it.
        return None


def _shell_segments(command: str) -> list[list[str]]:
    """One command per element, as its token list.

    `ls && sed -i ... f` is two commands, and only the second writes.
    """
    tokens = _tokenise(command)
    if tokens is None:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _OPERATORS:
            if current:
                segments.append(current)
            current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _piped_into(command: str) -> set[int]:
    """Indices of the segments that receive another command's output.

    Needed because `curl … | sh` hides the program entirely: the `sh` segment is
    the single token `sh`, with no `-c` and no script file, and its actual
    program arrives on stdin. Nothing in argv says what it writes.
    """
    tokens = _tokenise(command)
    if tokens is None:
        return set()
    piped: set[int] = set()
    index = 0
    for token in tokens:
        if token in _OPERATORS:
            if token in {"|", "|&"}:
                piped.add(index + 1)
            index += 1
    return piped


def _non_flag_args(parts: list[str]) -> list[str]:
    """argv[1:] with options — and the values they consume — removed."""
    args: list[str] = []
    skip = False
    for token in parts[1:]:
        if skip:
            skip = False
            continue
        if token == "--":
            continue
        if token.startswith("-") and token != "-":
            skip = token in _FLAG_TAKES_VALUE
            continue
        args.append(token)
    return args


def _target_directory(parts: list[str]) -> str | None:
    """The `-t DIR` / `--target-directory=DIR` destination, if one is given."""
    for i, token in enumerate(parts):
        if token in _TARGET_DIR_FLAGS and i + 1 < len(parts):
            return parts[i + 1]
        if token.startswith("--target-directory="):
            return token.split("=", 1)[1]
    return None


def _is_in_place_sed(parts: list[str]) -> bool:
    """Both spellings. `"--in-place".startswith("-i")` is False -- the second
    character is a dash -- so the old test cleared `sed --in-place`, which is
    the same command with a longer name."""
    return any(
        part == "-i" or (part.startswith("-i") and not part.startswith("--"))
        or part == "--in-place" or part.startswith("--in-place=")
        for part in parts
    )


def _paths_after_double_dash(parts: list[str]) -> list[str]:
    """Everything after `--`, which is how git spells "these are paths"."""
    return parts[parts.index("--") + 1:] if "--" in parts else []


def _writes_of(
    segment: list[str], raw: str, *, piped_into: bool = False
) -> tuple[list[str], str | None]:
    """What one command writes: (paths, why the destination is undeterminable).

    A non-None second element means "this mutates and I cannot say where", which
    the caller must treat as a refusal rather than as an empty path list. The two
    are deliberately different: no paths and no reason means the command writes
    nothing and is none of our business.

    The default used to be the wrong way round. Anything whose `parts[0]` was
    not on one of the tables below fell through to `([], None)` -- "writes
    nothing" -- which is the opposite of what the module says about itself, and
    it made every mutator reachable by putting one word in front of it. Verified
    against the shipped roster: read-only VERIFIER could run `env sed -i`,
    `timeout 5 sed -i`, `xargs sed -i`, `find -exec sed -i`, `curl | sh` and
    `echo $(rm f)` against paths FIXER itself is forbidden.
    """
    targets = [t for t in _REDIRECT_RE.findall(raw) if t not in _DEV_SINKS]

    # Command substitution runs a command this function cannot see. Refuse
    # before looking at argv0 -- the visible command is irrelevant.
    if _SUBSTITUTION_RE.search(raw):
        return targets, "it contains command substitution, which can run anything"
    if not segment:
        return targets, None

    parts = _peel_wrappers(segment)
    if not parts:
        return targets, "it is a wrapper with nothing to wrap"
    name = Path(parts[0]).name
    args = _non_flag_args(parts)

    if name in _INDIRECT:
        return targets, f"`{name}` runs a command chosen at runtime"
    if name in _SCRIPT_RUNNERS:
        if any(flag in parts for flag in _INLINE_SCRIPT_FLAGS):
            return targets, f"an inline {name} script can write anywhere"
        if not args and piped_into:
            # `curl … | sh`. A shell with no script argument at the end of a pipe
            # is reading its program from stdin, which is the one case where the
            # program is not in the command at all. Refused for the same reason
            # `-c` is: there is no destination to resolve.
            return targets, f"`{name}` at the end of a pipe runs a script from stdin"
        # NOT refused: `python foo.py`, `python -m pytest`. Both are arbitrary
        # code and both could write anywhere, and that is a real residual limit
        # of reading a shell command rather than a gap nobody noticed. Refusing
        # them was tried here and refuses `python -m pytest tests/ -x`, which is
        # how FIXER and VERIFIER run the suite -- a guardrail that blocks the
        # system's own happy path is a guardrail that gets switched off. The
        # enforced tools (`Write`/`Edit`) and the agent's `write_paths` remain
        # the boundary for anything a script leaves behind.
    if name == "find":
        # `-exec`/`-execdir`/`-ok` run another command per match; `-delete` and
        # `-fprint` write directly. None of them has a destination this can name.
        if any(p in {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint"} for p in parts):
            return targets, "`find` is running an action over paths it chooses itself"
    if name == "patch" or (name == "git" and "apply" in parts[1:3]):
        return targets, "a patch carries its own destinations"
    if name == "git":
        return _git_writes(parts, targets)
    if name == "sed" and _is_in_place_sed(parts):
        # `sed -i '' s/x/y/ f.py` (BSD) and `sed -i s/x/y/ f.py` (GNU) differ by
        # an empty argument. Drop the empties, then drop the script expression;
        # whatever is left is a file being rewritten in place.
        non_empty = [a for a in args if a]
        return targets + non_empty[1:], None
    if name in _DELETES_ALL_ARGS:
        return targets + args, None
    if name in _WRITES_ALL_ARGS:
        return targets + args, None
    if name in _WRITES_LAST_ARG and args:
        # `-t DIR` inverts the shape: the destination is the flag's value and
        # every positional is a source. `mv`'s sources are destroyed too, so
        # they are writes in their own right.
        into = _target_directory(parts)
        destinations = [into] if into else args[-1:]
        sources = args if into else args[:-1]
        if name == "mv":
            destinations = destinations + sources
        return targets + destinations, None
    return targets, None


def _looks_like_wrapper_operand(token: str) -> bool:
    """A token that belongs to the wrapper, not to the command it wraps.

    Three shapes, and nothing else: an option (`-n`, `--foreground`), an
    environment assignment (`FOO=1`, which is how `env` takes its arguments),
    and a bare duration or niceness (`5`, `1.5`, `30s`).
    """
    if token.startswith("-"):
        return True
    if "=" in token and not token.startswith("/"):
        return True
    return token.rstrip("smhd").replace(".", "", 1).isdigit()


def _peel_wrappers(parts: list[str]) -> list[str]:
    """Strip `env FOO=1`, `timeout 5`, `nice -n 10` … down to the real command.

    Bounded at four layers because a fifth is not a command anyone types, and an
    unbounded loop over agent-supplied argv is not a thing to have.
    """
    for _ in range(4):
        if not parts or Path(parts[0]).name not in _WRAPPERS:
            return parts
        rest = parts[1:]
        while rest and _looks_like_wrapper_operand(rest[0]):
            rest = rest[1:]
        parts = rest
    return parts


def _git_writes(parts: list[str], targets: list[str]) -> tuple[list[str], str | None]:
    """Git subcommands that change the working tree.

    `GIT_WRITE` gates the ones that move a *branch*; these move *files*, and
    neither table knew about them. `git checkout -- api/app/auth.py` reverts a
    protected file, `git rm` deletes it, and `git clean`/`git stash` remove work
    across paths chosen from the index rather than from argv.
    """
    sub = next((p for p in parts[1:] if not p.startswith("-")), "")
    if sub in {"clean", "stash"}:
        return targets, f"`git {sub}` chooses its own paths from the index"
    if sub == "rm":
        return targets + _non_flag_args(parts)[1:], None
    if sub in {"checkout", "restore"}:
        paths = _paths_after_double_dash(parts)
        if paths:
            return targets + paths, None
        if sub == "restore":
            return targets + _non_flag_args(parts)[1:], None
    return targets, None


def _branch_from_command(command: str) -> str | None:
    """Best-effort branch name out of a git command, for policy matching."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    # Only a git command names a branch. Without this, `-c` was read as
    # `switch -c` in anything that happens to take one: `python -c '...'` was
    # refused as "branch 'open(...)' is outside FIXER's patterns", which is
    # both a wrong answer and an unactionable one.
    if not any(Path(part).name == "git" for part in parts[:2]):
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
