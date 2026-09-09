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

import pytest

# Constants live in `tests/support.py`, not here. There are two conftest files --
# this one and `tests/mcp/conftest.py` -- and `from conftest import ...` resolves
# to whichever lands on sys.path first, which is the nested one. Duplicating them
# here left `PACKAGED_SKILLS` pointing at `src/qaas/skills` after skills moved
# into the plugin, so the copy silently named a directory that no longer exists:
# the exact failure shape `paths.py` was written to eliminate.


@pytest.fixture(scope="session", autouse=True)
def _select_the_demo_target() -> None:
    """Point the suite at the bundled calibration target for the whole session."""
    os.environ.setdefault("QAAS_TARGET", "corvid")


@pytest.fixture(scope="session", autouse=True)
def _ignore_any_dotenv() -> None:
    """The suite must not read the developer's `.env`.

    The CLI loads one before every command, which is correct for a person and
    fatal for a test: a suite that passes on a laptop with real Jira credentials
    and fails in CI without them is testing the laptop. This is not a test hook
    -- `QAAS_ENV_FILE=` is the documented way any caller turns the mechanism off.
    """
    os.environ["QAAS_ENV_FILE"] = ""
