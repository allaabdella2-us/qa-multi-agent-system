"""qaas: a harness that runs governed Claude Code sessions over a repository.

Deliberately almost empty. Everything `import qaas.<module>` loads must stay
importable without side effects, so nothing heavier than the version belongs
here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

#: The distribution is `qaas-python` (`qaas` was taken); the import package is
#: `qaas`. Read from the installed metadata rather than restated here, so
#: `pyproject.toml` stays the one place a release number is written. There was
#: no `__init__.py` at all before this, so the package shipped as a namespace
#: package and there was nothing for `qaas --version` to read.
try:
    __version__ = _dist_version("qaas-python")
except PackageNotFoundError:  # a bare source tree on sys.path, never installed
    __version__ = "0+unknown"
