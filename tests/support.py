"""Constants shared by the test suite.

Not in `conftest.py` because there are two of those -- `tests/conftest.py` and
`tests/mcp/conftest.py` -- and `from conftest import ...` resolves to whichever
lands on sys.path first, which is the nested one. A plain module has one name
and one meaning.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Config search order, highest precedence first -- the same order a real user
#: gets from `Workspace.resolve()`. `<repo>/config/` holds only the bundled demo
#: target (kept out of the wheel); everything else ships inside the package.
CONFIG_SEARCH: tuple[Path, ...] = (
    REPO / "config",
    REPO / "src" / "qaas" / "defaults" / "config",
)

PACKAGED_SKILLS = REPO / "src" / "qaas" / "skills"
PACKAGED_PROMPTS = REPO / "src" / "qaas" / "prompts"

#: A single directory containing a complete config, for tests that copy or
#: pass --config and therefore need one real path rather than a search order.
PACKAGED_CONFIG = REPO / "src" / "qaas" / "defaults" / "config"
