"""Writing `overrides.yaml`: the one thing the dashboard may change.

The rest of `src/qaas/ui/` reads. This module is the deliberate exception, and
the shape of the exception is the point: it can change what a model *is*, never
what an agent is *allowed to do*.

- **Tuning, not permission.** `config.TUNABLE_AGENT_FIELDS` is the whole
  vocabulary — model, effort, turn cap, budget, enabled — plus thresholds. A
  policy, a tool list, an MCP server list and a `must_call` contract are not
  writable here at any price. Those are what `guardrails.py` enforces, and a
  localhost page able to widen `write_paths` would be a second, quieter door
  onto the write-permission matrix. Anyone who can reach the port could open it.
- **Validated before it lands.** The candidate file is merged into a real
  `load_config` in a scratch directory first. A value that would not load is
  refused with the loader's own message rather than written and discovered on
  the next run, when it costs a dispatch.
- **One file, reversible.** Everything goes to `<config>/overrides.yaml`, so
  "what did I change" is one `cat` and reverting is one `rm`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from qaas.config import (
    OVERRIDES_FILE,
    TUNABLE_AGENT_FIELDS,
    load_config,
)

HEADER = """\
# Written by `qaas dashboard`. Safe to edit by hand, and safe to delete: with
# this file gone every value returns to what the packaged config and any
# `agents/*.yaml` override say.
#
# Only tuning lives here. An agent's policy, tools, MCP servers and must_call
# contract are NOT read from this file however they are written -- the
# write-permission matrix is enforced in code, and this layer cannot widen it.
"""


class OverrideError(ValueError):
    """A change that cannot be applied, with a sentence saying why."""


def read(config_dir: Path) -> dict[str, Any]:
    """The overrides file in one layer, or {} when there is none.

    Raises `OverrideError` for a file that is there and cannot be used. It is
    "safe to edit by hand", so a hand edit that does not parse is an ordinary
    state -- and it raised `yaml.YAMLError` straight out of the save, a 500 on
    every change until someone found the file. A file that parses to something
    other than a mapping used to read as {}, and the next save then replaced
    whatever the person had written with a single field.
    """
    path = Path(config_dir) / OVERRIDES_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise OverrideError(
            f"{path} does not parse ({_first_line(exc)}). Fix it or delete it; "
            "nothing was changed."
        ) from exc
    except OSError as exc:
        raise OverrideError(f"{path} cannot be read: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise OverrideError(
            f"{path} holds a {type(data).__name__}, not a mapping of sections. "
            "Fix it or delete it; nothing was changed."
        )
    return data


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _mapping(value: Any, where: str) -> dict[str, Any]:
    """A section of the file that must be a mapping. `FIXER:` with no value is one.

    A hand edit that leaves `FIXER:` (or `thresholds:`) with nothing under it is
    YAML for null, and `dict(None)` made every later save a 500 -- for every
    agent, not just the one left empty. Null reads as empty, which is what the
    loader already does with it; anything else that is not a mapping is refused
    with its location, because merging into it would overwrite what someone
    wrote there.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise OverrideError(
            f"{OVERRIDES_FILE}: {where} is a {type(value).__name__}, expected a "
            "mapping of field: value. Fix or delete the file; nothing was changed."
        )
    return dict(value)


def _merge(current: dict[str, Any], section: str, key: str | None,
           values: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
    if section == "agents":
        # Other agents' entries are carried as they are, not `dict()`-ed: one
        # left empty by hand is not this change's business and must not fail it.
        agents = {
            k: dict(v) if isinstance(v, dict) else v
            for k, v in _mapping(out.get("agents"), "agents").items()
        }
        merged = _mapping(agents.get(key or ""), f"agents.{key}")
        for field, value in values.items():
            if value is None:
                merged.pop(field, None)      # "back to the packaged value"
            else:
                merged[field] = value
        if merged:
            agents[key or ""] = merged
        else:
            agents.pop(key or "", None)
        out["agents"] = agents
        if not agents:
            out.pop("agents")
    else:
        thresholds = _mapping(out.get("thresholds"), "thresholds")
        for field, value in values.items():
            if value is None:
                thresholds.pop(field, None)
            else:
                thresholds[field] = value
        out["thresholds"] = thresholds
        if not thresholds:
            out.pop("thresholds")
    return out


def _validate(config_dirs: list[Path], candidate: dict[str, Any]):
    """Load the whole config with the candidate in place, in a scratch copy.

    Writing first and validating afterwards would leave a project whose every
    command fails until someone finds the file. The cost of copying is a few
    small YAML files. Returns the loaded config, so a caller can ask it what
    exists.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged: list[Path] = []
        for index, directory in enumerate(config_dirs):
            if not Path(directory).is_dir():
                continue
            mirror = Path(tmp) / f"layer{index}"
            shutil.copytree(directory, mirror, symlinks=False, dirs_exist_ok=True)
            staged.append(mirror)
        if not staged:
            raise OverrideError("no config directory to validate against")
        (staged[0] / OVERRIDES_FILE).write_text(
            HEADER + yaml.safe_dump(candidate, sort_keys=True), encoding="utf-8"
        )
        try:
            return load_config(search=staged)
        except Exception as exc:                       # the loader's own message
            raise OverrideError(str(exc)) from exc


def set_values(
    config_dirs: list[Path],
    *,
    section: str,
    key: str | None,
    values: dict[str, Any],
    write_to: Path | None = None,
    state_root: Path | str | None = None,
) -> dict[str, Any]:
    """Apply one change and persist it. Returns the file's new contents.

    `values` maps a field to its new value, or to None to drop the override and
    return that field to whatever the packaged config says.

    Everything that arrives here came out of a JSON body, so its *shape* is
    checked before anything else is: `values: "ab"` reached `dict()` as a
    sequence and was a 500, and `agent: []` reached a dict lookup unhashable.
    A refusal is an `OverrideError`, which the route turns into a 400 with the
    reason in it.
    """
    if not isinstance(section, str) or section not in ("agents", "thresholds"):
        raise OverrideError(f"'{section}' is not an overridable section")
    if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
        raise OverrideError("values must be a JSON object mapping a field name to its value")
    if section == "agents":
        if key is not None and not isinstance(key, str):
            raise OverrideError("the agent must be named by a string")
        if not key:
            raise OverrideError("an agent override needs an agent name")
        rejected = sorted(set(values) - TUNABLE_AGENT_FIELDS)
        if rejected:
            # Named rather than silently dropped: someone trying to widen a
            # policy from here should be told it is not a thing this door does.
            raise OverrideError(
                f"{', '.join(rejected)} cannot be set from the dashboard. Only "
                f"{', '.join(sorted(TUNABLE_AGENT_FIELDS))} are tunable — an "
                "agent's policy, tools, servers and must_call contract are what "
                "the guardrails enforce, and they are edited in the config file."
            )

    target = Path(write_to) if write_to else override_layer(config_dirs, state_root=state_root)
    search = _with_layer(config_dirs, target)
    candidate = _merge(_current(search, target), section, key, values)
    loaded = _validate(search, candidate)

    if section == "agents" and key not in loaded.agents:
        # `_apply_overrides` skips a name it does not know, so a typo -- or
        # `fixer` for `FIXER` -- validated, was written, reported success, and
        # changed nothing a run would ever read. Dropping a stale entry for an
        # agent that has since left the roster is still allowed: that is a
        # removal, and refusing it would leave the entry stuck in the file.
        if any(value is not None for value in values.values()):
            raise OverrideError(
                f"there is no agent named '{key}' in this configuration, so an "
                "override for it would be written and never read. Known agents: "
                f"{', '.join(sorted(loaded.agents)) or 'none'}."
            )

    target.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(candidate, sort_keys=True) if candidate else ""
    _write_atomic(target / OVERRIDES_FILE, HEADER + body)
    return candidate


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory and `os.replace`.

    `write_text` truncates first and writes second, so a process killed in
    between -- or a disk that filled -- left an empty or half-written
    `overrides.yaml`, and every command that loads a config reads it. The
    replace is atomic on one filesystem, so a reader sees the old file or the
    new one and never a torn one.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # `mkstemp` creates 0600. Keep the mode the file already had, so a
        # replace does not quietly change who may read the project's config.
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_packaged(directory: Path | str) -> bool:
    from qaas.paths import packaged_config

    try:
        return Path(directory).resolve() == packaged_config().resolve()
    except (OSError, RuntimeError):
        return False


def _same(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, RuntimeError):
        return str(a) == str(b)


def _with_layer(config_dirs: list[Path], layer: Path) -> list[Path]:
    """The search path with `layer` in it, nearest, when it is not already.

    The only layer that can be missing is the `<state_root>/config` fallback,
    and it is only chosen when nothing but the packaged defaults is on the path
    -- so nearest is exactly where `Workspace.resolve` puts `.qaas/config`.
    """
    dirs = [Path(d) for d in config_dirs]
    return dirs if any(_same(d, layer) for d in dirs) else [Path(layer), *dirs]


def effective_dirs(config_dirs: list[Path], *, state_root: Path | str | None = None) -> list[Path]:
    """The search path an override will be validated and read back against.

    The route replaces its own list with this after a write, so its reload, and
    `/api/config`, see the layer the file was just created in. A fallback layer
    that does not exist yet (a reset before any write) is not added.
    """
    layer = override_layer(config_dirs, state_root=state_root)
    if not layer.is_dir():
        return [Path(d) for d in config_dirs]
    return _with_layer(config_dirs, layer)


def _current(search: list[Path], target: Path) -> dict[str, Any]:
    """The overrides in force, which are what a change is merged into.

    Normally the target's own file. The exception is a file in the packaged
    layer -- written there by the bug `override_layer` describes -- which the
    loader reads while nothing nearer has one. The first write now lands
    nearer and shadows it, so it is carried over rather than discarded.
    """
    for directory in search:
        if (Path(directory) / OVERRIDES_FILE).is_file():
            if _same(directory, target) or _is_packaged(directory):
                return read(Path(directory))
            break
    return read(target)


def override_layer(config_dirs: list[Path], *, state_root: Path | str | None = None) -> Path:
    """The layer `config._apply_overrides` will actually read back.

    That loader takes the *nearest layer which already contains* an
    `overrides.yaml` and stops there; this wrote unconditionally to
    `config_dirs[0]`. When the live overrides file sat in a farther layer --
    `<project>/config/overrides.yaml`, with a nearer `.qaas/config/` on the path
    -- the page read an empty file, merged one field into it, wrote it to the
    nearer directory, and reported success. Every override already in force was
    silently discarded, and the page then shadowed the file it had discarded.

    Falls back to the nearest writable layer when no overrides file exists yet,
    which is what creating the first one should do.

    **Never the packaged defaults.** With no `.qaas/config` on the path the
    nearest layer *is* `site-packages/qaas/defaults/config`, and this wrote
    there: a PermissionError and a 500 on a read-only install, and in a source
    checkout a file under `src/` that the next wheel would have shipped to
    everyone. The fallback is `<state_root>/config`, created on first write --
    the directory `qaas init` makes and `Workspace.resolve` reads back.
    """
    for directory in config_dirs:
        if _is_packaged(directory):
            continue
        if (Path(directory) / OVERRIDES_FILE).is_file():
            return Path(directory)
    for directory in config_dirs:
        if not _is_packaged(directory):
            return Path(directory)
    if state_root is None:
        from qaas.paths import Workspace

        state_root = Workspace.resolve().state_root
    return Path(state_root).resolve() / "config"


#: The old private name, kept so nothing importing it breaks.
_override_layer = override_layer


def reset(
    config_dirs: list[Path],
    *,
    write_to: Path | None = None,
    state_root: Path | str | None = None,
) -> dict[str, Any]:
    """Delete every override. One `rm`, exposed as a button."""
    target = Path(write_to) if write_to else override_layer(config_dirs, state_root=state_root)
    path = target / OVERRIDES_FILE
    if path.is_file():
        path.unlink()
    return {}
