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
    path = Path(config_dir) / OVERRIDES_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _merge(current: dict[str, Any], section: str, key: str | None,
           values: dict[str, Any]) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in current.items()}
    if section == "agents":
        agents = {k: dict(v) for k, v in (out.get("agents") or {}).items()}
        merged = dict(agents.get(key or "", {}))
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
        thresholds = dict(out.get("thresholds") or {})
        for field, value in values.items():
            if value is None:
                thresholds.pop(field, None)
            else:
                thresholds[field] = value
        out["thresholds"] = thresholds
        if not thresholds:
            out.pop("thresholds")
    return out


def _validate(config_dirs: list[Path], candidate: dict[str, Any]) -> None:
    """Load the whole config with the candidate in place, in a scratch copy.

    Writing first and validating afterwards would leave a project whose every
    command fails until someone finds the file. The cost of copying is a few
    small YAML files.
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
            load_config(search=staged)
        except Exception as exc:                       # the loader's own message
            raise OverrideError(str(exc)) from exc


def set_values(
    config_dirs: list[Path],
    *,
    section: str,
    key: str | None,
    values: dict[str, Any],
    write_to: Path | None = None,
) -> dict[str, Any]:
    """Apply one change and persist it. Returns the file's new contents.

    `values` maps a field to its new value, or to None to drop the override and
    return that field to whatever the packaged config says.
    """
    if section not in ("agents", "thresholds"):
        raise OverrideError(f"'{section}' is not an overridable section")
    if section == "agents":
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

    target = Path(write_to or config_dirs[0])
    candidate = _merge(read(target), section, key, values)
    _validate(config_dirs, candidate)

    target.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(candidate, sort_keys=True) if candidate else ""
    (target / OVERRIDES_FILE).write_text(HEADER + body, encoding="utf-8")
    return candidate


def reset(config_dirs: list[Path], *, write_to: Path | None = None) -> dict[str, Any]:
    """Delete every override. One `rm`, exposed as a button."""
    target = Path(write_to or config_dirs[0])
    path = target / OVERRIDES_FILE
    if path.is_file():
        path.unlink()
    return {}
