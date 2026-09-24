"""Whether the browser the Playwright MCP server drives is installed.

BROWSER and GUIDE work through `@playwright/mcp`, and each release of it wants
one exact browser build. The setup docs said `npx playwright install chromium`,
which installs the build the *playwright CLI* wants -- another release, another
build number -- so in the first run of the whole roster both agents started, got
`Browser "chrome-for-testing" is not installed`, tried to install it through a
Bash they do not hold, and spent $5.06 between them without opening a page.
BROWSER fell back to raw HTTP; GUIDE, whose whole method is the browser, found
nothing. Nothing had checked.

So the pinned server is asked itself -- `install-browser --dry-run` prints where
its build lives -- before either agent is dispatched, and `qaas doctor` prints
the one command that fixes it. A check that cannot run answers "unknown", never
"missing": refusing to dispatch BROWSER because npm was slow would be the same
mistake in the other direction.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from qaas.registry import PLAYWRIGHT_MCP_VERSION

#: The build `--browser chromium` launches. Recent Playwright ships Chrome for
#: Testing under the `chromium` name, and the server's own error names it so.
BROWSER_NAME = "chrome-for-testing"

#: Long enough for npx to fetch the server once on a cold cache.
PROBE_TIMEOUT_S = 120


def install_argv() -> list[str]:
    return ["npx", "-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}", "install-browser", BROWSER_NAME]


def install_command() -> str:
    return " ".join(install_argv())


def needed_by(config: Any) -> list[str]:
    """Enabled agents that drive the bundled Playwright server."""
    agents = getattr(config, "agents", {}) or {}
    return sorted(n for n, s in agents.items() if s.enabled and "playwright" in s.mcp_servers)


def status(config: Any = None) -> tuple[bool | None, str]:
    """(installed, what to say). `None` means this could not be determined.

    A project that declares its own `playwright` under `mcp_servers:` has chosen
    its own server and browser; that is not this check's to second-guess.
    """
    if "playwright" in (getattr(config, "mcp_servers", None) or {}):
        return None, "playwright is declared in system.yaml; its browser is not checked"
    if shutil.which("npx") is None:
        return False, "npx is not on PATH -- the Playwright server needs Node.js"
    locations, problem = _install_locations()
    if problem:
        return None, problem
    # ffmpeg records video, which nothing here asks for; its absence must not
    # cost a run its two browser agents.
    missing = [p for p in locations if not Path(p).exists() and not Path(p).name.startswith("ffmpeg")]
    if missing:
        return False, (
            f"the browser @playwright/mcp@{PLAYWRIGHT_MCP_VERSION} launches is not installed "
            f"({', '.join(Path(p).name for p in missing)}). Run: {install_command()}"
        )
    return True, f"installed ({', '.join(Path(p).name for p in locations)})"


@functools.lru_cache(maxsize=1)
def _install_locations() -> tuple[tuple[str, ...], str | None]:
    """Where the pinned server expects its browser, by asking it. Cached: the
    answer cannot change within one process, and the router and doctor both ask."""
    try:
        proc = subprocess.run(
            [*install_argv(), "--dry-run"],
            capture_output=True, text=True, errors="replace",
            timeout=PROBE_TIMEOUT_S, check=False,
            # Not the target's directory: a `package.json` there has no say in
            # which browser this server wants.
            cwd=tempfile.gettempdir(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (), f"could not ask the Playwright server which browser it needs: {exc}"
    locations = tuple(
        line.split(":", 1)[1].strip()
        for line in proc.stdout.splitlines()
        if line.strip().startswith("Install location:")
    )
    if proc.returncode != 0 or not locations:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        return (), f"could not ask the Playwright server which browser it needs: {tail[0]}"
    return locations, None
