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

PACKAGED_PLUGIN = REPO / "src" / "qaas" / "plugin"
PACKAGED_SKILLS = PACKAGED_PLUGIN / "skills"
PACKAGED_PROMPTS = REPO / "src" / "qaas" / "prompts"

#: A single directory containing a complete config, for tests that copy or
#: pass --config and therefore need one real path rather than a search order.
PACKAGED_CONFIG = REPO / "src" / "qaas" / "defaults" / "config"


#: The bundled demo target profile, which lives in `<repo>/config/` rather than
#: in the package -- the application it points at is 69M of deliberately
#: vulnerable code and has no business in a wheel.
PACKAGED_TARGETS = REPO / "config" / "targets"


def make_project(root: Path) -> Path:
    """A scratch directory `find_project` recognises, with a *complete* config.

    Complete means it has `targets/` too. A config with agents and no targets is
    not a state a user is ever in, and copying only the packaged half made the
    suite depend on a bug: a target named by `QAAS_TARGET` and absent from the
    config path used to be silently ignored, so these scratch projects "worked"
    while pointing every write-path sandbox at the process's working directory.
    Now that an absent named target is fatal, the fixture has to build the thing
    it was pretending to build.
    """
    import shutil

    config = root / ".qaas" / "config"
    shutil.copytree(PACKAGED_CONFIG, config)
    if PACKAGED_TARGETS.is_dir():
        shutil.copytree(PACKAGED_TARGETS, config / "targets", dirs_exist_ok=True)
    return root


def write_scratch_target(config_dir: Path, root: Path, name: str = "corvid") -> Path:
    """A minimal target profile inside a scratch config, pointing at a real path.

    The suite names a target through `QAAS_TARGET`, and a named target absent
    from the config path is fatal -- it used to be silently ignored, which left
    `target_root()` pointing at whatever directory the process happened to be
    standing in while the run reported success. Every scratch config therefore
    needs a profile, and it has to be a *scratch* one: symlinking the
    repository's own `config/targets/` in was tried, and the CLI's
    `_writable_targets_dir` wrote a generated profile straight back through the
    link into the real checkout.
    """
    targets = config_dir / "targets"
    targets.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    path = targets / f"{name}.yaml"
    path.write_text(
        f"name: {name}\nroot: {root}\ndescription: a scratch target\n"
        "default_branch: main\nlayout:\n  backend: [.]\n"
        "environment:\n  mode: none\nauth:\n  mode: none\n",
        encoding="utf-8",
    )
    return path
