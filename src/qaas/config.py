"""Configuration: agents are data, not code.

An agent is a prompt file plus an entry in `config/agents/`. Adding one of the
remaining agents from the roster should never require touching the router,
the runner, or the guardrails — that is the property this module exists to keep.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Sequence

import os

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qaas.envelope import Severity
from qaas.target import TargetProfile, load_target

# §5.3 is explicit that no agent gets more than six MCP servers, because tool
# selection accuracy falls off past roughly 5-7. Enforced, not just documented.
MAX_MCP_SERVERS_PER_AGENT = 6

Layer = Literal["control", "discovery", "synthesis", "triage", "remediation", "reporting"]


class Policy(BaseModel):
    """One agent's slice of the §8.1 write-permission matrix.

    Default is read-only. Anything an agent may write, it says so here, and
    guardrails.py enforces it against the actual tool call arguments.
    """

    model_config = ConfigDict(extra="forbid")

    write_paths: list[str] = Field(default_factory=list)
    branch_patterns: list[str] = Field(default_factory=list)
    may_open_pr: bool = False
    may_create_tickets: bool = False
    may_transition_tickets: bool = False
    max_tickets_per_run: int = 0
    max_diff_files: int | None = None
    max_diff_lines: int | None = None
    protected_paths: list[str] = Field(default_factory=list)

    #: Paths an agent may write without spending its §8.2 diff budget.
    #:
    #: The budget bounds how much *production code* one agent may change on its
    #: own authority. A scratch harness is not production code and is not part
    #: of the fix, and counting it meant FIXER never reached the fix at all: it
    #: writes a probe project to investigate a defect -- package.json,
    #: vitest.config.ts, .gitignore, README.md, probe.test.ts -- which is
    #: exactly five files against a limit of five. Across one full-loop run,
    #: 115 of the 115 files that consumed FIXER's budget were under `qa/repro`
    #: and none were product code, and all seven tickets escalated with "there
    #: is no fix to review".
    #:
    #: Safe because nothing in the application imports this directory, and
    #: REVIEWER diffs the product paths specifically -- it caught a
    #: scaffolding-only branch unaided.
    scratch_paths: list[str] = Field(default_factory=list)

    #: Path globs this agent may never modify, whatever else its policy allows.
    #: §8.2 names the classes: migrations, auth, payment paths and infra config.
    #: These are the changes whose blast radius a review cannot reliably bound,
    #: so they stop at a human even when everything else in the envelope holds.
    forbidden_paths: list[str] = Field(default_factory=list)

    @property
    def read_only(self) -> bool:
        return not (
            self.write_paths
            or self.branch_patterns
            or self.may_open_pr
            or self.may_create_tickets
            or self.may_transition_tickets
        )


class AgentSpec(BaseModel):
    """Everything needed to build one agent's ClaudeAgentOptions."""

    model_config = ConfigDict(extra="forbid")

    name: str
    layer: Layer
    role: str
    prompt: str  # path relative to src/qaas/prompts/
    enabled: bool = True

    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_turns: int = 40
    max_budget_usd: float | None = None


    mcp_servers: list[str] = Field(default_factory=list)
    builtin_tools: list[str] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    # Procedure lives in skills, role and standards live in the prompt. A skill
    # named here is preloaded; the agent can still reach others through Skill.
    skills: list[str] = Field(default_factory=list)

    # Tools this agent must have called before it is allowed to finish. The Stop
    # hook enforces it. Without this an agent can produce a confident summary and
    # no artifact, and the failure only surfaces afterwards in the router —
    # too late for the agent to fix it.
    must_call: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tool_budget(self) -> "AgentSpec":
        if len(self.mcp_servers) > MAX_MCP_SERVERS_PER_AGENT:
            raise ValueError(
                f"{self.name} declares {len(self.mcp_servers)} MCP servers; "
                f"the cap is {MAX_MCP_SERVERS_PER_AGENT} (§5.3). "
                "An agent needing more is a signal to split it."
            )
        if len(set(self.mcp_servers)) != len(self.mcp_servers):
            raise ValueError(f"{self.name} lists a duplicate MCP server")
        for tool in self.must_call:
            server = tool.split("__")[1] if tool.startswith("mcp__") else None
            if server and server not in self.mcp_servers:
                raise ValueError(
                    f"{self.name} must_call names '{tool}' but is not connected to "
                    f"the '{server}' server; it could never satisfy that."
                )
        return self

    def prompt_path(self, prompts_dir: Path) -> Path:
        return prompts_dir / self.prompt


class StdioServerSpec(BaseModel):
    """A user-declared MCP server run as a subprocess.

    Pure data: `command` and `args` are passed to the CLI, which spawns it. No
    shell, ever -- `command` is a program and `args` is a list, so a string like
    `"foo && rm -rf /"` is a program name that does not exist rather than two
    commands.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class UrlServerSpec(BaseModel):
    """A user-declared MCP server reached over HTTP or SSE."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["http", "sse"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


#: What a user may declare. Deliberately no in-process Python type: that would
#: mean `importlib.import_module` on a name from a config file, executing
#: arbitrary module-level code inside the process holding this user's Anthropic
#: credentials, Jira token and GitHub auth. A subprocess is a subprocess; an
#: import is a foothold. If someone needs a Python server they can wrap it in a
#: stdio entry point and it costs them one line.
McpServerSpec = StdioServerSpec | UrlServerSpec


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_confidence_to_file: float = 0.6
    max_findings_per_agent_run: int = 25
    #: Findings less severe than this are filed on discovery's evidence instead
    #: of each earning a fresh REPRODUCER context. See `_phase_reproduce` for
    #: the run that made this necessary.
    reproduce_min_severity: Severity = Severity.MAJOR
    #: A §8.3 loop breaker, not a throttle. It was 10, which is lower than the
    #: number of real defects an ordinary repository holds -- so a run that
    #: found eighty filed ten and left seventy on disk, having already paid to
    #: find them. The enforced limit is `min(this, TRIAGE's policy cap)`, so
    #: both have to move; raising only one silently changes nothing.
    max_tickets_per_run: int = 100
    flake_runs: int = 5
    max_mender_arbiter_round_trips: int = 2
    max_proof_reopens: int = 1


class RunMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str
    agents: list[str]
    max_budget_usd: float | None = None

    max_wall_clock_s: int = 3600
    max_concurrency: int = 3
    files_tickets: bool = True

    #: How much of the clock the finding phases may NOT have, so that filing and
    #: reporting still can. `BudgetExceeded` unwinds all the way to `run()`, so a
    #: run that ran out of time during discovery or reproduce skipped file,
    #: verify and report entirely -- the envelopes sat on disk, no ticket existed,
    #: no report existed, and whoever scheduled it saw a run that cost money and
    #: produced nothing they could act on. `pr-check` is the likely victim at 900
    #: seconds. The reserve converts work already paid for into tickets instead of
    #: discarding it; the run still stops early and still escalates.
    reserve_fraction: float = 0.15


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = "qaas"

    #: Which target profile in config/targets/ this run is pointed at. The
    #: profile is what makes the system portable: without it every prompt and
    #: every environment call is welded to the application it was built beside.
    #: The active target profile, or None when nothing is configured yet.
    #: A fresh `pip install` is legitimately in that state; commands that
    #: need a profile say so rather than crashing during config load.
    target: str | None = None

    #: Servers this project declares, on top of the built-in ones. Declaring a
    #: server here grants nothing; an agent receives it only by naming it in its
    #: own `mcp_servers:` list.
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)

    #: Overridable with QAAS_TRACKER. Keep the committed value `local`.
    tracker: Literal["local", "jira"] = "local"
    vcs: Literal["local", "github"] = "local"
    thresholds: Thresholds = Field(default_factory=Thresholds)
    run_modes: dict[str, RunMode] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    profile: TargetProfile | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _agents_name_real_servers(self) -> "SystemConfig":
        """Every server an agent names must resolve to something.

        `AgentSpec` cannot check this -- it has no view of the rest of the
        config -- so an unresolvable name used to surface as `UnknownServer`
        part-way through a paid run. Here it is a load-time error, which is what
        `qaas validate` is for.

        Imported inside the function: `registry` imports `config`, so a
        module-level import would be a cycle.
        """
        from qaas.registry import SDK_SERVER_MODULES, STDIO_SERVERS

        builtin = set(SDK_SERVER_MODULES) | set(STDIO_SERVERS)
        known = builtin | set(self.mcp_servers)
        for name, spec in sorted(self.agents.items()):
            unknown = [s for s in spec.mcp_servers if s not in known]
            if unknown:
                raise ValueError(
                    f"{name} names MCP server(s) nothing provides: {', '.join(unknown)}. "
                    f"Built in: {', '.join(sorted(builtin))}. "
                    f"Declared in system.yaml: {', '.join(sorted(self.mcp_servers)) or 'none'}."
                )
        return self

    @model_validator(mode="after")
    def _modes_name_real_agents(self) -> "SystemConfig":
        for mode_name, mode in self.run_modes.items():
            unknown = [a for a in mode.agents if a not in self.agents]
            if unknown:
                raise ValueError(
                    f"run mode '{mode_name}' names unknown agents: {', '.join(unknown)}"
                )
        return self

    def enabled_agents(self, mode: str) -> list[AgentSpec]:
        """Agents for a run mode, skipping any that are switched off."""
        if mode not in self.run_modes:
            raise KeyError(f"unknown run mode '{mode}'; have: {', '.join(sorted(self.run_modes))}")
        return [self.agents[n] for n in self.run_modes[mode].agents if self.agents[n].enabled]

    def target_root(self, base: Path | None = None) -> Path:
        """Where the application under test lives.

        This used to be `Path.cwd() / config.target_app` -- one value serving as
        both "where qaas lives" and "the application under test". That holds
        only while the target is a subdirectory of the qaas checkout, which is
        true of exactly one target: the bundled demo. `qaas run --repo <url>`
        clones into `.qaas/targets/<slug>`, and every write-path allowlist,
        every test cwd and the SDK subprocess cwd are anchored on this value --
        so getting it from the profile is not tidying, it is the security
        boundary being pointed at the right directory.

        With no profile there is nothing to test; the base (the qaas project, or
        the cwd) is returned so read-only tooling still has somewhere to stand.
        """
        if self.profile is not None:
            return self.profile.root_path(base)
        from qaas.paths import project_root

        return base if base is not None else project_root()


#: Environment overrides for the two swappable backends.
TRACKER_ENV = "QAAS_TRACKER"
VCS_ENV = "QAAS_VCS"
#: Which target profile to run against. Useful on its own (`QAAS_TARGET=staging
#: qaas run`), and it is how this repo's own test suite selects the bundled demo
#: without putting a demo name in the defaults that ship to everyone else.
TARGET_ENV = "QAAS_TARGET"


#: What `overrides.yaml` may change on an agent. Tuning, never permission.
#: Anything touching the §8.1 write-permission matrix is absent on purpose:
#: `policy`, `mcp_servers`, `builtin_tools`, `skills` and `must_call` are what
#: bound an agent, and a partial layer able to widen them would be a quieter
#: second door onto the rules `guardrails.py` exists to enforce.
TUNABLE_AGENT_FIELDS = frozenset({
    "model", "effort", "max_turns", "max_budget_usd", "enabled",
})

#: What it may change globally. Thresholds are governors, so they are the other
#: thing worth tuning without editing a committed file.
TUNABLE_SECTIONS = frozenset({"agents", "thresholds"})

#: Where the dashboard and `qaas config set` write. One file, so "what did I
#: change" is one `cat`, and reverting is one `rm`.
OVERRIDES_FILE = "overrides.yaml"


def _apply_overrides(raw: dict[str, Any], dirs: Sequence[Path]) -> None:
    """Merge the nearest `overrides.yaml` into an already-layered config.

    Nearest wins and stops: unlike agents, these are not unioned across layers.
    Two override files disagreeing about FIXER's model is a state nobody can
    read back, and the file exists precisely to be the one place a local change
    lives.

    An unknown field is dropped rather than raising. The file is written by a
    UI and edited by hand afterwards, and a typo in it must not take down every
    command that loads a config -- `qaas validate` is where that gets reported.
    """
    path = next((d / OVERRIDES_FILE for d in dirs if (d / OVERRIDES_FILE).is_file()), None)
    if path is None:
        return
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return
    if not isinstance(data, dict):
        return

    for agent_name, fields in (data.get("agents") or {}).items():
        spec = raw["agents"].get(agent_name)
        if not isinstance(spec, dict) or not isinstance(fields, dict):
            continue
        for field, value in fields.items():
            if field in TUNABLE_AGENT_FIELDS:
                spec[field] = value

    thresholds = data.get("thresholds")
    if isinstance(thresholds, dict):
        current = dict(raw.get("thresholds") or {})
        current.update(thresholds)
        raw["thresholds"] = current


#: Layout sections an agent's paths may name instead of hard-coding a directory.
#:
#: `write_paths`, `protected_paths` and `forbidden_paths` are target-relative
#: globs, and the shipped roster's were the bundled demo's directories:
#: FIXER carried `[api/app, web/src, qa/repro]`. Pointed at a real repository
#: whose application lives under `build-battle/merchant-console/src`, two of
#: those three matched no file and the survivor was `qa/repro` -- REPRODUCER's
#: sandbox. FIXER ran, could reach no product code, committed nothing that
#: changed the defect, and reported success. REVIEWER caught it by reading the
#: diff; nothing else would have.
#:
#: The profile already answers "where is the code" for every other consumer, so
#: an agent should be able to say *which kind* of code it may touch and let the
#: target say where that is. `$backend` is portable in a way `api/app` can
#: never be.
LAYOUT_TOKENS = ("backend", "frontend", "tests", "migrations", "docs")

#: Policy fields whose entries may name a layout section.
_PATH_FIELDS = ("write_paths", "protected_paths", "forbidden_paths")


def expand_layout_tokens(paths: Sequence[str], layout: Any) -> list[str]:
    """Replace `$section` with what the target profile says that section is.

    A token the profile leaves empty expands to **nothing** rather than to the
    repository root. An agent whose only write path is `$frontend` against a
    backend-only project may write nowhere, which is correct and is what
    `qaas doctor` reports; expanding to `.` would hand it the whole tree.

    An unknown token is left alone. It is then an ordinary glob that matches
    nothing, which the same doctor check reports -- and that is a better
    outcome than a load failure for a typo in a file the user may not own.
    """
    out: list[str] = []
    for entry in paths:
        if not entry.startswith("$"):
            out.append(entry)
            continue
        section = entry[1:]
        if section not in LAYOUT_TOKENS:
            out.append(entry)
            continue
        for path in getattr(layout, section, None) or []:
            if path not in out:
                out.append(path)
    return out


def _resolve_agent_paths(config: "SystemConfig") -> "SystemConfig":
    """Expand layout tokens in every agent's policy, once the target is known.

    Runs after the profile is attached, because until then there is no layout
    to expand against.
    """
    if config.profile is None:
        return config
    layout = config.profile.layout
    budget = config.profile.diff_budget
    agents = {}
    for name, spec in config.agents.items():
        policy = spec.policy
        updates = {
            field: expand_layout_tokens(getattr(policy, field), layout)
            for field in _PATH_FIELDS
            if any(p.startswith("$") for p in getattr(policy, field))
        }
        # How wide a legitimate fix is belongs to the codebase, not to the
        # agent. FIXER's shipped `max_diff_files: 5` could not express a
        # cross-currency defect spanning six aggregation sites: it edited five,
        # was refused the sixth, and ended with an empty branch that REVIEWER
        # had to discover. Applied only to an agent that already has a budget --
        # this raises or lowers a ceiling, it never gives one to an agent the
        # §8.2 matrix left unbounded on purpose.
        if budget is not None:
            for field in ("max_diff_files", "max_diff_lines"):
                value = getattr(budget, field)
                if value is not None and getattr(policy, field) is not None:
                    updates[field] = value
        if not updates:
            agents[name] = spec
            continue
        agents[name] = spec.model_copy(
            update={"policy": policy.model_copy(update=updates)}
        )
    return config.model_copy(update={"agents": agents})


def load_config(
    config_dir: Path | str | None = None,
    *,
    search: Sequence[Path] | None = None,
    target: str | None = None,
) -> SystemConfig:
    """Read system.yaml plus every agents/*.yaml, layered across search paths.

    Passing `config_dir` positionally puts that directory at the **head of the
    layered search**, which is what `paths.py` has always documented --
    "1. explicit --config / QAAS_CONFIG_DIR, 2. project, 3. packaged" -- and
    what `QAAS_CONFIG_DIR` already did. It used to mean "this directory and
    nothing else", so the flag and the environment variable, documented as the
    same precedence step, behaved differently: `--config .qaas/config` pointed
    at a directory holding one overridden agent and hid the other fourteen, and
    `--config <packaged>` could not see the project's own `targets/`.

    Passing `search` gives the layer list verbatim, for callers that have
    already resolved it (and for tests that want exactly one directory).
    `system.yaml` is taken whole from the first layer that has one, while
    `agents/*.yaml` and `targets/*.yaml` are unioned by filename with earlier
    directories shadowing later ones -- so a user can override one agent without
    forking the whole roster and freezing on today's version of it.

    With neither argument, the workspace resolver decides.

    `target` beats everything -- system.yaml, QAAS_TARGET, the single-profile
    guess. It is what `qaas run --target X` and `qaas run --repo <url>` mean:
    *this* application, whatever is configured. Without it, a stale `target:`
    naming a profile that no longer exists killed the run inside config loading,
    before the override the operator had just typed was ever consulted.
    """
    if config_dir is not None:
        from qaas.paths import Workspace

        workspace = Workspace.resolve(config=config_dir)
        if workspace.missing_explicit_config:
            # A directory that does not exist is a typo, not a layer to skip.
            # `_existing` drops it silently, so `--config /wrogn/path` quietly
            # discarded the highest-precedence layer and ran on the packaged
            # defaults -- a different configuration than the one asked for,
            # reported as success. An *empty* directory is a different thing and
            # legitimately layers: that is how a user with one overridden agent
            # and no `system.yaml` of their own is meant to work.
            raise FileNotFoundError(
                f"--config names {workspace.missing_explicit_config}, which is not a "
                "directory. Nothing was read from it."
            )
        dirs = list(workspace.config_dirs)
    elif search is not None:
        dirs = [Path(d) for d in search]
    else:
        from qaas.paths import Workspace

        dirs = list(Workspace.resolve().config_dirs)

    system_path = next((d / "system.yaml" for d in dirs if (d / "system.yaml").is_file()), None)
    if system_path is None:
        looked = ", ".join(str(d) for d in dirs) or "(nowhere -- no search path)"
        raise FileNotFoundError(f"no system config at {dirs[0] / 'system.yaml'} (looked in: {looked})")

    raw: dict[str, Any] = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}

    # `target_app:` used to name the application's directory relative to the
    # process cwd. The target profile's `root` says the same thing and says it
    # better, so the field is gone -- but `extra="forbid"` would turn an old
    # system.yaml into a hard load failure, and someone else's committed config
    # is not ours to break. Dropped silently: there is nothing for the reader to
    # do about it, and the profile already carries the answer.
    raw.pop("target_app", None)

    # Backend overrides from the environment, so pointing a run at a real
    # tracker or reproducer is not a committed file change.
    #
    # `tracker: local` is the committed default and must stay that way. When
    # `jira` was committed instead, 18 tests failed and 14 errored: the agent
    # fixtures build a real JiraTracker, which demands credentials CI does not
    # have. The house rule is that the default `pytest` run is offline and free,
    # and a committed backend switch silently breaks it -- so the switch belongs
    # in the environment of the person who wants it, not in the repo.
    for key, var in (("tracker", TRACKER_ENV), ("vcs", VCS_ENV), ("target", TARGET_ENV)):
        raw_value = (os.environ.get(var) or "").strip()
        if raw_value:
            # Backends are lowercase literals; a target is a profile name.
            raw[key] = raw_value if key == "target" else raw_value.lower()

    # An explicit argument outranks both the file and the environment.
    if target:
        raw["target"] = target

    # Agents layer by filename. Walking the search paths in reverse means the
    # highest-precedence directory writes last and therefore wins.
    #
    # `agents/<target>/<agent>.yaml` is a further layer on top, active only
    # while that target is the one being run. Agent config is otherwise global:
    # one `fixer.yaml` serves every profile, so a project with two repositories
    # of different shapes could not configure FIXER for both -- editing it for
    # one silently mis-configured the other, and `qaas doctor` said so in both
    # directions. Layout tokens (`$backend`) fix that for *paths*, which was the
    # case that bit; a model, a turn cap or a diff budget still cannot vary by
    # target without this.
    #
    # Keyed on the target named explicitly or in system.yaml, not on the
    # single-profile guess made further down: an overlay that switched itself on
    # because there happened to be one profile on disk would be a configuration
    # nobody wrote.
    by_stem: dict[str, Path] = {}
    for d in reversed(dirs):
        for path in sorted((d / "agents").glob("*.yaml")):
            by_stem[path.stem] = path
    named_target = raw.get("target")
    if named_target:
        for d in reversed(dirs):
            overlay = d / "agents" / str(named_target)
            if overlay.is_dir():
                for path in sorted(overlay.glob("*.yaml")):
                    by_stem[path.stem] = path

    agents: dict[str, Any] = {}
    for path in by_stem.values():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = spec.get("name") or path.stem.upper()
        spec["name"] = name
        if name in agents:
            raise ValueError(f"duplicate agent definition for {name} at {path}")
        agents[name] = spec

    raw["agents"] = agents

    # `overrides.yaml` is a *partial* layer, and the only one.
    #
    # Every other layer replaces whole: an `agents/fixer.yaml` in a nearer
    # directory shadows the packaged file entirely, which is right for someone
    # forking an agent and wrong for someone who wants FIXER on a different
    # model. Forking the file to change one line freezes that agent's policy,
    # prompt and tool list on the day it was copied, so a later fix to a
    # `forbidden_paths` pattern never reaches them.
    #
    # So this merges, and it is deliberately narrow: `TUNABLE_AGENT_FIELDS`
    # holds the model and the budget knobs and nothing else. A policy is not
    # tunable from here -- the write-permission matrix is the thing the
    # guardrails enforce, and a partial layer that could widen `write_paths`
    # would be a second, quieter way to edit them.
    _apply_overrides(raw, dirs)

    config = SystemConfig.model_validate(raw)

    # Resolve the target profile if one is named and findable.
    #
    # A named-but-missing profile stays fatal -- running the wrong application
    # is worse than not running. But `target: null` is a legitimate state now:
    # `pip install qaas-python` gives you a working CLI that is not yet pointed
    # at anything, and the commands that need a profile say "run qaas init"
    # rather than dying inside config loading.
    # Profiles layer by filename, exactly as agents and skills do. This used to
    # take the *first* config layer that had a `targets/` at all -- and the day
    # `qaas run --repo` started writing a generated profile into the writable
    # layer (`.qaas/config/targets/`), that layer became "the" targets directory
    # and every profile in `<project>/config/targets/` vanished: `qaas doctor
    # --target corvid` reported the demo profile did not exist.
    profiles = target_files(dirs)
    chosen = config.target

    # No target named, but exactly one profile on disk: use it. Choosing between
    # two would be guessing, and running the wrong application is worse than not
    # running -- but with one candidate there is nothing to guess at, and making
    # the user restate it is ceremony. This is also what keeps this repository
    # working: its `system.yaml` ships in the package and names no target,
    # because a demo name has no business in the defaults everyone installs.
    if not chosen and len(profiles) == 1:
        chosen = next(iter(profiles))

    # `and profiles` used to guard this, so "named but absent stays fatal" held
    # only when at least one profile existed *somewhere*. With none -- which is
    # every fresh `pip install`, since the packaged `defaults/config/` ships no
    # `targets/` at all -- a target named by `--target`, `QAAS_TARGET` or
    # `system.yaml` was silently ignored and `target_root()` fell back to the
    # working directory. The run then pointed every write-path sandbox, the test
    # runner's cwd and the SDK subprocess at whatever directory the operator
    # happened to be standing in, and said nothing.
    if chosen:
        if chosen not in profiles:
            # Named but absent stays fatal: running the wrong application is
            # worse than not running. Listed from the merged view, so the
            # suggestion names every profile the user actually has.
            available = (
                f"Available: {', '.join(sorted(profiles))}. "
                if profiles
                else "No target profiles are visible on the config path at all. "
            )
            raise FileNotFoundError(
                f"no target profile '{chosen}'. {available}"
                "Create one with `qaas init <path-to-repo>`."
            )
        profile = load_target(chosen, profiles[chosen].parent)
        config = config.model_copy(update={"target": chosen, "profile": profile})
    return _resolve_agent_paths(config)


def target_files(dirs: Sequence[Path]) -> dict[str, Path]:
    """Every target profile visible across the config layers, nearest wins."""
    found: dict[str, Path] = {}
    for d in reversed(list(dirs)):
        base = Path(d) / "targets"
        if base.is_dir():
            for path in sorted(base.glob("*.yaml")):
                found[path.stem] = path
    return found
