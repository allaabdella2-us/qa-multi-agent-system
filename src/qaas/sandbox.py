"""The operating-system sandbox around an agent's shell.

`guardrails.py` reads a shell command for what it writes, and that is
best-effort by construction: `python foo.py` is arbitrary code, and a script
the target repository ships -- a `conftest.py`, a `package.json` test script --
can write, read and send whatever it likes while the command that ran it looks
like `python -m pytest`. That residual was stated in CLAUDE.md as a decision,
because refusing it refuses how FIXER and VERIFIER run the suite.

Claude Code can confine a Bash command and everything it spawns at the kernel
(Seatbelt on macOS, bubblewrap on Linux). This module turns an agent's policy
into those settings, so the residual is enforced rather than stated:

  * **Writes** stay inside the target checkout and a private temp directory.
    `.git/hooks`, `.git/config`, `.claude/`, `.env*` and qaas's own state are
    denied even there -- a hook or a git config written by sandboxed code runs
    later, *outside* the sandbox, the next time the operator commits.
  * **Reads** of credential stores (`~/.ssh`, `~/.config/gh`, `~/.aws`,
    `.qaas/.env`, ...) are denied. The agent's own `Read` tool is not affected;
    this is about code from the target repository running in the shell.
  * **Network** reaches the target's own hosts and loopback, nothing else. A
    `conftest.py` that reads a token has nowhere to send it.

The parsed rules in `guardrails.py` still run first -- the PreToolUse hook
sees every Bash call, sandboxed or not (verified against the bundled CLI) --
and still do the part the sandbox cannot: *where inside the checkout* an agent
may write, the diff budget, the git rules. The sandbox is the floor under them.

Every value here was checked against the bundled Claude Code by driving it
through a scripted stand-in for the API, because a sandbox that silently
blocks the happy path -- localhost, `tmp_path`, `git commit` -- is one that
gets switched off. Two of the settings exist because of what that found:
`allowLocalBinding`, without which a test cannot reach the app on localhost,
and a private `CLAUDE_CODE_TMPDIR` that is also on `allowWrite`, without which
`TMPDIR` points somewhere unwritable and Python's `tempfile` falls back to
writing into the checkout.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from qaas.config import AgentSpec, SystemConfig
    from qaas.mcp.context import ToolContext

#: Credential stores no sandboxed command may read. Missing paths cost nothing.
CREDENTIAL_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.config/gh",
    "~/.kube",
    "~/.docker/config.json",
    "~/.gnupg",
    "~/.netrc",
    "~/.git-credentials",
    "~/.pypirc",
    "~/.npmrc",
)

#: Always reachable: the target running locally is the ordinary case.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

#: The env var Claude Code reads for its session temp directory.
TMPDIR_ENV = "CLAUDE_CODE_TMPDIR"

#: Where qaas's own state lives, relative to the state root. Denied one by one
#: when the target is itself under the state root (`qaas run --repo` clones
#: into `.qaas/targets/`), where denying the root would deny the target.
_STATE_ENTRIES = (
    "runs", "config", "prompts", "system-map", "tickets", "scores", "generated",
    "artifacts", "memory.db", ".env", ".gitignore",
)


def applies(spec: "AgentSpec") -> bool:
    """Only the shell is sandboxed; an agent without Bash has nothing to confine."""
    return "Bash" in spec.builtin_tools


def support() -> tuple[bool, str]:
    """Whether this machine can sandbox a shell, and how -- or what is missing."""
    system = platform.system()
    if system == "Darwin":
        if Path("/usr/bin/sandbox-exec").exists():
            return True, "macOS Seatbelt"
        return False, "/usr/bin/sandbox-exec is missing"
    if system == "Linux":
        missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool) is None]
        if not missing:
            return True, "bubblewrap"
        names = " and ".join("bubblewrap" if t == "bwrap" else t for t in missing)
        return False, f"install {names} (e.g. `apt-get install bubblewrap socat`)"
    return False, f"Claude Code sandboxes Bash on macOS and Linux only, not {system}"


def status(spec: "AgentSpec", config: "SystemConfig") -> str | None:
    """One line for the ledger and `qaas doctor`, or None for an agent with no shell."""
    if not applies(spec):
        return None
    mode = config.sandbox.mode
    if mode == "off":
        return "off (sandbox.mode: off)"
    available, how = support()
    if available:
        return f"on ({how})"
    if mode == "required":
        return f"required but unavailable: {how}"
    return f"unavailable, shell runs unsandboxed: {how}"


def make_tmpdir(agent: str) -> Path:
    """A private temp directory for one agent invocation.

    Under `/tmp` rather than the platform default: the sandbox puts its proxy
    sockets inside it, and macOS's `/var/folders/...` prefix leaves little of
    the 104-byte limit on a Unix socket path. `mkdtemp`, not a predictable
    name, because another local user could plant a symlink at one.
    """
    base = "/tmp" if os.name == "posix" and Path("/tmp").is_dir() else None
    return Path(tempfile.mkdtemp(prefix=f"qaas-{agent.lower()}-", dir=base))


def cleanup(options: Any) -> None:
    """Remove the private temp directory `settings_for` created. Never raises."""
    env = getattr(options, "env", None) or {}
    path = env.get(TMPDIR_ENV)
    if path and Path(path).name.startswith("qaas-"):
        shutil.rmtree(path, ignore_errors=True)


def _hosts(config: "SystemConfig") -> list[str]:
    """The target's own hosts, from the profile's URLs."""
    profile = getattr(config, "profile", None)
    env = getattr(profile, "environment", None)
    hosts: list[str] = []
    for url in (getattr(env, "api_url", None), getattr(env, "web_url", None)):
        if not url:
            continue
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def settings_for(spec: "AgentSpec", ctx: "ToolContext", tmpdir: Path) -> dict[str, Any] | None:
    """The `sandbox` settings for one agent, or None when it gets none.

    `tmpdir` becomes the agent's `CLAUDE_CODE_TMPDIR`; the caller sets that.
    """
    if not applies(spec):
        return None
    config = ctx.config
    mode = config.sandbox.mode
    if mode == "off":
        return None

    target = ctx.target_root.resolve()
    state = Path(ctx.store.root).resolve()
    home = Path.home()

    deny_write = [
        target / ".git" / "hooks",
        target / ".git" / "config",
        target / ".claude",
        target / ".qaas",
        target / ".env",
    ]
    if target == state or target.is_relative_to(state):
        deny_write += [state / name for name in _STATE_ENTRIES]
    else:
        deny_write.append(state)

    deny_read = [Path(p.replace("~", str(home), 1)) for p in CREDENTIAL_PATHS]
    deny_read += [state / ".env", state / "memory.db"]
    # The project's own `.env` holds qaas's Jira token when the operator put it
    # there -- but when the project *is* the target, it is also the
    # application's, and its test suite may load it. Denied only when it is not
    # the target's own file.
    project_env = state.parent / ".env"
    if project_env.parent != target:
        deny_read.append(project_env)

    domains = [*LOOPBACK_HOSTS, *_hosts(config), *config.sandbox.allowed_domains]
    return {
        "enabled": True,
        # `required` turns a missing sandbox into a failed agent; `auto` lets
        # Claude Code warn and run the shell unsandboxed, which `status`
        # records, because the parsed guardrails still stand.
        "failIfUnavailable": mode == "required",
        # The escape hatch is closed: with the default `true`, a command the
        # sandbox refused could be retried with `dangerouslyDisableSandbox`, and
        # this roster allowlists Bash, so the retry would be auto-approved.
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "excludedCommands": [],
        "network": {
            "allowedDomains": list(dict.fromkeys(domains)),
            # Without it a direct connection to localhost is refused on macOS
            # even with loopback allowed, and a test cannot reach the app.
            "allowLocalBinding": True,
        },
        "filesystem": {
            "allowWrite": [str(target), str(tmpdir)],
            "denyWrite": [str(p) for p in dict.fromkeys(deny_write)],
            "denyRead": [str(p) for p in dict.fromkeys(deny_read)],
        },
    }
