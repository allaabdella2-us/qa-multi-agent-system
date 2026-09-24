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

import asyncio
import fnmatch
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas.adapters.vcs import VcsAdapter, VcsError, build_vcs
from qaas.envelope import DefectClass, DefectEnvelope, Domain
from qaas.guardrails import DIFF_BUDGET_REFUSAL, Guardrail
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


def _is_security(envelope: DefectEnvelope) -> bool:
    """The same test the tracker routes on: a security domain, a vulnerability,
    or an impact the discovering agent marked security-relevant."""
    return (
        envelope.domain == Domain.SECURITY
        or envelope.defect_class == DefectClass.VULNERABILITY
        or bool(envelope.impact.security_relevant)
    )


def _security_refusal(ctx: ToolContext, ticket: str | None) -> str | None:
    """Why a fix for this ticket may not be published, or None.

    The tracker refuses to put a security finding in a readable backlog, and
    routes it to a restricted project -- and nothing stopped the fix for that
    same finding going out as a pushed branch and a pull request whose title
    and body describe the vulnerability, on a remote that may be public.
    `--from-board` picks restricted tickets up like any other. §8.4 makes any
    security ticket a mandatory human touch; publishing is where that lands.
    The fix itself is not refused: it stays on the local branch for a human.
    """
    key = (ticket or ctx.scope or "").strip().upper()
    if not key:
        return None
    for envelope in ctx.store.envelopes():
        if (envelope.jira.key or "").upper() == key and _is_security(envelope):
            return (
                f"{key} is a security finding. Security fixes stop at a human (§8.4): "
                "a pushed branch or a pull request describes the vulnerability to "
                "everyone who can read the remote. Commit the fix locally and say in "
                "your summary which branch holds it; a human publishes it."
            )
    return None


def _remote_refusal(adapter: VcsAdapter, capability: str) -> str | None:
    """The local backend has no remote; say so rather than raise AttributeError."""
    if not hasattr(adapter, capability):
        return (
            f"the configured vcs backend ({type(adapter).__name__}) has no remote, so "
            f"'{capability}' does not exist. Set `vcs: github` in config/system.yaml "
            "to work against a GitHub repository."
        )
    return None


def _path_refusal(
    ctx: ToolContext, raw: str, *, count_against_budget: bool = True
) -> tuple[Path | None, str | None]:
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

    decision = Guardrail(ctx)._check_path(raw, count_against_budget=count_against_budget)
    if not decision.allowed:
        return None, decision.reason
    return resolved, None


def _product_files(ctx: ToolContext, files: list[str]) -> list[str]:
    """The files that are the change, not the probe harness beside it (§8.2)."""
    scratch = ctx.agent.policy.scratch_paths
    return [
        f for f in files
        if not any(_under_prefix(f, prefix) for prefix in scratch)
    ]


def _under_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.strip("/")
    return path == prefix or path.startswith(prefix + "/")


def _line_budget_refusal(ctx: ToolContext, adapter: VcsAdapter, files: list[str]) -> str | None:
    """Why this commit would take the agent past `max_diff_lines`, or None.

    `max_diff_lines` was configured, overridable per target, shown on the
    dashboard and told to FIXER and REVIEWER as "the number the guardrail
    enforces" -- and nothing enforced it. The commit is where a line count is
    both knowable and final, and FIXER must commit for its round to count at
    all. Scratch files are exempt exactly as they are from the file budget.
    """
    limit = ctx.agent.policy.max_diff_lines
    if limit is None:
        return None
    product = _product_files(ctx, files)
    try:
        lines = adapter.staged_line_count(product) if product else 0
    except (VcsError, ValueError):
        return None
    already = ctx.store.committed_lines(ctx.agent.name, ctx.scope)
    if already + lines <= limit:
        return None
    return (
        f"{ctx.agent.name} would have committed {already + lines} changed lines, which is "
        f"past its limit of {limit} {DIFF_BUDGET_REFUSAL}. A fix this wide is outside the "
        "autonomy envelope: stop, and escalate with what you have found."
    )


def _charge_lines(ctx: ToolContext, adapter: VcsAdapter, files: list[str]) -> None:
    product = _product_files(ctx, files)
    if ctx.agent.policy.max_diff_lines is None or not product:
        return
    try:
        # Measured against the commit just made rather than re-read from the
        # index, which `commit_files` has now emptied of these paths.
        lines = adapter.committed_line_count(product)
    except (VcsError, ValueError, AttributeError):
        return
    ctx.store.add_committed_lines(ctx.agent.name, ctx.scope, lines)


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
                "from_ref": {
                    "type": "string",
                    "description": (
                        "Base ref. Defaults to the ticket's reproduction branch when you are "
                        "fixing one (so the failing test is on your branch), otherwise to the "
                        "commit this run started on -- never to whatever happens to be checked out."
                    ),
                },
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
        # The base is decided by the router, not by what is checked out. The
        # working tree is shared across findings, so "the current HEAD" was
        # wherever the previous agent had left it: REPRODUCER's second branch
        # grew out of its first, and each FIXER branch after the first carried
        # other findings' commits into its pull request.
        base = str(args.get("from_ref") or "").strip() or ctx.base_ref
        try:
            branch = vcs().create_branch(name, base or None)
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        ctx.store.log("vcs", agent=ctx.agent.name, action="create_branch", branch=branch, base=base)
        return ok(f"Created and switched to '{branch}' from {base or 'the current HEAD'}.",
                  branch=branch, base=base)

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

        # A commit with no explicit paths stages the agent's *product* write
        # paths, never its scratch area.
        #
        # It used to default to the whole of `write_paths`, and FIXER's includes
        # `qa/repro` -- so every commit swept in whatever scaffolding happened
        # to be sitting there. The target tree is shared across findings ("two
        # invocations are separate contexts but not separate sandboxes"), so
        # that was usually *another* finding's probe harness. QAAS-53 was
        # committed with five files in it, all five of them scaffolding for
        # finding 4d955330, and REVIEWER escalated it as "there is no fix here
        # to review" -- correctly, because there was not.
        #
        # An agent that genuinely wants to commit scratch still can, by naming
        # the path; this only changes what "commit everything I may write"
        # means. REPRODUCER declares no `scratch_paths`, so its own sandbox is
        # product to it and its commits are unaffected.
        scratch = ctx.agent.policy.scratch_paths
        default = [p for p in ctx.agent.policy.write_paths if p not in scratch]
        requested = args.get("paths") or default or ctx.agent.policy.write_paths
        staged: list[str] = []
        for raw in requested:
            # Read-only: staging a path is not changing it. The §8.2 diff budget
            # counts *files an agent has written*, and `commit` is handed the
            # agent's own `write_paths` as pathspecs -- so committing `api/app`
            # and `web/src` charged two of FIXER's five before a line had
            # changed. The files themselves were already counted when they were
            # written.
            resolved, path_refusal = _path_refusal(ctx, raw, count_against_budget=False)
            if path_refusal:
                return _deny(ctx, "commit", path_refusal)
            assert resolved is not None
            staged.append(resolved.relative_to(ctx.target_root.resolve()).as_posix())

        try:
            adapter = vcs()
            files = await asyncio.to_thread(adapter.stage, staged)
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        if not files:
            return err("nothing staged to commit: no file under those paths has changed.")

        # The matrix is applied to the *files*, not only to the pathspecs. A
        # pathspec of `api/app` passes `_check_path`, and staging it pulled in
        # whatever was under it -- `api/app/auth.py` changed some other way (a
        # script run through Bash, a code generator), eight files against a
        # budget of five -- none of which was ever checked. Counted against the
        # budget here, which is a no-op for files `Write`/`Edit` already charged.
        for rel in files:
            _, file_refusal = _path_refusal(ctx, rel, count_against_budget=True)
            if file_refusal:
                adapter.unstage(files)
                return _deny(ctx, "commit", f"refusing to commit {rel}: {file_refusal}")

        line_refusal = _line_budget_refusal(ctx, adapter, files)
        if line_refusal:
            adapter.unstage(files)
            return _deny(ctx, "commit", line_refusal)

        try:
            sha = await asyncio.to_thread(adapter.commit_files, message, files)
        except (VcsError, NotImplementedError, ValueError) as exc:
            return err(str(exc))
        _charge_lines(ctx, adapter, files)
        ctx.store.log("vcs", agent=ctx.agent.name, action="commit", branch=branch, sha=sha)
        return ok(
            f"Committed {sha[:10]} on {branch}: {len(files)} file(s).",
            sha=sha, branch=branch, paths=staged, files=files,
        )

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
            patch = await asyncio.to_thread(vcs().diff, args.get("ref"), args.get("paths"))
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

        refusal = _publish_refusal(ctx, branch) or _security_refusal(ctx, None)
        if refusal:
            return _deny(ctx, "push", refusal)
        unsupported = _remote_refusal(adapter, "push")
        if unsupported:
            return err(unsupported)

        try:
            pushed = await asyncio.to_thread(adapter.push, branch)
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

        refusal = _publish_refusal(ctx, branch) or _security_refusal(ctx, ticket)
        if refusal:
            return _deny(ctx, "open_pr", refusal)
        unsupported = _remote_refusal(adapter, "open_pr")
        if unsupported:
            return err(unsupported)

        draft = args.get("draft")
        try:
            # Off the event loop, like every network call here: `gh pr create`
            # and `git push` can take up to two minutes, and while they ran
            # synchronously inside this async handler nothing else in the
            # process moved -- not the router's clock, not a sibling agent.
            pr = await asyncio.to_thread(
                adapter.open_pr,
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
            patch = await asyncio.to_thread(adapter.pr_diff, args["number"])
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
            files = await asyncio.to_thread(
                adapter.list_changed_files, str(args["base"]), str(args["head"])
            )
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
