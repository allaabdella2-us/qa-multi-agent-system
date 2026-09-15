"""The configuration read model: what this installation is actually set to.

The runs half of the dashboard answers "what happened". This half answers "what
would happen", which is a different question with the same audience — the
commands that answer it today are `qaas validate`, `qaas prompts list`, `qaas
doctor` and `qaas run --dry-run`, and every one of them prints a slice of the
same object graph into a different terminal table.

Two rules this module inherits from `state.py` and must keep:

* **It imports no web dependency.** That is what lets it be tested in the
  default offline suite without the `[ui]` extra installed.
* **It reads; it never participates.** Nothing here writes a file, resolves a
  credential, or constructs an agent. A value that comes from the environment
  is reported as *whether it is set*, never as what it is — the page is served
  over loopback but it is still a page, and a Jira token rendered into HTML is
  a token in a browser cache.

The layering rules it reports are the ones in `paths.py`, and they are not
uniform: `system.yaml` is first-hit-wins *whole*, while agents, prompts and
skills union by name with the nearer layer shadowing. Showing which file won
is the point — "why is FIXER on opus" is answered by a path, not by a value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qaas.config import AgentSpec, SystemConfig
from qaas.paths import Workspace

#: Which environment variables each tracker backend reads. Named here rather
#: than imported from the adapters because this module reports *presence* and
#: must not import a module that might construct a client on import.
TRACKER_ENV: dict[str, tuple[str, ...]] = {
    "jira": ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
             "JIRA_SECURITY_PROJECT_KEY"),
    "local": (),
}

#: The three hook events `registry.build_hooks` wires, and what each one is for.
#: This is documentation of enforcement, so it is stated once, here, rather than
#: inferred from a live SDK object the page has no business constructing.
HOOKS: tuple[dict[str, str], ...] = (
    {
        "event": "PreToolUse",
        "handlers": "Guardrail.pre_tool_use, turn recorder",
        "blocking": "yes",
        "what": "The §8.1 write-permission matrix. Checks the tool's path, branch "
                "or shell command against this agent's policy and returns a "
                "reason when it refuses. Primary enforcement: an allowlist entry "
                "naming a whole tool auto-approves it before can_use_tool is "
                "consulted, so a policy implemented only there is dead code.",
    },
    {
        "event": "PostToolUse",
        "handlers": "held-envelope notice, must_call recorder",
        "blocking": "no",
        "what": "Tells an agent immediately when an envelope it emitted was held "
                "rather than filed, while it still has turns to attach evidence. "
                "Also records which must_call tools succeeded — a denied or "
                "errored call never reaches here, which is exactly the "
                "distinction that matters.",
    },
    {
        "event": "Stop",
        "handlers": "output-contract check",
        "blocking": "once",
        "what": "Blocks an agent trying to stop without having called its "
                "must_call tools, while it still has a turn to fix it. Honours "
                "stop_hook_active: blocking twice burns the budget.",
    },
)


def _rel(path: Path | None, roots: list[Path]) -> str | None:
    """A path shown against the nearest root that contains it.

    An absolute path to a packaged prompt is forty characters of virtualenv
    before the part that identifies it.
    """
    if path is None:
        return None
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


@dataclass
class Source:
    """Where a value came from, and whether something nearer shadows it."""

    path: str | None
    layer: str                 # project | state | packaged | builtin | env | default
    shadows: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "layer": self.layer, "shadows": self.shadows}


def _layer_of(path: Path, dirs: list[Path]) -> str:
    """Name the config layer a file was found in.

    `paths.py` orders the search nearest-first: an explicit `--config`, then the
    project's `.qaas/config` and `config`, then the packaged defaults.
    """
    for index, directory in enumerate(dirs):
        try:
            path.relative_to(directory)
        except ValueError:
            continue
        if index == len(dirs) - 1:
            return "packaged"
        return "project" if ".qaas" not in directory.parts else "state"
    return "packaged"


class ConfigView:
    """One snapshot of everything `qaas` is configured to do.

    Built once per request rather than cached: a dashboard left open while
    someone edits `system.yaml` should show the edit on reload, and the whole
    thing is a few hundred small dictionaries.
    """

    def __init__(self, cfg: SystemConfig | None, workspace: Workspace | None = None) -> None:
        self.cfg = cfg
        self.ws = workspace or Workspace.resolve()

    # -- sections ---------------------------------------------------------

    def agents(self) -> list[dict[str, Any]]:
        if self.cfg is None:
            return []
        return [self._agent(spec) for spec in self.cfg.agents.values()]

    def _agent(self, spec: AgentSpec) -> dict[str, Any]:
        prompt = self.ws.prompt_file(spec.prompt)
        append = self.ws.prompt_file(spec.prompt.replace(".md", ".append.md"))
        policy = spec.policy
        return {
            "name": spec.name,
            "layer": spec.layer,
            "role": spec.role.strip(),
            "enabled": spec.enabled,
            "model": spec.model,
            "effort": spec.effort,
            "max_turns": spec.max_turns,
            "max_budget_usd": spec.max_budget_usd,
            "mcp_servers": list(spec.mcp_servers),
            "builtin_tools": list(spec.builtin_tools),
            "skills": list(spec.skills),
            "must_call": list(spec.must_call),
            "prompt": {
                "name": spec.prompt,
                "source": self._prompt_source(prompt).to_json(),
                "append": _rel(append, self.ws.prompt_dirs) if append else None,
                "chars": prompt.stat().st_size if prompt and prompt.exists() else 0,
            },
            "policy": {
                "read_only": policy.read_only,
                "write_paths": list(policy.write_paths),
                "branch_patterns": list(policy.branch_patterns),
                "protected_paths": list(policy.protected_paths),
                "forbidden_paths": list(policy.forbidden_paths),
                "may_open_pr": policy.may_open_pr,
                "may_create_tickets": policy.may_create_tickets,
                "may_transition_tickets": policy.may_transition_tickets,
                "max_diff_files": policy.max_diff_files,
                "max_diff_lines": policy.max_diff_lines,
            },
            "source": self._agent_source(spec.name).to_json(),
        }

    def _agent_source(self, name: str) -> Source:
        """Which `agents/<name>.yaml` won, and which ones it shadows.

        Agents union by name with the nearer layer shadowing, so more than one
        file of this name can exist and only the first is in force. Saying which
        is what makes "I edited the YAML and nothing changed" a two-second
        answer instead of a bug report.
        """
        found: list[Path] = []
        for directory in self.ws.config_dirs:
            candidate = directory / "agents" / f"{name.lower()}.yaml"
            if candidate.is_file():
                found.append(candidate)
        if not found:
            return Source(path=None, layer="packaged")
        return Source(
            path=_rel(found[0], self.ws.config_dirs),
            layer=_layer_of(found[0], self.ws.config_dirs),
            shadows=[_rel(p, self.ws.config_dirs) or "" for p in found[1:]],
        )

    def _prompt_source(self, path: Path | None) -> Source:
        if path is None:
            return Source(path=None, layer="packaged")
        packaged = self.ws.prompt_dirs[-1] if self.ws.prompt_dirs else None
        is_packaged = packaged is not None and str(path).startswith(str(packaged))
        return Source(
            path=_rel(path, self.ws.prompt_dirs),
            layer="packaged" if is_packaged else "project",
        )

    def prompts(self) -> list[dict[str, Any]]:
        """Every prompt in force, plus the shared block appended to all of them.

        `_shared.md` resolves independently of the agent block, which is the
        point of the design: overriding `API.md` keeps the house rules.
        """
        rows: list[dict[str, Any]] = []
        shared = self.ws.prompt_file("_shared.md")
        if shared:
            rows.append({
                "name": "_shared.md",
                "agent": None,
                "role": "Appended to every agent prompt, and always last — house "
                        "rules must stay the last word.",
                "chars": shared.stat().st_size,
                "source": self._prompt_source(shared).to_json(),
                "append": None,
            })
        for spec in (self.cfg.agents.values() if self.cfg else []):
            path = self.ws.prompt_file(spec.prompt)
            append = self.ws.prompt_file(spec.prompt.replace(".md", ".append.md"))
            rows.append({
                "name": spec.prompt,
                "agent": spec.name,
                "role": spec.role.strip(),
                "chars": path.stat().st_size if path and path.exists() else 0,
                "source": self._prompt_source(path).to_json(),
                "append": _rel(append, self.ws.prompt_dirs) if append else None,
            })
        return rows

    def mcp_servers(self) -> list[dict[str, Any]]:
        """Builtin, stdio and project-declared servers, with who may use each.

        Declaring a server grants nothing: an agent receives it only by naming
        it. The `used_by` list is that fact, made visible — a declared server
        nobody names is almost always a typo in an agent's list.
        """
        from qaas.registry import SDK_SERVER_MODULES, STDIO_SERVERS

        users: dict[str, list[str]] = {}
        for spec in (self.cfg.agents.values() if self.cfg else []):
            for server in spec.mcp_servers:
                users.setdefault(server, []).append(spec.name)

        rows: list[dict[str, Any]] = []
        for name, module in sorted(SDK_SERVER_MODULES.items()):
            rows.append({
                "name": name, "kind": "in-process", "runs": module,
                "used_by": sorted(users.get(name, [])), "declared": False,
            })
        for name, entry in sorted(STDIO_SERVERS.items()):
            command = " ".join([entry.get("command", "")] + list(entry.get("args") or []))
            rows.append({
                "name": name, "kind": "stdio", "runs": command.strip(),
                "used_by": sorted(users.get(name, [])), "declared": False,
            })

        for name, entry in sorted((self.cfg.mcp_servers if self.cfg else {}).items()):
            data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
            kind = str(data.get("type") or "stdio")
            runs = data.get("url") or " ".join(
                [str(data.get("command") or "")] + [str(a) for a in (data.get("args") or [])]
            )
            rows.append({
                "name": name, "kind": kind, "runs": runs.strip(),
                "used_by": sorted(users.get(name, [])), "declared": True,
                # Values are never rendered: a declared server's env block holds
                # `${ACME_TOKEN}` references that expand to real credentials.
                "env": sorted((data.get("env") or {}).keys()),
            })
        return rows

    def models(self) -> list[dict[str, Any]]:
        """Model choice per agent, grouped by the model rather than the agent.

        Model is per-agent config, and the roadmap's question — "which model is
        actually best at this agent's job" — starts by being able to see the
        current answer in one place.
        """
        by_model: dict[str, list[dict[str, Any]]] = {}
        for spec in (self.cfg.agents.values() if self.cfg else []):
            by_model.setdefault(spec.model, []).append({
                "name": spec.name, "layer": spec.layer, "effort": spec.effort,
                "max_turns": spec.max_turns, "max_budget_usd": spec.max_budget_usd,
            })
        return [
            {"model": model, "agents": sorted(rows, key=lambda r: r["name"]),
             "count": len(rows)}
            for model, rows in sorted(by_model.items())
        ]

    def skills(self) -> list[dict[str, Any]]:
        """Procedure, as it reaches an agent: a plugin skill, qualified.

        A skill no plugin provides is dropped rather than passed through, and
        the unqualified name loads on one SDK channel but never matches the
        allow rule on the other — which is why `qualified` is shown.
        """
        used: dict[str, list[str]] = {}
        for spec in (self.cfg.agents.values() if self.cfg else []):
            for skill in spec.skills:
                used.setdefault(skill, []).append(spec.name)

        rows: list[dict[str, Any]] = []
        for name, path in sorted(self.ws.skill_names().items()):
            # `skill_names` maps to the skill *directory*; the procedure is the
            # SKILL.md inside it, which is what carries the frontmatter and the
            # size worth reporting.
            document = path / "SKILL.md" if path.is_dir() else path
            rows.append({
                "name": name,
                "qualified": self.ws.qualify(name),
                "path": _rel(document, self.ws.skill_dirs),
                "chars": document.stat().st_size if document.exists() else 0,
                "summary": _frontmatter_description(document),
                "used_by": sorted(used.get(name, [])),
            })
        return rows

    def hooks(self) -> list[dict[str, str]]:
        return [dict(hook) for hook in HOOKS]

    def run_modes(self) -> list[dict[str, Any]]:
        rows = []
        for name, mode in (self.cfg.run_modes.items() if self.cfg else []):
            rows.append({
                "name": name,
                "trigger": mode.trigger,
                "agents": list(mode.agents),
                "max_budget_usd": mode.max_budget_usd,
                "max_wall_clock_s": mode.max_wall_clock_s,
                "max_concurrency": mode.max_concurrency,
                "files_tickets": mode.files_tickets,
            })
        return rows

    def thresholds(self) -> list[dict[str, Any]]:
        """The governors, each with the sentence explaining what it costs.

        A number with no explanation invites being raised. These are the loop
        breakers and the spend levers, so each carries why it exists.
        """
        if self.cfg is None:
            return []
        t = self.cfg.thresholds
        notes = {
            "min_confidence_to_file":
                "A finding below this is recorded but held rather than filed. "
                "Precision lever: raising it files fewer, surer defects.",
            "max_findings_per_agent_run":
                "§8.3 loop breaker. An agent past this pauses and escalates "
                "instead of filing — a run that finds 85 issues has usually "
                "found one issue 85 times.",
            "reproduce_min_severity":
                "Reproduction runs once per finding in a fresh context, so its "
                "cost scales with findings rather than agents. Below this floor "
                "a finding is still filed on discovery's evidence; it just does "
                "not get a committed failing test.",
            "max_tickets_per_run":
                "§4.12 rate limit. The cap takes the most severe findings "
                "first, so what it drops is the tail.",
            "flake_runs":
                "How many times REPRODUCER runs a reproduction to measure "
                "flake. A test that passes sometimes has not reproduced "
                "anything.",
            "max_mender_arbiter_round_trips":
                "How many FIXER → REVIEWER rounds one ticket gets before a "
                "human is asked.",
            "max_proof_reopens":
                "How many times VERIFIER may reopen a ticket before escalating. "
                "Without it a fix that keeps missing cycles until the budget is "
                "gone, and the run ends with no verdict and no money left.",
        }
        rows = []
        for key, value in t.model_dump().items():
            rows.append({
                "name": key,
                "value": value.value if hasattr(value, "value") else value,
                "note": notes.get(key, ""),
            })
        return rows

    def target(self) -> dict[str, Any] | None:
        """The active target profile. Credentials are named, never read."""
        profile = self.cfg.profile if self.cfg else None
        if profile is None:
            return {"name": self.cfg.target if self.cfg else None, "loaded": False}
        environment = profile.environment
        auth = profile.auth
        roles = []
        for role_name, role in (auth.roles or {}).items():
            variable = getattr(role, "password_env", None)
            roles.append({
                "role": role_name,
                "username": getattr(role, "username", None),
                # Presence, never the value. A profile names an environment
                # variable precisely so the secret stays out of the file, and
                # rendering it here would put it back.
                "password_env": variable,
                "set": bool(variable and os.environ.get(variable)),
            })
        return {
            "name": profile.name,
            "loaded": True,
            "description": profile.description,
            "root": str(profile.root_path()),
            "default_branch": profile.default_branch,
            "environment_mode": environment.mode,
            "web_url": environment.web_url,
            "api_url": environment.api_url,
            "capabilities": profile.capabilities(),
            "auth_mode": auth.mode,
            "roles": roles,
        }

    def settings(self) -> dict[str, Any]:
        """Project-level wiring: which tracker, which vcs, which env vars are set."""
        tracker = str(self.cfg.tracker if self.cfg else "local")
        override = os.environ.get("QAAS_TRACKER")
        return {
            "project": self.cfg.project if self.cfg else None,
            "tracker": tracker,
            "tracker_override": override,
            "vcs": str(self.cfg.vcs if self.cfg else ""),
            "state_root": str(self.ws.state_root),
            "config_dirs": [
                {"path": str(directory), "exists": directory.is_dir(),
                 "layer": _layer_of(directory, self.ws.config_dirs)}
                for directory in self.ws.config_dirs
            ],
            "prompt_dirs": [str(p) for p in self.ws.prompt_dirs],
            "plugin_dirs": [str(p) for p in self.ws.plugin_dirs],
            # Only ever whether a variable is set. `qaas tracker-check` is the
            # command that validates the values, and it does so without
            # printing them either.
            "env": [
                {"name": name, "set": bool(os.environ.get(name))}
                for name in TRACKER_ENV.get(tracker, ()) or TRACKER_ENV["jira"]
            ],
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "loaded": self.cfg is not None,
            "agents": self.agents(),
            "prompts": self.prompts(),
            "mcp_servers": self.mcp_servers(),
            "models": self.models(),
            "skills": self.skills(),
            "hooks": self.hooks(),
            "run_modes": self.run_modes(),
            "thresholds": self.thresholds(),
            "target": self.target(),
            "settings": self.settings(),
        }


def _frontmatter_description(path: Path) -> str:
    """A skill's one-line description out of its YAML frontmatter.

    Parsed by hand rather than with yaml.safe_load: the body below the
    frontmatter is Markdown that routinely contains things a YAML parser would
    choke on, and only two scalar fields are wanted.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    lines = text[3:end].splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        # A folded scalar (`description: >`) carries its text on the indented
        # lines below, which is how most of these are written.
        if value in (">", "|", ">-", "|-", ""):
            body = []
            for following in lines[index + 1:]:
                if following.strip() and not following.startswith((" ", "\t")):
                    break
                body.append(following.strip())
            value = " ".join(part for part in body if part)
        return value.strip().strip("'\"")
    return ""
