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

REPO = Path(__file__).resolve().parents[1]

#: Highest precedence first, exactly as `Workspace.resolve()` orders them.
CONFIG_SEARCH: tuple[Path, ...] = (
    REPO / "config",
    REPO / "src" / "qaas" / "defaults" / "config",
)

#: Where the skills that ship in the wheel live.
PACKAGED_SKILLS = REPO / "src" / "qaas" / "skills"

#: Where the prompts that ship in the wheel live.
PACKAGED_PROMPTS = REPO / "src" / "qaas" / "prompts"


@pytest.fixture(scope="session", autouse=True)
def _select_the_demo_target() -> None:
    """Point the suite at the bundled calibration target for the whole session."""
    os.environ.setdefault("QAAS_TARGET", "corvid")
