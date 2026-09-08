"""M2 verification: the §8.1 write matrix holds against real git, not a mock.

Every test here is the same question asked of a different escape route — a
protected branch, a `..`, a symlink, an agent with no policy at all — and the
answer must always be a logged refusal rather than a written file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    config = load_config(REPO / "config")
    root = tmp_path / ".qaas"
    return ToolContext(
        store=RunStore(f"vcs-{agent_name.lower()}", root=root),
        maps=SystemMapStore(root),
        config=config,
        agent=config.agents[agent_name],
        repo_root=repo,
    )


@pytest.fixture
def forge(repo: Path, tmp_path: Path) -> ToolContext:
    """FORGE: write_paths ['qa/repro'], branch_patterns ['qa/repro/*'] (§8.1)."""
    return make_ctx("FORGE", repo, tmp_path)


@pytest.fixture
def conduit(repo: Path, tmp_path: Path) -> ToolContext:
    """CONDUIT: a discovery agent, so read-only by policy."""
    return make_ctx("CONDUIT", repo, tmp_path)


def tools_for(ctx: ToolContext) -> dict:
    return handlers(build_tools(ctx))


def denials(ctx: ToolContext) -> list[dict]:
    return [entry.detail for entry in ctx.store.ledger("denial")]


def denied_tools(ctx: ToolContext) -> list[str]:
    return [detail.get("tool") for detail in denials(ctx)]


# -- the policy is not vacuous ----------------------------------------------


def test_forge_policy_matches_the_architecture(forge, conduit):
    assert forge.agent.policy.branch_patterns == ["qa/repro/*"]
    assert forge.agent.policy.write_paths == ["qa/repro"]
    assert conduit.agent.policy.read_only


# -- what FORGE may do ------------------------------------------------------


async def test_forge_can_branch_write_and_commit(forge, repo):
    tools = tools_for(forge)

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
    assert not denials(forge)


async def test_diff_and_list_branches_are_readable(forge, repo):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/y"})
    (repo / "src" / "app.py").write_text("VALUE = 2\n")

    diff = await tools["diff"]({})
    assert "VALUE = 2" in diff["content"][0]["text"]

    branches = await tools["list_branches"]({})
    assert set(branches["structuredContent"]["branches"]) == {"main", "qa/repro/y"}


# -- what FORGE may not do --------------------------------------------------


async def test_forge_is_refused_main(forge):
    tools = tools_for(forge)
    result = await tools["create_branch"]({"name": "main"})
    assert result["isError"]
    assert "protected" in result["content"][0]["text"]
    assert denied_tools(forge) == ["create_branch"]


async def test_forge_is_refused_a_branch_outside_its_patterns(forge):
    tools = tools_for(forge)
    result = await tools["create_branch"]({"name": "fix/PROJ-1"})
    assert result["isError"]
    assert "qa/repro/*" in result["content"][0]["text"]
    assert denied_tools(forge) == ["create_branch"]


async def test_forge_cannot_commit_on_main(forge, repo):
    """Being on a branch it may not write to is enough; no file is even staged."""
    tools = tools_for(forge)
    (repo / "qa" / "repro" / "sneaky.py").write_text("x = 1\n")
    result = await tools["commit"]({"message": "on main"})
    assert result["isError"]
    assert "main" in result["content"][0]["text"]
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert denied_tools(forge) == ["commit"]


async def test_forge_cannot_write_outside_the_repository(forge):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "../../etc/passwd", "content": "pwned"})
    assert result["isError"]
    assert "outside the repository" in result["content"][0]["text"]
    assert denied_tools(forge) == ["write_file"]


async def test_forge_cannot_write_outside_its_sandbox(forge, repo):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "src/app.py", "content": "VALUE = 99\n"})
    assert result["isError"]
    assert "sandbox" in result["content"][0]["text"]
    assert (repo / "src" / "app.py").read_text() == "VALUE = 1\n"
    assert denied_tools(forge) == ["write_file"]


async def test_forge_cannot_write_through_a_symlink_out_of_the_sandbox(forge, repo, tmp_path):
    """A path that looks sandboxed but resolves elsewhere is the interesting case:
    the check happens after resolution precisely so this fails."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.py").write_text("original\n")
    (repo / "qa" / "repro" / "escape").symlink_to(outside)

    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": "qa/repro/escape/target.py", "content": "pwned"})

    assert result["isError"]
    assert (outside / "target.py").read_text() == "original\n"
    assert denied_tools(forge) == ["write_file"]


async def test_forge_cannot_write_an_absolute_path_elsewhere(forge, tmp_path):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/x"})
    result = await tools["write_file"]({"path": str(tmp_path / "elsewhere.py"), "content": "x"})
    assert result["isError"]
    assert not (tmp_path / "elsewhere.py").exists()


async def test_forge_cannot_stage_files_outside_its_sandbox(forge, repo):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "qa/repro/x"})
    (repo / "src" / "app.py").write_text("VALUE = 2\n")
    result = await tools["commit"]({"message": "sneak in a source change", "paths": ["src/app.py"]})
    assert result["isError"]
    assert denied_tools(forge) == ["commit"]


# -- a read-only agent ------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("create_branch", {"name": "qa/repro/x"}),
        ("create_branch", {"name": "conduit/notes"}),
        ("write_file", {"path": "qa/repro/note.py", "content": "x = 1\n"}),
        ("write_file", {"path": "src/app.py", "content": "x = 1\n"}),
        ("commit", {"message": "anything"}),
    ],
)
async def test_conduit_is_refused_every_write(conduit, repo, tool_name, args):
    """Empty write_paths and empty branch_patterns means no write path exists."""
    tools = tools_for(conduit)
    result = await tools[tool_name](args)
    assert result["isError"], result
    assert denied_tools(conduit) == [tool_name]
    assert not (repo / "qa" / "repro" / "note.py").exists()
    assert git(repo, "branch", "--show-current").strip() == "main"


async def test_conduit_may_still_read(conduit):
    tools = tools_for(conduit)
    current = await tools["current_branch"]({})
    assert current["structuredContent"] == {"branch": "main", "writable": False}
    assert not (await tools["list_branches"]({})).get("isError")
    assert not denials(conduit)


# -- the refusal is auditable -----------------------------------------------


async def test_every_refusal_carries_agent_tool_and_reason(forge):
    tools = tools_for(forge)
    await tools["create_branch"]({"name": "master"})
    await tools["write_file"]({"path": "/etc/passwd", "content": "pwned"})

    entries = list(forge.store.ledger("denial"))
    assert len(entries) == 2
    for entry in entries:
        assert entry.agent == "FORGE"
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
