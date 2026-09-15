"""Shared test fixtures.

The one thing worth explaining here is `CONFIG_SEARCH`.

Config used to live at `<repo>/config/` and tests loaded it from there. But that
directory shipped to nobody: the wheel carried only Python and prompts, so every
assertion about "the config" was an assertion about a file no installed user
would ever have. The agent definitions and the default `system.yaml` now live
inside the package at `src/qaas/defaults/config/`, and `<repo>/config/` keeps
only `targets/` -- the bundled demo profile, which stays out of the wheel
because the application it points at is 69M and deliberately vulnerable.

So tests search both, in the same precedence a real user gets: project first,
packaged second. That means the suite now asserts the bytes that actually ship,
and it exercises the layering mechanism rather than a path unique to this
checkout.

`QAAS_TARGET` selects the demo profile for the whole session. The shipped
default deliberately names no target -- "installed but not pointed at anything"
is a legitimate state -- so the demo name belongs here, in this repo's own test
setup, not in the defaults everyone else receives.
"""

from __future__ import annotations

import os
from pathlib import Path

# Constants live in `tests/support.py`, not here. There are two conftest files --
# this one and `tests/mcp/conftest.py` -- and `from conftest import ...` resolves
# to whichever lands on sys.path first, which is the nested one. Duplicating them
# here left `PACKAGED_SKILLS` pointing at `src/qaas/skills` after skills moved
# into the plugin, so the copy silently named a directory that no longer exists:
# the exact failure shape `paths.py` was written to eliminate.


# Module level, not a session fixture, and that is the whole point of the
# rewrite. `tests/mcp/conftest.py` calls `load_config` at import time, and
# conftest modules are imported during *collection* -- before any fixture runs.
# So a session-scoped autouse fixture setting these was already too late for the
# module that needed them most: with `QAAS_TRACKER=jira` in a developer's shell,
# fourteen tests errored during collection while the fixture meant to prevent it
# sat waiting to run. The root conftest is imported before any nested one, so
# this is the earliest place that works.

#: The suite must not read the developer's `.env`. The CLI loads one before
#: every command, which is right for a person and fatal for a test: a suite that
#: passes on a laptop with real Jira credentials and fails in CI without them is
#: testing the laptop. Not a test hook -- `QAAS_ENV_FILE=` is the documented way
#: any caller turns the mechanism off.
os.environ["QAAS_ENV_FILE"] = ""

#: Every other `QAAS_*` knob a shell might hold, for the same reason.
#: `QAAS_TRACKER=jira` makes the agent fixtures build a real `JiraTracker`;
#: `QAAS_CONFIG_DIR` and `QAAS_HOME` silently repoint the whole config search;
#: `QAAS_VCS` swaps the version-control backend.
for _leaked in ("QAAS_TRACKER", "QAAS_VCS", "QAAS_CONFIG_DIR", "QAAS_HOME"):
    os.environ.pop(_leaked, None)

#: `QAAS_TARGET` is the exception: it is *set*, not cleared. The shipped default
#: deliberately names no target -- "installed but not pointed at anything" is a
#: legitimate state -- so the demo name belongs here, in this repository's own
#: test setup, and not in the defaults everyone else receives.
os.environ.setdefault("QAAS_TARGET", "corvid")
