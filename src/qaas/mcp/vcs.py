"""The `vcs` MCP server — the §8.1 write matrix, enforced where the write happens.

REPRODUCER needs a branch and a committed failing test; every other Phase 1 agent
needs none of that. The difference is one line of YAML (`policy.branch_patterns`,
`policy.write_paths`) and this module is what makes that line true. An agent
cannot argue its way past a refusal here the way it can past a prompt, and every
refusal lands in the ledger as a `denial` so the audit trail in §8 is real.

Three rules the tools below exist to keep:

  * `main` and `master` are refused for everyone, always, even if a policy were
    misconfigured to name them. Merging is a human act (§8.4).
  * Force-push is not a tool. There is nothing to refuse because there is
    nothing to call.
  * A write path is checked after resolution, not before, so `..`, an absolute
    path and a symlink out of the sandbox all fail the same check.
  * Pushing and opening a PR ride one permission (`policy.may_open_pr`), because
    publishing a branch to a shared remote exposes the same work the PR would.
    There is no merge tool: §8.4 makes merging a human act.

The guardrail never sees an MCP tool's arguments, so this module is where the
matrix is applied to them — but it does not restate it. `_path_refusal` calls
`Guardrail._check_path`, the same function that answers for Write and Edit.
It used to hold a near-copy instead, and the copy had drifted: it had never
learned about `forbidden_paths`, so `write_file` on `api/app/auth.py` was
allowed at the exact moment `Edit` on it was refused.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas.adapters.vcs import VcsAdapter, VcsError, build_vcs
from qaas.guardrails import Guardrail
from qaas.mcp.context import ToolContext, err, ok

# Refused for every agent regardless of policy. A policy that names one of these
# is a misconfiguration, and this is the layer that survives it.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "release"})

# Cap on a single agent-authored file. A repro case that needs more than this is
# not minimal, which is REPRODUCER's actual job (§4.11).
MAX_WRITE_BYTES = 512_000


def _deny(ctx: ToolContext, tool_name: str, reason: str) -> dict[str, Any]:
    """Refuse, log, and hand the agent the reason it needs to adapt."""
    ctx.store.log("denial", agent=ctx.agent.name, tool=tool_name, reason=reason)
    return err(reason)


def _branch_refusal(ctx: ToolContext, branch: str) -> str | None:
    """Why `branch` is off limits for this agent, or None if it is allowed."""
    patterns = ctx.agent.policy.branch_patterns
    if not patterns:
        return (
            f"{ctx.agent.name} has no branch patterns in its policy and may not "
            "create branches or commit. Report what you found instead "
            "(§2: the finder never fixes)."
        )
    if branch.lower() in PROTECTED_BRANCHES:
        return (
            f"'{branch}' is a protected branch. No agent writes to it, ever (§8.1). "
            f"Work on one of {', '.join(patterns)}."
        )
    if not any(fnmatch.fnmatch(branch, pattern) for pattern in patterns):
        return (
            f"branch '{branch}' is outside {ctx.agent.name}'s patterns "
            f"({', '.join(patterns)}). Create a branch that matches one of them first."
        )
    return None


def _publish_refusal(ctx: ToolContext, branch: str) -> str | None:
    """Why this agent may not publish `branch`, or None if it may.

    Push and open_pr share one gate. Putting a branch on the shared remote is
    the same act of exposure as opening the pull request that follows it, so
    §8.1's "pull requests: open only, and only for the remediation agent" has to
    cover both or it covers neither.
    """
    if not ctx.agent.policy.may_open_pr:
        return (
            f"{ctx.agent.name}'s policy does not grant may_open_pr, so it may not "
            "push branches or open pull requests (§8.1: pull requests are the "
            "remediation agent's, open only). Commit locally and report instead."
        )
    return _branch_refusal(ctx, branch)


def _remote_refusal(adapter: VcsAdapter, capability: str) -> str | None:
    """The local backend has no remote; say so rather than raise AttributeError."""
    if not hasattr(adapter, capability):
        return (
            f"the configured vcs backend ({type(adapter).__name__}) has no remote, so "
            f"'{capability}' does not exist. Set `vcs: github` in config/system.yaml "
            "to work against a GitHub repository."
        )
    return None


def _path_refusal(ctx: ToolContext, raw: str) -> tuple[Path | None, str | None]:
    """Resolve a repo-relative path and say why it is off limits, if it is.

    Resolution happens before the containment check so `..`, an absolute path
    and a symlink pointing out of the sandbox are all caught by the same test.

    The *verdict* comes from `Guardrail._check_path` — the same code that answers
    for `Write` and `Edit`. This used to be a near-copy of it, and the copy had
    drifted: it honoured `write_paths` and `protected_paths` and had never
    learned about `forbidden_paths`, so `mcp__vcs__write_file` wrote
    `api/app/auth.py` happily while `Edit` on that path was refused as outside
    the §8.2 autonomy envelope. One rule cannot be true at one door and false at
    another; the way to guarantee that is to have one implementation.

    `Guardrail.check` is deliberately not what is called here: it would also run
    `_check_declared("Write")`, and an agent holding this server need not carry
    the `Write` builtin at all — it would be refused for a reason that is not
    the true one.
    """
    root = ctx.target_root.resolve()
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()

    # Kept here rather than deferred: this message names the checkout, which is
    # the useful thing to say about a path that left it.
    if resolved != root and not resolved.is_relative_to(root):
        return None, (
            f"write refused: '{raw}' resolves to {resolved}, outside the repository "
            f"({root}). Paths must stay inside the checkout."
        )

    decision = Guardrail(ctx)._check_path(raw)
    if not decision.allowed:
        return None, decision.reason
    return resolved, None


def build_tools(ctx: ToolContext) -> list:
    """The vcs tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """
    adapter: dict[str, VcsAdapter] = {}

    def vcs() -> VcsAdapter:
        """Built on first use: the GitHub backend raises on construction, and a
        read-only agent should not eat that error just for loading the server."""
        if "adapter" not in adapter:
            adapter["adapter"] = build_vcs(ctx.config.vcs, ctx.target_root)
        return adapter["adapter"]

    @tool(
        "current_branch",
        "Name of the branch currently checked out. Check this before writing: "
        "writes are refused on any branch outside your policy.",
        {"type": "object", "properties": {}},
    )
    async def current_branch(args: dict[str, Any]) -> dict[str, Any]:
        try:
            branch = vcs().current_branch()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        allowed = _branch_refusal(ctx, branch) is None
        note = "" if allowed else " You may not write on it."
        return ok(f"On branch '{branch}'.{note}", branch=branch, writable=allowed)

    @tool(
        "create_branch",
        "Create and switch to a branch. It must match your policy's branch patterns "
        "(REPRODUCER: qa/repro/*). main and master are refused for every agent.",
        {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "description": "e.g. 'qa/repro/PROJ-1284-order-500'"},
                "from_ref": {"type": "string", "description": "Base ref. Defaults to the current HEAD."},
            },
        },
    )
    async def create_branch(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["name"]).strip()
        if not name:
            return err("name is required.")
        refusal = _branch_refusal(ctx, name)
        if refusal:
            return _deny(ctx, "create_branch", refusal)
        try:
            branch = vcs().create_branch(name, args.get("from_ref"))
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        ctx.store.log("vcs", agent=ctx.agent.name, action="create_branch", branch=branch)
        return ok(f"Created and switched to '{branch}'.", branch=branch)

    @tool(
        "write_file",
        "Write one file inside your sandbox. The path is resolved first, so '..', "
        "absolute paths and symlinks that leave the sandbox are all refused.",
        {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "Repo-relative, e.g. 'qa/repro/test_order_500.py'"},
                "content": {"type": "string"},
            },
        },
    )
    async def write_file(args: dict[str, Any]) -> dict[str, Any]:
        raw = str(args["path"])
        content = args.get("content")
        if not isinstance(content, str):
            return err("content must be a string.")
        if len(content.encode()) > MAX_WRITE_BYTES:
            return err(
                f"content is {len(content.encode())} bytes; the cap is {MAX_WRITE_BYTES}. "
                "A minimal repro should be far smaller than this."
            )

        resolved, refusal = _path_refusal(ctx, raw)
        if refusal:
            return _deny(ctx, "write_file", refusal)

        try:
            branch = vcs().current_branch()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        branch_refusal = _branch_refusal(ctx, branch)
        if branch_refusal:
            return _deny(
                ctx,
                "write_file",
                f"refusing to write while on '{branch}': {branch_refusal}",
            )

        assert resolved is not None  # _path_refusal returns one or the other
        relative = resolved.relative_to(ctx.target_root.resolve()).as_posix()
        try:
            vcs().write_files({relative: content})
        except OSError as exc:
            return err(f"could not write {relative}: {exc}")
        ctx.store.log("vcs", agent=ctx.agent.name, action="write_file", path=relative, branch=branch)
        return ok(f"Wrote {relative} ({len(content)} chars) on {branch}.", path=relative, branch=branch)

    @tool(
        "commit",
        "Commit your sandbox changes on the current branch. Only files inside your "
        "write paths are staged; committing on a branch outside your policy is refused.",
        {
            "type": "object",
            "required": ["message"],
            "properties": {
                "message": {"type": "string", "description": "Why this commit exists, not what it changes."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative paths to stage. Defaults to your whole sandbox.",
                },
            },
        },
    )
    async def commit(args: dict[str, Any]) -> dict[str, Any]:
        message = str(args["message"]).strip()
        if not message:
            return err("message is required.")
        if not ctx.agent.policy.write_paths:
            return _deny(
                ctx,
                "commit",
                f"{ctx.agent.name} is read-only: its policy declares no write paths.",
            )

        try:
            branch = vcs().current_branch()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        refusal = _branch_refusal(ctx, branch)
        if refusal:
            return _deny(ctx, "commit", f"refusing to commit on '{branch}': {refusal}")

        requested = args.get("paths") or ctx.agent.policy.write_paths
        staged: list[str] = []
        for raw in requested:
            resolved, path_refusal = _path_refusal(ctx, raw)
            if path_refusal:
                return _deny(ctx, "commit", path_refusal)
            assert resolved is not None
            staged.append(resolved.relative_to(ctx.target_root.resolve()).as_posix())

        try:
            sha = vcs().commit(message, staged)
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        ctx.store.log("vcs", agent=ctx.agent.name, action="commit", branch=branch, sha=sha)
        return ok(f"Committed {sha[:10]} on {branch}.", sha=sha, branch=branch, paths=staged)

    @tool(
        "diff",
        "Unified diff of the working tree, optionally against a ref. Read-only.",
        {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Compare against this ref, e.g. 'main' or a sha."},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Limit to these paths."},
            },
        },
    )
    async def diff(args: dict[str, Any]) -> dict[str, Any]:
        try:
            patch = vcs().diff(args.get("ref"), args.get("paths"))
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        if not patch.strip():
            return ok("No changes.", empty=True)
        truncated = patch[:60_000]
        return ok(truncated, truncated=len(patch) > len(truncated), lines=patch.count("\n"))

    @tool(
        "list_branches",
        "Local branch names. Read-only.",
        {"type": "object", "properties": {}},
    )
    async def list_branches(args: dict[str, Any]) -> dict[str, Any]:
        try:
            branches = vcs().list_branches()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        return ok(", ".join(branches) or "(no branches yet)", branches=branches)

    @tool(
        "push",
        "Publish a branch to the remote. Requires may_open_pr in your policy; main, "
        "master, trunk, develop and release* are refused as the branch to push. "
        "There is no force-push.",
        {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Defaults to the branch you are on."},
            },
        },
    )
    async def push(args: dict[str, Any]) -> dict[str, Any]:
        try:
            adapter = vcs()
            branch = str(args.get("branch") or "").strip() or adapter.current_branch()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))

        refusal = _publish_refusal(ctx, branch)
        if refusal:
            return _deny(ctx, "push", refusal)
        unsupported = _remote_refusal(adapter, "push")
        if unsupported:
            return err(unsupported)

        try:
            pushed = adapter.push(branch)
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        ctx.store.log("vcs", agent=ctx.agent.name, action="push", branch=pushed)
        return ok(f"Pushed '{pushed}' to the remote.", branch=pushed)

    @tool(
        "open_pr",
        "Open a pull request from a branch, as a draft. Requires may_open_pr in your "
        "policy. The body always gains a line saying an automated QA agent opened it "
        "and for which ticket. Merging is a human decision; there is no merge tool.",
        {
            "type": "object",
            "required": ["title", "body", "ticket"],
            "properties": {
                "title": {"type": "string", "description": "What the change does, in one line."},
                "body": {"type": "string", "description": "Why the change is correct, and how it was verified."},
                "ticket": {"type": "string", "description": "The ticket this answers, e.g. 'PROJ-1284'."},
                "branch": {"type": "string", "description": "Head branch. Defaults to the branch you are on."},
                "base": {"type": "string", "description": "Base branch. Defaults to the repository's default."},
                "draft": {
                    "type": "boolean",
                    "description": "Defaults to true. Leave it true unless a human asked for review.",
                },
            },
        },
    )
    async def open_pr(args: dict[str, Any]) -> dict[str, Any]:
        title = str(args.get("title") or "").strip()
        body = str(args.get("body") or "").strip()
        ticket = str(args.get("ticket") or "").strip()
        if not title or not body or not ticket:
            return err("title, body and ticket are all required.")

        try:
            adapter = vcs()
            branch = str(args.get("branch") or "").strip() or adapter.current_branch()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))

        refusal = _publish_refusal(ctx, branch)
        if refusal:
            return _deny(ctx, "open_pr", refusal)
        unsupported = _remote_refusal(adapter, "open_pr")
        if unsupported:
            return err(unsupported)

        draft = args.get("draft")
        try:
            pr = adapter.open_pr(
                branch,
                title,
                body,
                args.get("base"),
                draft=True if draft is None else bool(draft),
                ticket=ticket,
            )
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        ctx.store.log(
            "vcs",
            agent=ctx.agent.name,
            action="open_pr",
            branch=branch,
            ticket=ticket,
            number=pr.get("number"),
            url=pr.get("url"),
        )
        state = "draft PR" if pr.get("draft") else "PR"
        return ok(f"Opened {state} #{pr.get('number')} for {ticket}: {pr.get('url')}", **pr)

    @tool(
        "pr_diff",
        "Unified diff of an existing pull request. Read-only.",
        {
            "type": "object",
            "required": ["number"],
            "properties": {"number": {"type": "integer", "description": "The PR number."}},
        },
    )
    async def pr_diff(args: dict[str, Any]) -> dict[str, Any]:
        try:
            adapter = vcs()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        unsupported = _remote_refusal(adapter, "pr_diff")
        if unsupported:
            return err(unsupported)
        try:
            patch = adapter.pr_diff(args["number"])
        except (VcsError, NotImplementedError, ValueError, KeyError) as exc:
            return err(str(exc))
        if not patch.strip():
            return ok("No changes.", empty=True)
        truncated = patch[:60_000]
        return ok(truncated, truncated=len(patch) > len(truncated), lines=patch.count("\n"))

    @tool(
        "list_changed_files",
        "Paths that differ between two refs on the remote, e.g. a PR's base and head. "
        "Read-only.",
        {
            "type": "object",
            "required": ["base", "head"],
            "properties": {
                "base": {"type": "string", "description": "e.g. 'main'"},
                "head": {"type": "string", "description": "e.g. 'fix/PROJ-1284'"},
            },
        },
    )
    async def list_changed_files(args: dict[str, Any]) -> dict[str, Any]:
        try:
            adapter = vcs()
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        unsupported = _remote_refusal(adapter, "list_changed_files")
        if unsupported:
            return err(unsupported)
        try:
            files = adapter.list_changed_files(str(args["base"]), str(args["head"]))
        except (VcsError, NotImplementedError, ValueError, KeyError) as exc:
            return err(str(exc))
        return ok(", ".join(files) or "(no files changed)", files=files, count=len(files))

    return [
        current_branch,
        create_branch,
        write_file,
        commit,
        diff,
        list_branches,
        push,
        open_pr,
        pr_diff,
        list_changed_files,
    ]


def build(ctx: ToolContext):
    """Construct the vcs MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="vcs", version="1.0.0", tools=build_tools(ctx))
