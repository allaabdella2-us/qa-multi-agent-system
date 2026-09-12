"""The `qaas dashboard` web UI.

Two rules hold this package together.

It is **read-only**. It tails a run's ledger and renders it; it never dispatches
an agent, files a ticket or writes to a target. A view that can spend money is a
view someone has to trust, and this one does not have to be trusted.

Its web dependencies are **optional**. `qaas.ui.state` is pure Python over the
run store and imports in any install, which is what lets the read model be
tested in the default offline suite. Only `qaas.ui.server` needs starlette, and
`require_extra()` is how a missing `[ui]` extra becomes a sentence instead of a
traceback.
"""

from __future__ import annotations

#: What to install, named once so the CLI and the server cannot disagree.
EXTRA_HINT = "the dashboard needs the ui extra — pip install 'qaas-python[ui]'"


class MissingUIExtra(RuntimeError):
    """Raised when the web dependencies are not installed."""

    def __init__(self, missing: str = "") -> None:
        super().__init__(f"{EXTRA_HINT}{f' (missing: {missing})' if missing else ''}")
        self.missing = missing


def require_extra() -> None:
    """Raise `MissingUIExtra` unless every web dependency imports.

    Checked together rather than one at a time: all three arrive from the same
    extra, so reporting the first missing one and letting the user install it
    only to hit the second is a worse experience than naming all of them.
    """
    import importlib

    missing = [
        name
        for name in ("starlette", "uvicorn", "sse_starlette")
        if not _importable(importlib, name)
    ]
    if missing:
        raise MissingUIExtra(", ".join(missing))


def _importable(importlib_module, name: str) -> bool:
    try:
        importlib_module.import_module(name)
    except ImportError:
        return False
    return True
