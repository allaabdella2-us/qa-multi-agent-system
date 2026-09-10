"""The GitHub backend, verified without touching GitHub.

The interesting surface of this adapter is not what `gh` returns — it is the
command line the adapter builds. Every test here therefore points the adapter at
a fake `gh` on PATH that records its own argv and echoes canned output, so the
assertions are about the exact arguments an agent's request turns into: which
branch is the head, that `--draft` is there, that no `--force` ever is.

Anything that needs the network or a real repository is marked `github` and
excluded from the default run (see pyproject `addopts`).
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import CONFIG_SEARCH, PACKAGED_CONFIG

from qaas.adapters.vcs import (
    AUTOMATION_NOTICE,
    GitHubVcs,
    LocalGit,
    VcsError,
    build_vcs,
    is_protected_head,
)
from qaas.config import AgentSpec, load_config
from qaas.mcp.context import ToolContext, handlers
from qaas.mcp.vcs import build_tools
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[2]

PR_URL = "https://github.com/acme/widgets/pull/42"

# A stand-in for the real binary: records every invocation as one JSON line and
# answers the handful of subcommands the adapter knows how to call.
FAKE_GH = '''\
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["QAAS_FAKE_GH_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\\n")

if argv[:2] == ["auth", "status"]:
    if os.environ.get("QAAS_FAKE_GH_UNAUTH"):
        sys.stderr.write("You are not logged into any GitHub hosts. "
                         "To log in, run: gh auth login\\n")
        sys.exit(1)
    sys.exit(0)

if argv[:2] == ["pr", "create"]:
    if os.environ.get("QAAS_FAKE_GH_PR_FAILS"):
        sys.stderr.write("pull request create failed: No commits between main and x\\n")
        sys.exit(1)
    print("Creating pull request for x into main in acme/widgets")
    print(%(pr_url)r)
    sys.exit(0)

if argv[:2] == ["pr", "diff"]:
    print("--- a/api/app/routes/orders.py\\n+++ b/api/app/routes/orders.py")
    sys.exit(0)

if argv[:1] == ["api"]:
    print("api/app/routes/orders.py")
    print("tests/test_orders.py")
    sys.exit(0)

if argv[:2] == ["repo", "clone"]:
    os.makedirs(argv[3], exist_ok=True)
    sys.exit(0)

sys.stderr.write("fake gh: unhandled " + " ".join(argv) + "\\n")
sys.exit(1)
''' % {"pr_url": PR_URL}


class FakeGh:
    """The fake binary plus the log of what was run through it."""

    def __init__(self, path: Path, log: Path):
        self.path = path
        self.log = log

    @property
    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def calls_to(self, *prefix: str) -> list[list[str]]:
        """Every invocation whose subcommand matches, e.g. `calls_to("pr", "create")`."""
        return [call for call in self.calls if call[: len(prefix)] == list(prefix)]


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """A `gh` on PATH that records argv instead of talking to GitHub."""
    script = tmp_path / "bin" / "gh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!{sys.executable}\n{FAKE_GH}")
    script.chmod(0o755)

    log = tmp_path / "gh-calls.jsonl"
    monkeypatch.setenv("QAAS_FAKE_GH_LOG", str(log))
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_DEFAULT_BRANCH", raising=False)
    return FakeGh(script, log)


def flag_value(argv: list[str], name: str) -> str:
    """The value of `--name=value`, which is how every gh flag is passed.

    Joined rather than positional so a title or body beginning with a dash can
    never be re-read as a flag; the tests assert the value, not the shape.
    """
    prefix = f"{name}="
    matches = [a[len(prefix):] for a in argv if a.startswith(prefix)]
    assert matches, f"{name} not in {argv}"
    return matches[0]


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A checkout on `fix/PROJ-1284` with a bare `origin` to push into.

    Real git, no network: the remote is a bare repo two directories over, which
    is enough to prove `push` publishes and enough to prove it never forces.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

    root = tmp_path / "clone"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "qa@test.local")
    git(root, "config", "user.name", "QA Test")
    git(root, "remote", "add", "origin", str(origin))
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    git(root, "push", "origin", "main")
    git(root, "checkout", "-b", "fix/PROJ-1284")
    return root


# -- the command line is the contract ---------------------------------------


def test_open_pr_builds_a_draft_pr_create(fake_gh, clone):
    adapter = GitHubVcs(clone)
    pr = adapter.open_pr(
        "fix/PROJ-1284",
        "Refund endpoint checks the caller's role",
        "The route never checked role. Adds the check plus a regression test.",
        "main",
        ticket="PROJ-1284",
    )

    (argv,) = fake_gh.calls_to("pr", "create")
    assert argv[:2] == ["pr", "create"]
    assert flag_value(argv, "--head") == "fix/PROJ-1284"
    assert flag_value(argv, "--base") == "main"
    assert flag_value(argv, "--title") == "Refund endpoint checks the caller's role"
    assert "--draft" in argv, "a PR nobody asked for must not request review"
    assert "--repo" not in argv, "with no slug configured, gh infers the repo from the checkout"
    assert not any(a.startswith("--force") or a == "-f" for a in argv)

    assert pr["number"] == 42
    assert pr["url"] == PR_URL
    assert pr["draft"] is True


def test_open_pr_can_be_told_not_to_be_a_draft(fake_gh, clone):
    GitHubVcs(clone).open_pr("fix/x", "t", "b", "main", draft=False, ticket="PROJ-1")
    (argv,) = fake_gh.calls_to("pr", "create")
    assert "--draft" not in argv


def test_open_pr_body_ends_with_the_automated_origin_line(fake_gh, clone):
    GitHubVcs(clone).open_pr("fix/x", "t", "Why this fix is right.", "main", ticket="PROJ-1284")
    (argv,) = fake_gh.calls_to("pr", "create")
    body = flag_value(argv, "--body")

    assert body.startswith("Why this fix is right.")
    assert body.rstrip().endswith(AUTOMATION_NOTICE.format(ticket="PROJ-1284"))
    assert "automated QA agent" in body
    assert "PROJ-1284" in body


def test_open_pr_refuses_to_open_one_that_names_no_ticket(fake_gh, clone):
    with pytest.raises(VcsError, match="ticket"):
        GitHubVcs(clone).open_pr("fix/x", "t", "b", "main")
    assert fake_gh.calls_to("pr", "create") == []


def test_open_pr_uses_the_configured_slug_when_there_is_one(fake_gh, clone):
    GitHubVcs(clone, repo_slug="acme/widgets").open_pr("fix/x", "t", "b", "main", ticket="P-1")
    (argv,) = fake_gh.calls_to("pr", "create")
    assert argv[2:4] == ["--repo", "acme/widgets"]


def test_pr_diff_and_list_changed_files_read_a_real_pr(fake_gh, clone):
    adapter = GitHubVcs(clone, repo_slug="acme/widgets")

    assert "orders.py" in adapter.pr_diff(42)
    assert fake_gh.calls_to("pr", "diff") == [["pr", "diff", "--repo", "acme/widgets", "42"]]

    files = adapter.list_changed_files("main", "fix/PROJ-1284")
    assert files == ["api/app/routes/orders.py", "tests/test_orders.py"]
    assert fake_gh.calls_to("api") == [
        [
            "api",
            "repos/acme/widgets/compare/main...fix/PROJ-1284",
            "--jq",
            ".files[].filename",
        ]
    ]


def test_list_changed_files_lets_gh_infer_the_repo_when_unconfigured(fake_gh, clone):
    GitHubVcs(clone).list_changed_files("main", "fix/x")
    (argv,) = fake_gh.calls_to("api")
    assert argv[1] == "repos/{owner}/{repo}/compare/main...fix/x"


def test_clone_is_shallow_and_returns_an_adapter(fake_gh, tmp_path):
    dest = tmp_path / "work" / "widgets"
    dest.parent.mkdir()

    adapter = GitHubVcs.clone("https://github.com/acme/widgets", dest)

    (argv,) = fake_gh.calls_to("repo", "clone")
    assert argv == ["repo", "clone", "https://github.com/acme/widgets", str(dest), "--", "--depth=1"]
    assert isinstance(adapter, GitHubVcs)
    assert adapter.repo == dest.resolve()


# -- push works, and only where it is allowed -------------------------------


def test_push_publishes_the_branch_without_forcing(fake_gh, clone, tmp_path):
    pushed = GitHubVcs(clone).push()
    assert pushed == "fix/PROJ-1284"
    remote_branches = git(tmp_path / "origin.git", "branch", "--list")
    assert "fix/PROJ-1284" in remote_branches


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "develop", "release/2.1", "MAIN"])
def test_push_refuses_a_protected_head(fake_gh, clone, branch):
    with pytest.raises(VcsError, match="protected"):
        GitHubVcs(clone).push(branch)


@pytest.mark.parametrize("branch", ["main", "master", "trunk", "develop", "release-2.1"])
def test_open_pr_refuses_a_protected_head(fake_gh, clone, branch):
    with pytest.raises(VcsError, match="protected"):
        GitHubVcs(clone).open_pr(branch, "t", "b", "main", ticket="P-1")
    assert fake_gh.calls_to("pr", "create") == []


def test_a_ref_may_not_smuggle_in_an_option(fake_gh, clone):
    """Everything is argv, so the only trick left is a value read as a flag."""
    with pytest.raises(VcsError, match="may not start with"):
        GitHubVcs(clone).open_pr("--upload-pack=touch /tmp/pwned", "t", "b", "main", ticket="P-1")
    with pytest.raises(VcsError, match="may not start with"):
        GitHubVcs(clone).list_changed_files("main", "--jq=.")


@pytest.mark.parametrize(
    "branch",
    [
        "fix/PROJ-1284:main",
        "fix/PROJ-1284:refs/heads/main",
        "+fix/PROJ-1284:main",
        "fix/PROJ-1284:master",
    ],
)
def test_push_refuses_a_branch_that_is_really_a_refspec(fake_gh, clone, tmp_path, branch):
    """`git push origin <x>` parses <x> as a refspec, not as a name.

    Every gate on this path saw a string that was neither `main` nor outside the
    agent's patterns — `fnmatch("fix/PROJ-1284:main", "fix/*")` is True and
    `is_protected_head` tested the whole string — while git pushed the local
    branch onto the remote's main. The assertion that matters is not the
    exception: it is that origin's main is still where it was.
    """
    origin = tmp_path / "origin.git"
    before = git(origin, "rev-parse", "refs/heads/main").strip()

    with pytest.raises(VcsError, match="refspec|forced update"):
        GitHubVcs(clone).push(branch)

    assert git(origin, "rev-parse", "refs/heads/main").strip() == before


def test_push_states_its_destination_rather_than_letting_git_infer_one(fake_gh, clone, tmp_path):
    """The refspec is fully qualified on both sides, and only the head moves."""
    origin = tmp_path / "origin.git"
    main_before = git(origin, "rev-parse", "refs/heads/main").strip()

    assert GitHubVcs(clone).push() == "fix/PROJ-1284"

    assert git(origin, "rev-parse", "refs/heads/main").strip() == main_before
    assert "fix/PROJ-1284" in git(origin, "branch", "--list")


def test_a_diff_ref_may_not_smuggle_in_an_option(clone, tmp_path):
    """`git diff --output=<path>` exits 0 and writes the file it names.

    `pr_diff` and `list_changed_files` already refused a flag-like ref; `diff`,
    the one tool documented "read-only", did not — and ARBITER holds it with a
    policy that grants no write access at all.
    """
    target = tmp_path / "pwned"
    with pytest.raises(VcsError, match="may not start with"):
        LocalGit(clone).diff(f"--output={target}")
    assert not target.exists()


def test_a_branch_start_point_may_not_smuggle_in_an_option(clone):
    """`name` had two validators; `from_ref`, sitting beside it in argv, had none."""
    with pytest.raises(VcsError, match="may not start with"):
        LocalGit(clone).create_branch("qa/repro/x", from_ref="--upload-pack=touch /tmp/pwned")


# -- the capabilities that must not exist -----------------------------------


def test_nothing_in_the_adapter_merges():
    """§8.4: merge is a human decision. The guarantee is that no code exists."""
    names = [n for n in dir(GitHubVcs) if not n.startswith("__")]
    assert not [n for n in names if "merge" in n.lower()]

    source = inspect.getsource(GitHubVcs)
    assert "pr merge" not in source
    assert '"merge"' not in source


def test_no_method_accepts_a_force_flag():
    for name in dir(GitHubVcs):
        if name.startswith("__"):
            continue
        attr = inspect.getattr_static(GitHubVcs, name)
        func = attr.__func__ if isinstance(attr, (classmethod, staticmethod)) else attr
        if not callable(func):
            continue
        params = inspect.signature(func).parameters
        assert not [p for p in params if "force" in p.lower()], f"{name} takes a force flag"

    source = inspect.getsource(GitHubVcs)
    assert "--force" not in source
    assert "force-with-lease" not in source


def test_protected_head_covers_every_release_branch():
    assert is_protected_head("release")
    assert is_protected_head("release/2026.01")
    assert is_protected_head("refs/heads/main")
    assert not is_protected_head("fix/PROJ-1284-release-note")


# -- setup failures reach the user as instructions --------------------------


def test_a_missing_gh_says_how_to_install_it(clone, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(VcsError) as exc:
        GitHubVcs(clone).open_pr("fix/x", "t", "b", "main", ticket="P-1")
    message = str(exc.value)
    assert "gh auth login" in message
    assert "vcs: local" in message
    assert "Traceback" not in message


def test_an_unauthenticated_gh_says_to_log_in(fake_gh, clone, monkeypatch):
    monkeypatch.setenv("QAAS_FAKE_GH_UNAUTH", "1")
    with pytest.raises(VcsError) as exc:
        GitHubVcs(clone).open_pr("fix/x", "t", "b", "main", ticket="P-1")
    assert "gh auth login" in str(exc.value)
    assert fake_gh.calls_to("pr", "create") == [], "no PR is attempted before auth is known good"


def test_a_failing_gh_surfaces_its_own_stderr(fake_gh, clone, monkeypatch):
    monkeypatch.setenv("QAAS_FAKE_GH_PR_FAILS", "1")
    with pytest.raises(VcsError, match="No commits between main and x"):
        GitHubVcs(clone).open_pr("fix/x", "t", "b", "main", ticket="P-1")


# -- wiring -----------------------------------------------------------------


def test_build_vcs_returns_a_working_github_adapter(clone):
    adapter = build_vcs("github", clone)
    assert isinstance(adapter, GitHubVcs)
    assert isinstance(adapter, LocalGit), "the working tree half is plain git"
    assert adapter.current_branch() == "fix/PROJ-1284"


# -- the MCP layer decides who may publish ----------------------------------


def make_ctx(agent: AgentSpec, repo: Path, tmp_path: Path) -> ToolContext:
    config = load_config(search=CONFIG_SEARCH).model_copy(update={"vcs": "github"})
    root = tmp_path / ".qaas"
    return ToolContext(
        store=RunStore(f"gh-{agent.name.lower()}", root=root),
        maps=SystemMapStore(root),
        config=config,
        agent=agent,
        target_root=repo,
    )


def mender(repo: Path, tmp_path: Path) -> ToolContext:
    """The remediation agent §8.1 grants `fix/*` and PRs (open only)."""
    return make_ctx(
        AgentSpec(
            name="MENDER",
            layer="remediation",
            role="fix author",
            prompt="MENDER.md",
            policy={"write_paths": ["."], "branch_patterns": ["fix/*"], "may_open_pr": True},
        ),
        repo,
        tmp_path,
    )


def tools_for(ctx: ToolContext) -> dict:
    return handlers(build_tools(ctx))


def denials(ctx: ToolContext) -> list[dict]:
    return [entry.detail for entry in ctx.store.ledger("denial")]


async def test_mender_may_push_and_open_a_draft_pr(fake_gh, clone, tmp_path):
    ctx = mender(clone, tmp_path)
    tools = tools_for(ctx)

    pushed = await tools["push"]({})
    assert not pushed.get("isError"), pushed

    opened = await tools["open_pr"](
        {"title": "Check the caller's role", "body": "Why.", "ticket": "PROJ-1284"}
    )
    assert not opened.get("isError"), opened
    assert opened["structuredContent"]["number"] == 42
    assert opened["structuredContent"]["draft"] is True

    (argv,) = fake_gh.calls_to("pr", "create")
    assert flag_value(argv, "--head") == "fix/PROJ-1284"
    assert "--draft" in argv
    assert not denials(ctx)

    logged = [e.detail for e in ctx.store.ledger("vcs")]
    assert {"push", "open_pr"} <= {d.get("action") for d in logged}


@pytest.mark.parametrize("tool_name, args", [("push", {}), ("open_pr", {"title": "t", "body": "b", "ticket": "P-1"})])
async def test_an_agent_without_may_open_pr_is_refused_and_logged(fake_gh, clone, tmp_path, tool_name, args):
    """FORGE may branch and commit (§8.1) but publishing is not its job."""
    config_forge = load_config(search=CONFIG_SEARCH).agents["FORGE"]
    assert not config_forge.policy.may_open_pr

    ctx = make_ctx(config_forge, clone, tmp_path)
    result = await tools_for(ctx)[tool_name](args)

    assert result["isError"], result
    assert "may_open_pr" in result["content"][0]["text"]
    assert [d["tool"] for d in denials(ctx)] == [tool_name]
    assert denials(ctx)[0]["reason"]
    assert fake_gh.calls_to("pr", "create") == []


@pytest.mark.parametrize("branch", ["main", "master"])
async def test_the_server_refuses_a_protected_head_before_gh_is_called(fake_gh, clone, tmp_path, branch):
    ctx = mender(clone, tmp_path)
    tools = tools_for(ctx)

    pushed = await tools["push"]({"branch": branch})
    assert pushed["isError"]
    assert "protected" in pushed["content"][0]["text"]

    opened = await tools["open_pr"](
        {"branch": branch, "title": "t", "body": "b", "ticket": "P-1"}
    )
    assert opened["isError"]
    assert [d["tool"] for d in denials(ctx)] == ["push", "open_pr"]
    assert fake_gh.calls == [] or all(c[:2] != ["pr", "create"] for c in fake_gh.calls)


async def test_no_tool_on_the_server_merges(clone, tmp_path):
    names = set(tools_for(mender(clone, tmp_path)))
    assert not [n for n in names if "merge" in n]
    assert {"push", "open_pr", "pr_diff", "list_changed_files"} <= names


async def test_the_local_backend_says_it_has_no_remote(clone, tmp_path):
    ctx = mender(clone, tmp_path)
    ctx.config = ctx.config.model_copy(update={"vcs": "local"})
    result = await tools_for(ctx)["open_pr"](
        {"title": "t", "body": "b", "ticket": "P-1"}
    )
    assert result["isError"]
    assert "vcs: github" in result["content"][0]["text"]


# -- against a real repository ----------------------------------------------


@pytest.mark.github
def test_gh_is_installed_and_authenticated_here():
    """Excluded by default. Run with `-m github` to check this machine's setup."""
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "ref",
    ["main?foo=1", "main#frag", "../../other/compare/x", "main%2e%2e", "a b", "refs/heads/x:y"],
)
def test_a_ref_that_is_not_ref_shaped_is_refused(fake_gh, clone, ref):
    """`list_changed_files` interpolates a ref into a `gh api` *path*.

    `_reject_flaglike` only ever looked at the first character, so `?`, `#`, `%`
    and `..` all passed — enough to leave `/compare/` and reach a different
    endpoint. git itself allows none of them in a refname, so refusing them
    costs nothing.
    """
    with pytest.raises(VcsError):
        GitHubVcs(clone, repo_slug="acme/widgets").list_changed_files(ref, "main")


def test_a_pr_title_that_looks_like_a_flag_is_passed_as_a_value(fake_gh, clone):
    """A dash-leading title is legitimate; being re-read as a flag is not."""
    GitHubVcs(clone).open_pr("fix/x", "--repo=evil/repo", "b", "main", ticket="P-1")
    (argv,) = fake_gh.calls_to("pr", "create")
    assert flag_value(argv, "--title") == "--repo=evil/repo"
    assert "--repo=evil/repo" not in [a for a in argv if not a.startswith("--title=")]
