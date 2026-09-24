"""M2 verification: the §8.1 write matrix holds against real git, not a mock.

Every test here is the same question asked of a different escape route — a
protected branch, a `..`, a symlink, an agent with no policy at all — and the
answer must always be a logged refusal rather than a written file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from support import CONFIG_SEARCH, PACKAGED_CONFIG

from qaas.adapters.vcs import GitHubVcs, LocalGit, VcsError, build_vcs
from qaas.config import load_config
from qaas.mcp.context import ToolContext, handlers
from qaas.mcp.vcs import build_tools
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[2]


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch checkout on `main` with one commit and an empty sandbox."""
    root = tmp_path / "repo"
    (root / "qa" / "repro").mkdir(parents=True)
    (root / "src").mkdir()
    git(root.parent, "init", "-b", "main", str(root))
    git(root, "config", "user.email", "qa@test.local")
    git(root, "config", "user.name", "QA Test")
    (root / "README.md").write_text("scratch\n")
    (root / "src" / "app.py").write_text("VALUE = 1\n")
    (root / "qa" / "repro" / ".gitkeep").write_text("")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


def make_ctx(agent_name: str, repo: Path, tmp_path: Path) -> ToolContext:
    config = load_config(search=CONFIG_SEARCH)
    root = tmp_path / ".qaas"
    return ToolContext(
        store=RunStore(f"vcs-{agent_name.lower()}", root=root),
        maps=SystemMapStore(root),
        config=config,
        agent=config.agents[agent_name],
        target_root=repo,
    )


@pytest.fixture
def reproducer(repo: Path, tmp_path: Path) -> ToolContext:
    """REPRODUCER: write_paths ['qa/repro'], branch_patterns ['qa/repro/*'] (§8.1)."""
    return make_ctx("REPRODUCER", repo, tmp_path)


@pytest.fixture
def api(repo: Path, tmp_path: Path) -> ToolContext:
    """API: a discovery agent, so read-only by policy."""
    return make_ctx("API", repo, tmp_path)


def tools_for(ctx: ToolContext) -> dict:
    return handlers(build_tools(ctx))


def denials(ctx: ToolContext) -> list[dict]:
    return [entry.detail for entry in ctx.store.ledger("denial")]


def denied_tools(ctx: ToolContext) -> list[str]:
    return [detail.get("tool") for detail in denials(ctx)]


# -- the policy is not vacuous ----------------------------------------------


def test_forge_policy_matches_the_architecture(reproducer, api):
    assert reproducer.agent.policy.branch_patterns == ["qa/repro/*"]
    assert reproducer.agent.policy.write_paths == ["qa/repro"]
    assert api.agent.policy.read_only


# -- what REPRODUCER may do ------------------------------------------------------


async def test_forge_can_branch_write_and_commit(reproducer, repo):
    tools = tools_for(reproducer)

    created = await tools["create_branch"]({"name": "qa/repro/x"})
    assert not created.get("isError"), created
    assert created["structuredContent"]["branch"] == "qa/repro/x"

    current = await tools["current_branch"]({})
    assert current["structuredContent"] == {"branch": "qa/repro/x", "writable": True}

    written = await tools["write_file"](
        {"path": "qa/repro/test_case.py", "content": "def test_repro():\n    assert False\n"}
    )
    assert not written.get("isError"), written
    assert (repo / "qa" / "repro" / "test_case.py").exists()

    committed = await tools["commit"]({"message": "repro: failing case"})
    assert not committed.get("isError"), committed
    sha = committed["structuredContent"]["sha"]
    assert len(sha) == 40
    assert "test_case.py" in git(repo, "show", "--name-only", "--format=", sha)
    assert not denials(reproducer)


async def test_diff_and_list_branches_are_readable(reproducer, repo):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/y"})
    (repo / "src" / "app.py").write_text("VALUE = 2\n")

    diff = await tools["diff"]({})
    assert "VALUE = 2" in diff["content"][0]["text"]

    branches = await tools["list_branches"]({})
    assert set(branches["structuredContent"]["branches"]) == {"main", "qa/repro/y"}


# -- what REPRODUCER may not do --------------------------------------------------


async def test_forge_is_refused_main(reproducer):
    tools = tools_for(reproducer)
    result = await tools["create_branch"]({"name": "main"})
    assert result["isError"]
    assert "protected" in result["content"][0]["text"]
    assert denied_tools(reproducer) == ["create_branch"]


async def test_forge_is_refused_a_branch_outside_its_patterns(reproducer):
    tools = tools_for(reproducer)
    result = await tools["create_branch"]({"name": "fix/PROJ-1"})
    assert result["isError"]
    assert "qa/repro/*" in result["content"][0]["text"]
    assert denied_tools(reproducer) == ["create_branch"]


async def test_forge_cannot_commit_on_main(reproducer, repo):
    """Being on a branch it may not write to is enough; no file is even staged."""
    tools = tools_for(reproducer)
    (repo / "qa" / "repro" / "sneaky.py").write_text("x = 1\n")
    result = await tools["commit"]({"message": "on main"})
    assert result["isError"]
    assert "main" in result["content"][0]["text"]
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert denied_tools(reproducer) == ["commit"]


async def test_forge_cannot_write_outside_the_repository(reproducer):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "../../etc/passwd", "content": "pwned"})
    assert result["isError"]
    assert "outside the repository" in result["content"][0]["text"]
    assert denied_tools(reproducer) == ["write_file"]


async def test_forge_cannot_write_outside_its_sandbox(reproducer, repo):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "src/app.py", "content": "VALUE = 99\n"})
    assert result["isError"]
    assert "sandbox" in result["content"][0]["text"]
    assert (repo / "src" / "app.py").read_text() == "VALUE = 1\n"
    assert denied_tools(reproducer) == ["write_file"]


async def test_forge_cannot_write_through_a_symlink_out_of_the_sandbox(reproducer, repo, tmp_path):
    """A path that looks sandboxed but resolves elsewhere is the interesting case:
    the check happens after resolution precisely so this fails."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.py").write_text("original\n")
    (repo / "qa" / "repro" / "escape").symlink_to(outside)

    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "qa/repro/escape/target.py", "content": "pwned"})

    assert result["isError"]
    assert (outside / "target.py").read_text() == "original\n"
    assert denied_tools(reproducer) == ["write_file"]


async def test_forge_cannot_write_an_absolute_path_elsewhere(reproducer, tmp_path):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": str(tmp_path / "elsewhere.py"), "content": "x"})
    assert result["isError"]
    assert not (tmp_path / "elsewhere.py").exists()


async def test_forge_cannot_stage_files_outside_its_sandbox(reproducer, repo):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "qa/repro/x"})
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    result = await tools["commit"]({"message": "sneak in a source change", "paths": ["src/app.py"]})
    assert result["isError"]
    assert denied_tools(reproducer) == ["commit"]


# -- a read-only agent ------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("create_branch", {"name": "qa/repro/x"}),
        ("create_branch", {"name": "api/notes"}),
        ("write_file", {"path": "qa/repro/note.py", "content": "x = 1\n"}),
        ("write_file", {"path": "src/app.py", "content": "x = 1\n"}),
        ("commit", {"message": "anything"}),
    ],
)
async def test_conduit_is_refused_every_write(api, repo, tool_name, args):
    """Empty write_paths and empty branch_patterns means no write path exists."""
    tools = tools_for(api)
    result = await tools[tool_name](args)
    assert result["isError"], result
    assert denied_tools(api) == [tool_name]
    assert not (repo / "qa" / "repro" / "note.py").exists()
    assert git(repo, "branch", "--show-current").strip() == "main"


async def test_conduit_may_still_read(api):
    tools = tools_for(api)
    current = await tools["current_branch"]({})
    assert current["structuredContent"] == {"branch": "main", "writable": False}
    assert not (await tools["list_branches"]({})).get("isError")
    assert not denials(api)


# -- the refusal is auditable -----------------------------------------------


async def test_every_refusal_carries_agent_tool_and_reason(reproducer):
    tools = tools_for(reproducer)
    await tools["create_branch"]({"name": "master"})
    await tools["write_file"]({"path": "/etc/passwd", "content": "pwned"})

    entries = list(reproducer.store.ledger("denial"))
    assert len(entries) == 2
    for entry in entries:
        assert entry.agent == "REPRODUCER"
        assert entry.detail["tool"] in {"create_branch", "write_file"}
        assert entry.detail["reason"]


# -- the adapter itself -----------------------------------------------------


def test_local_git_reports_a_failure_instead_of_swallowing_it(repo):
    adapter = LocalGit(repo)
    assert adapter.current_branch() == "main"
    with pytest.raises(VcsError):
        adapter.create_branch("main")  # already exists
    with pytest.raises(VcsError, match="nothing staged"):
        adapter.commit("empty")


def test_local_git_never_takes_a_command_string(repo):
    """A branch name with shell metacharacters is a bad ref, not a shell escape."""
    adapter = LocalGit(repo)
    with pytest.raises(VcsError):
        adapter.create_branch("qa/repro/x; touch /tmp/qaas-pwned")
    assert not Path("/tmp/qaas-pwned").exists()


def test_the_github_backend_adds_publishing_and_nothing_else(repo):
    """Swapping backends must not widen the §8.1 matrix: GitHub adds a remote,
    not a merge. The enforcement lives in this server either way."""
    adapter = GitHubVcs(repo)
    assert adapter.current_branch() == "main"
    assert not [n for n in dir(adapter) if "merge" in n.lower()]
    assert hasattr(adapter, "open_pr") and hasattr(adapter, "push")


def test_build_vcs_selects_by_config_value(repo):
    assert isinstance(build_vcs("local", repo), LocalGit)
    assert isinstance(build_vcs("github", repo), GitHubVcs)
    with pytest.raises(ValueError):
        build_vcs("perforce", repo)


# -- the doors must agree ---------------------------------------------------


async def test_write_file_honours_the_forbidden_classes_that_edit_honours(repo, tmp_path):
    """`_path_refusal` never checked `forbidden_paths`; `_check_write` always did.

    FIXER may write under `api/app` — so the sandbox check passes — while
    `api/app/auth.py` is a §8.2 forbidden class. The two doors disagreed: `Edit`
    was refused and `mcp__vcs__write_file` wrote the file. Asserting both here
    is the point; either alone would have passed throughout the bug.
    """
    from qaas.guardrails import Guardrail

    (repo / "api" / "app").mkdir(parents=True)
    (repo / "api" / "app" / "auth.py").write_text("SECRET = 1\n")
    ctx = make_ctx("FIXER", repo, tmp_path)
    ctx.agent.policy.branch_patterns = ["*"]

    edit = Guardrail(ctx).check("Edit", {"file_path": "api/app/auth.py"})
    assert not edit.allowed
    assert "autonomy envelope" in edit.reason

    tools = handlers(build_tools(ctx))
    result = await tools["write_file"]({"path": "api/app/auth.py", "content": "pwned"})

    assert result["isError"]
    assert "autonomy envelope" in result["content"][0]["text"]
    assert (repo / "api" / "app" / "auth.py").read_text() == "SECRET = 1\n"


async def test_a_glob_write_path_still_resolves_through_the_shared_check(repo, tmp_path):
    """The vcs server honoured globs in `write_paths` and the guardrail did not.

    Delegating to the guardrail would have silently narrowed this, so the glob
    support moved with it.
    """
    from qaas.guardrails import Guardrail

    ctx = make_ctx("REPRODUCER", repo, tmp_path)
    ctx.agent.policy.write_paths = ["qa/*/generated/*"]
    (repo / "qa" / "repro" / "generated").mkdir(parents=True)

    guard = Guardrail(ctx)
    assert guard.check("Write", {"file_path": "qa/repro/generated/test_x.py"}).allowed
    assert not guard.check("Write", {"file_path": "qa/repro/other/test_x.py"}).allowed


# -- a commit carries this agent's fix, not the sandbox it was standing in ---


def _fixer(repo: Path, tmp_path: Path) -> ToolContext:
    """FIXER, pointed at this scratch repo's own layout."""
    ctx = make_ctx("FIXER", repo, tmp_path)
    ctx.agent.policy.write_paths = ["src", "qa/repro"]
    ctx.agent.policy.scratch_paths = ["qa/repro"]
    ctx.agent.policy.branch_patterns = ["fix/*"]
    return ctx


def test_a_commit_leaves_another_findings_scaffolding_behind(repo: Path, tmp_path: Path):
    """`commit` defaulted to the whole of `write_paths`, and FIXER's holds
    `qa/repro` -- so it swept in whatever scaffolding was sitting there.

    The target tree is shared across findings ("separate contexts but not
    separate sandboxes"), so that was usually *another* finding's probe
    harness. QAAS-53 was committed with five files in it, all five scaffolding
    for finding 4d955330, and REVIEWER escalated it as "there is no fix here to
    review" -- correctly, because there was not.
    """
    ctx = _fixer(repo, tmp_path)
    tools = tools_for(ctx)

    # Someone else's probe harness, left in the shared sandbox.
    (repo / "qa" / "repro" / "4d955330").mkdir(parents=True)
    (repo / "qa" / "repro" / "4d955330" / "probe.test.ts").write_text("// not mine\n")

    # FIXER's actual fix.
    (repo / "src" / "app.py").write_text("VALUE = 2\n")

    import asyncio
    asyncio.run(tools["create_branch"]({"name": "fix/real-work"}))
    result = asyncio.run(tools["commit"]({"message": "Fix the value"}))
    assert not result.get("is_error"), result["content"][0]["text"]

    files = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "src/app.py" in files, files
    assert not any(f.startswith("qa/repro") for f in files), (
        f"another finding's scaffolding rode along: {files}"
    )


def test_naming_the_sandbox_explicitly_still_commits_it(repo: Path, tmp_path: Path):
    """This narrows what "commit everything I may write" means, nothing else."""
    ctx = _fixer(repo, tmp_path)
    tools = tools_for(ctx)
    (repo / "qa" / "repro" / "mine.test.ts").write_text("// deliberate\n")

    import asyncio
    asyncio.run(tools["create_branch"]({"name": "fix/deliberate"}))
    result = asyncio.run(tools["commit"]({"message": "Keep the probe", "paths": ["qa/repro"]}))
    assert not result.get("is_error"), result["content"][0]["text"]
    assert "qa/repro/mine.test.ts" in git(repo, "show", "--name-only", "--format=", "HEAD")


def test_an_agent_whose_sandbox_is_its_work_is_unaffected(repo: Path, tmp_path: Path):
    """REPRODUCER declares no `scratch_paths`, so `qa/repro` is product to it.

    Its committed failing test is the deliverable, and narrowing this for
    everyone would have stopped it committing anything at all.
    """
    ctx = make_ctx("REPRODUCER", repo, tmp_path)
    tools = tools_for(ctx)
    assert ctx.agent.policy.scratch_paths == []
    (repo / "qa" / "repro" / "failing.test.ts").write_text("// the deliverable\n")

    import asyncio
    asyncio.run(tools["create_branch"]({"name": "qa/repro/abc-thing"}))
    result = asyncio.run(tools["commit"]({"message": "Pin the defect"}))
    assert not result.get("is_error"), result["content"][0]["text"]
    assert "qa/repro/failing.test.ts" in git(repo, "show", "--name-only", "--format=", "HEAD")


# -- what a commit contains, and where a branch starts ---------------------------


def _fixer_ctx(repo: Path, tmp_path: Path, **policy) -> ToolContext:
    ctx = make_ctx("FIXER", repo, tmp_path)
    spec = ctx.agent.model_copy(deep=True)
    spec.policy.write_paths = ["src", "qa/repro"]
    spec.policy.scratch_paths = ["qa/repro"]
    spec.policy.forbidden_paths = ["*auth*"]
    for field, value in policy.items():
        setattr(spec.policy, field, value)
    ctx.agent = spec
    ctx.scope = "CORVID-1"
    return ctx


async def test_a_commit_takes_only_its_own_paths_not_the_whole_index(repo, tmp_path):
    """`git commit` commits the index. A `secret.env` the operator staged before
    the run went into REPRODUCER's commit, and from there onto a pushed branch."""
    (repo / "secret.env").write_text("TOKEN=x\n")
    git(repo, "add", "secret.env")
    tools = tools_for(make_ctx("REPRODUCER", repo, tmp_path))
    await tools["create_branch"]({"name": "qa/repro/one"})
    (repo / "qa" / "repro" / "test_one.py").write_text("def test_x(): assert False\n")
    result = await tools["commit"]({"message": "repro"})
    assert not result.get("is_error"), result
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["qa/repro/test_one.py"]
    assert git(repo, "diff", "--cached", "--name-only").split() == ["secret.env"]


async def test_a_file_changed_behind_the_guardrail_is_checked_at_commit(repo, tmp_path):
    """The pathspec `src` passed `_check_path`; the `src/auth.py` it staged never did."""
    ctx = _fixer_ctx(repo, tmp_path)
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "fix/CORVID-1"})
    (repo / "src" / "auth.py").write_text("ALLOW_ALL = True\n")  # e.g. via a script in Bash
    result = await tools["commit"]({"message": "fix"})
    assert result.get("is_error")
    assert "autonomy envelope" in result["content"][0]["text"]
    assert git(repo, "diff", "--cached", "--name-only").strip() == ""


async def test_the_file_budget_counts_what_a_commit_stages(repo, tmp_path):
    ctx = _fixer_ctx(repo, tmp_path, max_diff_files=2)
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "fix/CORVID-1"})
    for i in range(4):
        (repo / "src" / f"m{i}.py").write_text("x = 1\n")
    result = await tools["commit"]({"message": "fix"})
    assert result.get("is_error")
    assert "(§8.2)" in result["content"][0]["text"]


async def test_the_line_budget_is_enforced_at_commit(repo, tmp_path):
    """`max_diff_lines` was told to FIXER as enforced, and nothing enforced it."""
    ctx = _fixer_ctx(repo, tmp_path, max_diff_lines=10)
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "fix/CORVID-1"})
    (repo / "src" / "app.py").write_text("".join(f"V{i} = {i}\n" for i in range(30)))
    result = await tools["commit"]({"message": "too wide"})
    assert result.get("is_error")
    assert "(§8.2)" in result["content"][0]["text"]
    assert any("(§8.2)" in d.get("reason", "") for d in denials(ctx))


async def test_scratch_lines_do_not_count_and_a_small_fix_commits(repo, tmp_path):
    ctx = _fixer_ctx(repo, tmp_path, max_diff_lines=10)
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "fix/CORVID-1"})
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    (repo / "qa" / "repro" / "probe.py").write_text("".join(f"# {i}\n" for i in range(200)))
    result = await tools["commit"]({"message": "fix", "paths": ["src", "qa/repro"]})
    assert not result.get("is_error"), result
    assert ctx.store.committed_lines("FIXER", "CORVID-1") == 2


async def test_a_new_branch_starts_from_the_base_not_from_what_is_checked_out(repo, tmp_path):
    """The tree is shared across findings, so "the current HEAD" was wherever the
    previous agent had left it -- and every later PR carried its commits."""
    ctx = make_ctx("REPRODUCER", repo, tmp_path)
    ctx.base_ref = "main"
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "qa/repro/first"})
    (repo / "qa" / "repro" / "test_first.py").write_text("x = 1\n")
    await tools["commit"]({"message": "first"})
    await tools["create_branch"]({"name": "qa/repro/second"})
    files = git(repo, "diff", "--name-only", "main", "HEAD").split()
    assert "qa/repro/test_first.py" not in files


async def test_a_security_fix_is_committed_but_never_published(repo, tmp_path):
    """The tracker keeps a security finding out of a readable backlog; nothing kept
    its fix off a public remote as a branch and a PR describing the hole."""
    from qaas.envelope import DefectEnvelope

    ctx = _fixer_ctx(repo, tmp_path)
    ctx.agent.policy.may_open_pr = True
    ctx.store.put_envelope(DefectEnvelope(
        run_id=ctx.store.run_id, discovered_by="AUDITOR", domain="security",
        **{"class": "vulnerability"}, title="IDOR on orders", summary="s",
        severity="critical", confidence=0.9, jira={"key": "CORVID-1"},
    ))
    tools = tools_for(ctx)
    await tools["create_branch"]({"name": "fix/CORVID-1"})
    for name, args in (("push", {}), ("open_pr", {"title": "t", "body": "b", "ticket": "CORVID-1"})):
        result = await tools[name](args)
        assert result.get("is_error"), name
        assert "security" in result["content"][0]["text"].lower(), name


def test_open_pr_leaves_the_base_to_the_repository_when_none_is_named(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_BRANCH", raising=False)
    assert GitHubVcs.default_base(object.__new__(GitHubVcs)) is None


async def test_the_reproduction_branch_is_read_from_git_not_from_the_agent(repo, tmp_path):
    """A real REPRODUCER wrote `"master @ d8add44 (spin_up refused 'main' ...)"`
    into `environment.branch`; VERIFIER was sent to verify on that sentence and
    FIXER branched from master without the failing test."""
    from qaas.envelope import DefectEnvelope
    from qaas.mcp.envelope_server import build_tools as envelope_tools

    ctx = make_ctx("REPRODUCER", repo, tmp_path)
    git(repo, "checkout", "-q", "-b", "qa/repro/abc12345-limit")
    envelope = DefectEnvelope(
        run_id=ctx.store.run_id, discovered_by="API", domain="api", **{"class": "bug"},
        title="limit ignored", summary="s", severity="major", confidence=0.9,
    )
    ctx.store.put_envelope(envelope)
    tools = handlers(envelope_tools(ctx))
    result = await tools["record_reproduction"]({
        "envelope_id": envelope.id, "status": "reproduced", "confidence": 0.95,
        "environment": {"branch": "master @ d8add44 (spin_up refused 'main')"},
    })
    assert not result.get("is_error"), result
    stored = ctx.store.get_envelope(envelope.id)
    assert stored.reproduction.environment.branch == "qa/repro/abc12345-limit"


async def test_a_committed_diff_ignores_what_is_uncommitted_in_the_checkout(repo, tmp_path):
    """A real REVIEWER blocked a correct fix for "editing the golden ledger" -- the
    operator's own uncommitted edit, shown by a working-tree diff, in none of
    FIXER's commits. `head` diffs committed history only."""
    git(repo, "checkout", "-q", "-b", "fix/QAAS-1")
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-qam", "fix")
    (repo / "README.md").write_text("an operator's uncommitted edit\n")
    tools = tools_for(make_ctx("REVIEWER", repo, tmp_path))
    working = await tools["diff"]({"ref": "main"})
    committed = await tools["diff"]({"ref": "main", "head": "fix/QAAS-1"})
    assert "README.md" in working["content"][0]["text"]
    text = committed["content"][0]["text"]
    assert "src/app.py" in text and "README.md" not in text
