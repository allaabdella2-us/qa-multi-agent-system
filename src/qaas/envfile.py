"""Reading credentials out of a `.env` file, without a dependency.

Every credential this system needs is an environment variable, deliberately:
`config/` is committed, and a token in a committed file is a token that leaks.
That rule is right and it made the tool tedious to use — four exports in every
new shell, and a run that dies on the fourth because one was forgotten.

So: a `.env` next to the project, read once, at CLI start. Two rules keep it
from becoming a second configuration system:

* **The real environment always wins.** A value already exported is never
  overwritten, so `JIRA_PROJECT_KEY=OTHER qaas run` still means what it says,
  and a stale `.env` cannot silently redirect a run.
* **It is only ever read for credentials.** Nothing in `config/` is looked up
  here. A `.env` that sets `QAAS_TRACKER` works because that is an environment
  override that already existed, not because this file is config.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where to look, in order. The state directory first, because `.qaas/` is
#: already gitignored — a credential written there cannot be committed by
#: accident, which is not true of a `.env` at the root of someone's repository.
CANDIDATES = (".qaas/.env", ".env")

#: Overrides the search. A path names the file to read; an empty value turns
#: the whole mechanism off. Both are needed by real callers: CI passes real
#: environment variables and must not have a stray `.env` in a checkout
#: override them, and the test suite must be hermetic against whatever the
#: developer happens to have on disk.
ENV_FILE_VAR = "QAAS_ENV_FILE"


def parse_env(text: str) -> dict[str, str]:
    """`KEY=value` lines to a dict. Comments, blanks and `export ` tolerated.

    Quotes are stripped only when they wrap the whole value: a token that
    genuinely contains a quote character is more likely than a caller who meant
    to keep the wrapping ones.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(start: Path | str | None = None) -> tuple[Path | None, list[str]]:
    """Load the first `.env` found, without clobbering the real environment.

    Returns `(path, names_set)` — the file that was used and the variables it
    actually contributed. Names already present in the environment are reported
    as not set by the file, because that is the fact an operator debugging a
    wrong project key needs.
    """
    override = os.environ.get(ENV_FILE_VAR)
    if override is not None and not override.strip():
        return None, []

    base = Path(start).expanduser() if start else Path.cwd()
    candidates = (Path(override).expanduser(),) if override else tuple(base / n for n in CANDIDATES)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            values = parse_env(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            # An unreadable .env is not worth killing a command over; the
            # missing variable will produce a far clearer error downstream.
            return None, []
        applied = []
        for key, value in values.items():
            # Presence, not truthiness. `os.environ.get(key)` treated an
            # exported-but-empty variable as unset, so the file won — the exact
            # opposite of what this module, CLAUDE.md and
            # `test_the_real_environment_always_wins` all promise. The case that
            # matters is a CI job with `JIRA_API_TOKEN: ${{ secrets.X }}` where
            # the secret is not set: GitHub exports it as "", and a checkout
            # carrying an old `.env` would then file tickets into whatever
            # instance that stale token pointed at.
            if key in os.environ:
                continue
            os.environ[key] = value
            applied.append(key)
        return path, applied
    return None, []
