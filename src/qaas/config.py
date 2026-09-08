"""Configuration: agents are data, not code.

An agent is a prompt file plus an entry in `config/agents/`. Adding one of the
remaining agents from the roster should never require touching the conductor,
the runner, or the guardrails — that is the property this module exists to keep.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qaas.target import TargetProfile, load_target

# §5.3 is explicit that no agent gets more than six MCP servers, because tool
# selection accuracy falls off past roughly 5-7. Enforced, not just documented.
MAX_MCP_SERVERS_PER_AGENT = 6

Layer = Literal["control", "discovery", "triage", "remediation", "reporting"]


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
    max_budget_usd: float = 2.0

    mcp_servers: list[str] = Field(default_factory=list)
    builtin_tools: list[str] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    # Procedure lives in skills, role and standards live in the prompt. A skill
    # named here is preloaded; the agent can still reach others through Skill.
    skills: list[str] = Field(default_factory=list)

    # Tools this agent must have called before it is allowed to finish. The Stop
    # hook enforces it. Without this an agent can produce a confident summary and
    # no artifact, and the failure only surfaces afterwards in the conductor —
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


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_confidence_to_file: float = 0.6
    max_findings_per_agent_run: int = 25
    max_tickets_per_run: int = 10
    flake_runs: int = 5
    max_mender_arbiter_round_trips: int = 2
    max_proof_reopens: int = 1


class RunMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str
    agents: list[str]
    max_budget_usd: float = 10.0
    max_wall_clock_s: int = 3600
    max_concurrency: int = 3
    files_tickets: bool = True


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = "qaas"

    #: Which target profile in config/targets/ this run is pointed at. The
    #: profile is what makes the system portable: without it every prompt and
    #: every environment call is welded to the application it was built beside.
    target: str = "corvid"

    #: Repository root of the target, resolved from the profile at load time.
    #: Kept as a plain path because most callers only need that much.
    target_app: str = "target-app"
    tracker: Literal["local", "jira"] = "local"
    vcs: Literal["local", "github"] = "local"
    thresholds: Thresholds = Field(default_factory=Thresholds)
    run_modes: dict[str, RunMode] = Field(default_factory=dict)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    profile: TargetProfile | None = Field(default=None, exclude=True)

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


def load_config(config_dir: Path | str = "config") -> SystemConfig:
    """Read config/system.yaml plus every config/agents/*.yaml."""
    config_dir = Path(config_dir)
    system_path = config_dir / "system.yaml"
    if not system_path.exists():
        raise FileNotFoundError(f"no system config at {system_path}")

    raw: dict[str, Any] = yaml.safe_load(system_path.read_text()) or {}

    agents: dict[str, Any] = {}
    for path in sorted((config_dir / "agents").glob("*.yaml")):
        spec = yaml.safe_load(path.read_text()) or {}
        name = spec.get("name") or path.stem.upper()
        spec["name"] = name
        if name in agents:
            raise ValueError(f"duplicate agent definition for {name} at {path}")
        agents[name] = spec

    raw["agents"] = agents

    config = SystemConfig.model_validate(raw)

    # Resolve the target profile. A missing one is fatal rather than defaulted:
    # running the wrong application is worse than not running.
    targets_dir = config_dir / "targets"
    if targets_dir.exists():
        profile = load_target(config.target, targets_dir)
        config = config.model_copy(update={"profile": profile, "target_app": profile.root})
    return config
