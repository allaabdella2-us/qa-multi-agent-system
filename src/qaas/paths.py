"""Where qaas finds its own resources: config, prompts, skills, state.

This module exists because the package used to assume it was running from its
own git checkout. `config/` and `.claude/skills/` sat at the repo root, outside
the wheel, and were looked up relative to the process CWD or by climbing
`Path(__file__).parents[2]` -- which, once installed, lands in
`site-packages/../..`. A `pip install` therefore produced a CLI where every
command that needed config died, and `qaas validate` failed *always*, because
none of the 30 skills it checks for were anywhere on disk.

Three different ideas had been collapsed into `Path.cwd()`:

  1. where qaas's own resources live      -> packaged, or overridden by the user
  2. where the user's project state lives -> `.qaas/`
  3. where the application under test is  -> the target profile's root

This module owns the first two. The third belongs to `TargetProfile.root_path`.

The precedence is the same for every kind of resource, and it is the ordinary
one: an explicit flag beats the project, and the project beats what we shipped.

    1. explicit  --config / QAAS_CONFIG_DIR
    2. project   <project>/.qaas/config, and the source-checkout <project>/config
    3. packaged  src/qaas/defaults/config, src/qaas/prompts, src/qaas/skills

Layering granularity differs by kind, and that difference is deliberate:

  * `system.yaml`   first hit wins **whole**. Merging run-mode dictionaries
                    across layers produces a configuration nobody wrote and
                    nobody can read back.
  * `agents/*.yaml` union by filename, higher layer shadows. Someone who wants
                    MENDER's budget raised drops in one file; they do not fork
                    eight and freeze themselves on today's roster.
  * prompts/skills  union by name, higher layer shadows, same reasoning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: Overrides, in the order a reader will look for them.
CONFIG_DIR_ENV = "QAAS_CONFIG_DIR"
HOME_ENV = "QAAS_HOME"

#: The state directory, and the config dir inside it. `.qaas/` holds both a
#: user's committed config and their disposable run state, so `init` writes a
#: `.gitignore` inside it -- otherwise the obvious `.gitignore` line for `.qaas/`
#: would drop their configuration too.
STATE_DIRNAME = ".qaas"
PROJECT_CONFIG = "config"

#: How far up to look for a project before giving up and using packaged defaults.
MAX_WALK_UP = 24


def package_root() -> Path:
    """The installed package directory -- the one `__file__` seam in the codebase.

    `Path(__file__).parent` rather than `importlib.resources.files("qaas")`,
    which was tried first and is wrong here: under a src-layout editable install
    the package resolves to a `MultiplexedPath`, and `Path(str(...))` on one of
    those yields the literal string `MultiplexedPath('/...')` -- a path that
    exists nowhere. Every resource lookup then silently found nothing, which is
    the same failure shape as the bug this module was written to fix.

    This file lives inside the package, so its parent *is* the package, in an
    editable install and a wheel alike. Zip-safety is not a consideration: the
    skills directory is handed to a subprocess as a real filesystem path.
    """
    return Path(__file__).resolve().parent


def packaged_config() -> Path:
    return package_root() / "defaults" / "config"


def packaged_prompts() -> Path:
    return package_root() / "prompts"


def packaged_skills() -> Path:
    return package_root() / "skills"


def find_project(start: Path | None = None) -> Path | None:
    """Walk up looking for a qaas project. Returns None if there is not one.

    Three shapes count:

      * `<dir>/.qaas/config/`      what `qaas init` writes
      * `<dir>/config/system.yaml` a user who keeps config at the top level
      * `<dir>/config/targets/`    this repository, whose `system.yaml` now
                                   ships inside the package and whose `config/`
                                   holds only the bundled demo profile

    The third looks incidental and is not: without it, developing qaas in its
    own checkout stops finding the demo target the moment the defaults move
    into the wheel.
    """
    here = (start or Path.cwd()).resolve()
    for parent in [here, *here.parents][:MAX_WALK_UP]:
        if (parent / STATE_DIRNAME / PROJECT_CONFIG).is_dir():
            return parent
        if (parent / PROJECT_CONFIG / "system.yaml").is_file():
            return parent
        if (parent / PROJECT_CONFIG / "targets").is_dir():
            return parent
    return None


def _existing(*candidates: Path | None) -> tuple[Path, ...]:
    """Keep the directories that exist, in order, without duplicates."""
    seen: list[Path] = []
    for c in candidates:
        if c is None:
            continue
        r = c.resolve()
        if r.is_dir() and r not in seen:
            seen.append(r)
    return tuple(seen)


@dataclass(frozen=True)
class Workspace:
    """Every directory qaas reads from, resolved once and passed down.

    Frozen because half the codebase reads these; a value that can be mutated
    mid-run is a value that will be.
    """

    project: Path | None
    config_dirs: tuple[Path, ...]
    prompt_dirs: tuple[Path, ...]
    skill_dirs: tuple[Path, ...]
    state_root: Path

    @classmethod
    def resolve(
        cls,
        *,
        config: Path | str | None = None,
        state_root: Path | str | None = None,
        cwd: Path | None = None,
    ) -> "Workspace":
        """Build the search paths. Never raises: a missing project is legitimate.

        `pip install qaas-python` then `qaas --help` in an empty directory is a
        supported state, and it resolves to packaged defaults only.
        """
        here = (cwd or Path.cwd()).resolve()
        project = find_project(here)

        explicit = config or os.environ.get(CONFIG_DIR_ENV) or None
        explicit_path = Path(explicit).resolve() if explicit else None

        home = os.environ.get(HOME_ENV)
        home_path = Path(home).resolve() if home else None

        proj_state = (project / STATE_DIRNAME) if project else None

        config_dirs = _existing(
            explicit_path,
            home_path / PROJECT_CONFIG if home_path else None,
            proj_state / PROJECT_CONFIG if proj_state else None,
            project / PROJECT_CONFIG if project else None,
            packaged_config(),
        )
        prompt_dirs = _existing(
            home_path / "prompts" if home_path else None,
            proj_state / "prompts" if proj_state else None,
            packaged_prompts(),
        )
        skill_dirs = _existing(
            home_path / "skills" if home_path else None,
            proj_state / "skills" if proj_state else None,
            # This repo's own skills live where Claude Code expects them.
            project / ".claude" / "skills" if project else None,
            packaged_skills(),
        )

        if state_root is not None:
            state = Path(state_root).resolve()
        elif proj_state is not None:
            state = proj_state
        else:
            state = (here / STATE_DIRNAME).resolve()

        return cls(
            project=project,
            config_dirs=config_dirs,
            prompt_dirs=prompt_dirs,
            skill_dirs=skill_dirs,
            state_root=state,
        )

    # -- lookups ----------------------------------------------------------

    def find_file(self, dirs: Iterable[Path], relative: str) -> Path | None:
        """First hit wins. Used for whole-file resources like `system.yaml`."""
        for d in dirs:
            candidate = d / relative
            if candidate.is_file():
                return candidate
        return None

    def config_file(self, relative: str) -> Path | None:
        return self.find_file(self.config_dirs, relative)

    def prompt_file(self, relative: str) -> Path | None:
        return self.find_file(self.prompt_dirs, relative)

    def merged(self, dirs: Iterable[Path], subdir: str, pattern: str) -> dict[str, Path]:
        """Union by filename, earlier layers shadowing later ones.

        For `agents/*.yaml`, `targets/*.yaml` and skills: a user shadows the one
        file they care about and keeps receiving improvements to the rest.
        Reversed iteration so the highest-precedence directory writes last.
        """
        found: dict[str, Path] = {}
        for d in reversed(list(dirs)):
            base = d / subdir if subdir else d
            if not base.is_dir():
                continue
            for path in sorted(base.glob(pattern)):
                found[path.stem] = path
        return found

    def skill_names(self) -> dict[str, Path]:
        """Every skill directory visible, by name, nearest layer winning."""
        found: dict[str, Path] = {}
        for d in reversed(list(self.skill_dirs)):
            if not d.is_dir():
                continue
            for skill in sorted(d.glob("*/SKILL.md")):
                found[skill.parent.name] = skill.parent
        return found

    def describe(self) -> str:
        """One line per search path, for `qaas doctor` and error messages."""
        where = str(self.project) if self.project else "(none -- packaged defaults only)"
        lines = [f"project:  {where}", f"state:    {self.state_root}"]
        for label, dirs in (
            ("config", self.config_dirs),
            ("prompts", self.prompt_dirs),
            ("skills", self.skill_dirs),
        ):
            for i, d in enumerate(dirs):
                lines.append(f"{label + ':':9} {'*' if i == 0 else ' '} {d}")
        return "\n".join(lines)
