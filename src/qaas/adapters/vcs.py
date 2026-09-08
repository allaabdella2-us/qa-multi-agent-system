"""Version control behind an adapter, so policy has one place to stand.

The adapter deliberately knows nothing about agents or policy: it is a thin,
argv-only wrapper over git. Every "may this agent do this" question is answered
one layer up, in `qaas.mcp.vcs`, because that is where the run context lives and
where a refusal can be logged to the ledger.

The split also keeps the §8.1 matrix honest when the backend changes. Swapping
`vcs: local` for `vcs: github` must not quietly widen what FORGE may do, and it
cannot, because the enforcement is not in here.

Two capabilities are absent on purpose rather than by oversight: there is no
force-push and no merge. §8.1 says never force-push and §8.4 says merge is
always human, and the cheapest way to guarantee that is to never write the code.

The hosted backend drives the `gh` CLI rather than the REST API, so the system
never holds a token: it acts as whoever is already logged in, with exactly their
access and no more.
"""

from __future__ import annotations

import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Sequence

# Long enough for a slow `git status` on a large tree, short enough that a
# hanging git (a credential prompt, a stuck lock) fails the tool call instead of
# stalling the agent's turn.
GIT_TIMEOUT_S = 60

# Network round trips (`gh pr create`, a clone) are slower than local git but
# still bounded: an agent's turn should fail loudly, never hang.
GH_TIMEOUT_S = 120

# Used only when the environment has no committer identity of its own. Never
# overrides a configured one: an agent commit in a real repo should carry the
# repo's configured author, not a synthetic one.
FALLBACK_IDENTITY = ("QAaS Agent", "qaas-agent@localhost")


class VcsError(RuntimeError):
    """A git operation failed. Carries the command's own stderr, which is the
    only text an agent can actually act on."""


class VcsAdapter(ABC):
    """The operations the QA system needs from version control. Nothing more.

    Kept small on purpose: every method here is a method some agent can reach
    through the vcs MCP server, so each addition widens the blast radius.
    """

    @abstractmethod
    def current_branch(self) -> str:
        """Name of the checked-out branch ('HEAD' if detached)."""

    @abstractmethod
    def create_branch(self, name: str, from_ref: str | None = None) -> str:
        """Create `name` and switch to it. Returns the branch now checked out."""

    @abstractmethod
    def write_files(self, files: Mapping[str, str]) -> list[str]:
        """Write repo-relative path -> content. Returns the paths written."""

    @abstractmethod
    def commit(self, message: str, paths: Sequence[str] | None = None) -> str:
        """Stage `paths` (all tracked changes if None) and commit. Returns the sha."""

    @abstractmethod
    def diff(self, ref: str | None = None, paths: Sequence[str] | None = None) -> str:
        """Unified diff of the working tree, optionally against `ref`."""

    @abstractmethod
    def list_branches(self) -> list[str]:
        """Local branch names."""


class LocalGit(VcsAdapter):
    """git in a local checkout, driven by argv lists.

    Never takes a command string. Everything an agent supplies arrives as a
    single argv element, so a branch named `x; rm -rf /` is a branch name git
    rejects, not a shell fragment.
    """

    def __init__(self, repo: Path | str, timeout_s: int = GIT_TIMEOUT_S):
        self.repo = Path(repo).resolve()
        self.timeout_s = timeout_s

    # -- plumbing ---------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VcsError(f"git {' '.join(args)} timed out after {self.timeout_s}s") from exc
        except OSError as exc:  # git missing, repo path gone
            raise VcsError(f"could not run git: {exc}") from exc
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
            raise VcsError(f"git {' '.join(args)} failed: {detail}")
        return proc.stdout

    def _identity_args(self) -> list[str]:
        """`-c` overrides only when the environment has no identity configured."""
        configured = self._git("config", "--get", "user.email", check=False).strip()
        if configured:
            return []
        name, email = FALLBACK_IDENTITY
        return ["-c", f"user.name={name}", "-c", f"user.email={email}"]

    # -- VcsAdapter -------------------------------------------------------

    def current_branch(self) -> str:
        # `branch --show-current` answers even on a repo with no commits yet,
        # where `rev-parse --abbrev-ref HEAD` errors out.
        name = self._git("branch", "--show-current").strip()
        return name or "HEAD"

    def create_branch(self, name: str, from_ref: str | None = None) -> str:
        args = ["checkout", "-b", name]
        if from_ref:
            args.append(from_ref)
        self._git(*args)
        return self.current_branch()

    def checkout(self, ref: str) -> str:
        """Switch to an existing ref. Not part of the abstract surface: only the
        local backend has a working tree to switch."""
        self._git("checkout", ref)
        return self.current_branch()

    def write_files(self, files: Mapping[str, str]) -> list[str]:
        written: list[str] = []
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            written.append(rel)
        return written

    def commit(self, message: str, paths: Sequence[str] | None = None) -> str:
        if paths:
            self._git("add", "--", *paths)
        else:
            self._git("add", "-A")
        staged = self._git("diff", "--cached", "--name-only").strip()
        if not staged:
            raise VcsError("nothing staged to commit")
        self._git(*self._identity_args(), "commit", "-m", message)
        return self._git("rev-parse", "HEAD").strip()

    def diff(self, ref: str | None = None, paths: Sequence[str] | None = None) -> str:
        args = ["diff"]
        if ref:
            args.append(ref)
        if paths:
            args.extend(["--", *paths])
        return self._git(*args)

    def list_branches(self) -> list[str]:
        out = self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        return [line.strip() for line in out.splitlines() if line.strip()]


# Branches an agent may never publish or use as a PR head. Enforced in the
# adapter and not only in `qaas.mcp.vcs`, because the adapter is the layer that
# actually touches the user's remote: a caller that skips the MCP server (a
# script, a future runner, a mistake) still cannot push to main.
PROTECTED_HEAD_BRANCHES = frozenset({"main", "master", "trunk", "develop"})
PROTECTED_HEAD_PREFIXES = ("release",)

# Appended to every PR body. A reviewer looking at a pull request has to be able
# to tell in one line that no human wrote it and which finding it came from;
# §8.4 makes the merge their decision, and they cannot make it well while
# mistaking the author for a colleague.
AUTOMATION_NOTICE = (
    "---\n"
    "Opened by an automated QA agent (QAaS) for ticket {ticket}. "
    "No human authored this branch. Merge is a human decision (architecture §8.4)."
)

_PR_NUMBER_IN_URL = re.compile(r"/pull/(\d+)")

_GH_MISSING = (
    "the GitHub CLI ('gh') is not available: {detail}. Install it from "
    "https://cli.github.com, run `gh auth login`, or set `vcs: local` in "
    "config/system.yaml to work against a local checkout instead."
)
_GH_UNAUTHENTICATED = (
    "the GitHub CLI is not authenticated: {detail}. Run `gh auth login` (the "
    "adapter deliberately holds no token of its own and reuses your gh "
    "credentials), or set `vcs: local` in config/system.yaml."
)


def _reject_flaglike(kind: str, value: str) -> str:
    """Refuse a ref/slug that could be read as an option by git or gh.

    Everything is passed as its own argv element, so there is no shell to
    escape; the one remaining trick is a value like `--upload-pack=...` landing
    where a ref was expected. Names cannot start with `-`, so refusing that is
    free.
    """
    text = str(value).strip()
    if not text:
        raise VcsError(f"{kind} is required.")
    if text.startswith("-"):
        raise VcsError(f"refusing {kind} '{text}': a ref may not start with '-'.")
    return text


def is_protected_head(branch: str) -> bool:
    """Whether `branch` is one no agent may push or open a PR from (§8.1)."""
    name = branch.strip().lower().removeprefix("refs/heads/")
    return name in PROTECTED_HEAD_BRANCHES or name.startswith(PROTECTED_HEAD_PREFIXES)


class GitHubVcs(LocalGit):
    """A real GitHub checkout: local git for the tree, `gh` for the hosted side.

    Driving `gh` rather than the REST API is a deliberate trade. `gh` is already
    authenticated on a developer's machine, so the system never holds, stores or
    refreshes a token of its own, and it acts as the user with exactly the
    access the user already has — a QA agent should not be able to reach further
    into a repository than the person who pointed it there.

    Inherits the working-tree half from `LocalGit` because that half genuinely
    is plain git in a clone; only publishing (push, PR) needs GitHub.

    Three capabilities are missing on purpose and must stay missing: there is no
    merge (§8.4 makes that a human act), no force-push (§8.1), and no way to use
    main/master/trunk/develop/release* as a head branch.
    """

    # No token here on purpose: `gh auth` owns credentials. These only narrow or
    # override what `gh` would infer from the checkout's remotes.
    OPTIONAL_ENV = ("GITHUB_REPOSITORY", "GITHUB_DEFAULT_BRANCH")

    DEFAULT_BASE = "main"

    def __init__(
        self,
        repo: Path | str,
        *,
        repo_slug: str | None = None,
        gh_path: str = "gh",
        timeout_s: int = GH_TIMEOUT_S,
        remote: str = "origin",
    ):
        super().__init__(repo, timeout_s=timeout_s)
        # `owner/name`. Left unset, gh infers it from the checkout's remotes,
        # which is the right answer whenever the system was pointed at a clone.
        self.repo_slug = repo_slug or os.environ.get("GITHUB_REPOSITORY") or None
        self.gh_path = gh_path
        self.remote = remote
        self._gh_ready = False

    # -- plumbing ---------------------------------------------------------

    def _gh(self, *args: str, check: bool = True) -> str:
        """Run `gh` in the checkout, argv-only, with `gh`'s own stderr surfaced.

        The stderr matters more than the exit code: "no commits between main and
        x" or "must be authenticated" is the only text an agent can act on.
        """
        self._require_gh()
        return _run_gh([self.gh_path, *args], self.repo, self.timeout_s, check=check)

    def _require_gh(self) -> None:
        """Fail with instructions, once, rather than a traceback per call."""
        if self._gh_ready:
            return
        _ensure_gh(self.gh_path, self.timeout_s, cwd=self.repo)
        self._gh_ready = True

    def _repo_args(self) -> list[str]:
        """`--repo owner/name` when we were told which repo; nothing otherwise."""
        return ["--repo", self.repo_slug] if self.repo_slug else []

    def _api_repo_path(self) -> str:
        """`gh api` expands `{owner}`/`{repo}` from the checkout when unset."""
        return self.repo_slug or "{owner}/{repo}"

    def default_base(self) -> str:
        """Base branch for a new PR when the caller names none."""
        return os.environ.get("GITHUB_DEFAULT_BRANCH") or self.DEFAULT_BASE

    @staticmethod
    def _guard_head(branch: str) -> str:
        name = _reject_flaglike("branch", branch)
        if is_protected_head(name):
            raise VcsError(
                f"refusing to publish '{name}': it is a protected branch (§8.1). "
                "Agent work belongs on its own branch, and merging into a "
                "protected branch is a human decision (§8.4)."
            )
        return name

    # -- publishing -------------------------------------------------------

    def push(self, branch: str | None = None) -> str:
        """Publish `branch` (default: the current one) to the remote.

        Takes no force flag and never will. `--set-upstream` is here so the
        follow-up `gh pr create` can resolve the head without the agent having
        to know about tracking refs.
        """
        name = self._guard_head(branch or self.current_branch())
        self._git("push", "--set-upstream", self.remote, name)
        return name

    def open_pr(
        self,
        branch: str,
        title: str,
        body: str,
        base: str | None = None,
        draft: bool = True,
        *,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        """Open a pull request from `branch`, as a draft unless told otherwise.

        Draft is the default because a ready-for-review PR pulls a human into a
        review they did not ask for; a draft waits for one. `ticket` is required
        rather than optional so the body can always say what this PR is for —
        see AUTOMATION_NOTICE.
        """
        head = self._guard_head(branch)
        base_ref = _reject_flaglike("base", base or self.default_base())
        subject = str(title).strip()
        if not subject:
            raise VcsError("a PR needs a title.")
        key = str(ticket or "").strip()
        if not key:
            raise VcsError(
                "open_pr needs the ticket this work came from: every agent PR "
                "must say on its face what finding it answers."
            )

        args = [
            "pr",
            "create",
            *self._repo_args(),
            "--head",
            head,
            "--base",
            base_ref,
            "--title",
            subject,
            "--body",
            self.pr_body(body, key),
        ]
        if draft:
            args.append("--draft")

        out = self._gh(*args).strip()
        url = next(
            (line.strip() for line in reversed(out.splitlines()) if line.strip().startswith("http")),
            "",
        )
        match = _PR_NUMBER_IN_URL.search(url)
        return {
            "number": int(match.group(1)) if match else None,
            "url": url or out,
            "branch": head,
            "base": base_ref,
            "draft": draft,
        }

    @staticmethod
    def pr_body(body: str, ticket: str) -> str:
        """The agent's body with the automated-origin line appended, always."""
        return f"{str(body).rstrip()}\n\n{AUTOMATION_NOTICE.format(ticket=ticket)}\n"

    # -- reading a PR -----------------------------------------------------

    def pr_diff(self, number: int | str) -> str:
        """Unified diff of an existing PR, for an agent asked to review one."""
        ref = _reject_flaglike("pr number", str(number))
        return self._gh("pr", "diff", *self._repo_args(), ref)

    def list_changed_files(self, base: str, head: str) -> list[str]:
        """Paths that differ between two refs, via the compare endpoint.

        Asking GitHub rather than the local tree means this answers for a PR
        whose head was never fetched into this checkout.
        """
        base_ref = _reject_flaglike("base", base)
        head_ref = _reject_flaglike("head", head)
        out = self._gh(
            "api",
            f"repos/{self._api_repo_path()}/compare/{base_ref}...{head_ref}",
            "--jq",
            ".files[].filename",
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    # -- getting a checkout at all ----------------------------------------

    @classmethod
    def clone(
        cls,
        url: str,
        dest: Path | str,
        *,
        depth: int = 1,
        gh_path: str = "gh",
        timeout_s: int = GH_TIMEOUT_S,
    ) -> "GitHubVcs":
        """Shallow-clone `url` into `dest` and return an adapter rooted there.

        Shallow because the agents read the current tree, never the history, and
        a full clone of a real repository is minutes of wall clock the run does
        not have. `gh repo clone` rather than `git clone` so the user's existing
        credentials cover private repositories.
        """
        target = Path(dest)
        parent = target.parent if target.parent.exists() else Path.cwd()
        _ensure_gh(gh_path, timeout_s, cwd=parent)
        _run_gh(
            [gh_path, "repo", "clone", _reject_flaglike("url", url), str(target), "--", f"--depth={depth}"],
            parent,
            timeout_s,
        )
        return cls(target, gh_path=gh_path, timeout_s=timeout_s)


def _run_gh(argv: list[str], cwd: Path, timeout_s: int, *, check: bool = True) -> str:
    """One `gh` invocation. Shared by the adapter and by `clone`, which has no
    instance yet."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VcsError(f"gh {' '.join(argv[1:])} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise VcsError(_GH_MISSING.format(detail=exc)) from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise VcsError(f"gh {' '.join(argv[1:])} failed: {detail}")
    return proc.stdout


def _ensure_gh(gh_path: str, timeout_s: int, cwd: Path) -> None:
    """Check gh is installed and logged in, and say what to do when it is not.

    Both failures are ordinary setup mistakes rather than bugs, so they must
    reach the user as an instruction ("run `gh auth login`") and never as a
    traceback out of subprocess.
    """
    try:
        proc = subprocess.run(
            [gh_path, "auth", "status"],
            cwd=cwd if cwd.exists() else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VcsError(f"`gh auth status` timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise VcsError(_GH_MISSING.format(detail=exc)) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise VcsError(_GH_UNAUTHENTICATED.format(detail=detail))


def build_vcs(backend: str, repo_root: Path | str) -> VcsAdapter:
    """Pick the adapter named by `config.vcs`."""
    if backend == "local":
        return LocalGit(repo_root)
    if backend == "github":
        return GitHubVcs(repo_root)
    raise ValueError(f"unknown vcs backend '{backend}'; expected 'local' or 'github'")
