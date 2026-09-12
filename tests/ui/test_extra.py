"""A missing `[ui]` extra must be a sentence, not a traceback."""

from __future__ import annotations

import builtins

import pytest

from qaas.ui import EXTRA_HINT, MissingUIExtra, require_extra


def test_require_extra_passes_when_the_deps_are_installed() -> None:
    require_extra()


def test_a_missing_dep_names_every_one_that_is_missing(monkeypatch) -> None:
    # All three arrive from the same extra. Reporting the first and letting
    # someone install it only to hit the second is a worse experience than
    # naming them together.
    #
    # `import_module` is the seam, not `builtins.__import__`: starlette is
    # already in `sys.modules` in this environment, so an import hook never
    # runs and patching it proves nothing.
    import importlib

    real = importlib.import_module

    def refuse(name, *args, **kwargs):
        if name in ("starlette", "sse_starlette"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", refuse)
    with pytest.raises(MissingUIExtra) as caught:
        require_extra()
    assert "starlette" in str(caught.value)
    assert "sse_starlette" in str(caught.value)
    assert EXTRA_HINT in str(caught.value)


def test_the_hint_names_the_installable_thing() -> None:
    assert "qaas-python[ui]" in EXTRA_HINT


def test_the_read_model_is_importable_without_the_server(monkeypatch) -> None:
    """`qaas.ui.state` must not reach for a web dependency on import."""
    import importlib
    import sys

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.split(".")[0] in ("starlette", "uvicorn", "sse_starlette"):
            raise ImportError(f"No module named {name!r}")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    sys.modules.pop("qaas.ui.state", None)
    module = importlib.import_module("qaas.ui.state")
    assert module.PHASES[0] == "map"
