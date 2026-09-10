"""What the system is pointed at.

Everything the agents need to know about an application they have never seen:
where its code lives, how to reach it, who its users are. Without this the
system can only ever run against the app it was built alongside — which is the
difference between a demo and a tool.

A profile is deliberately small and mostly optional. An agent can discover a
great deal on its own; what it cannot discover is anything requiring a
credential, a URL that is not in the repository, or a judgement about which of
three directories is "the backend". Those are what a profile supplies.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from qaas.paths import project_root

# Directories that are never product code. Excluded by default so a profile does
# not have to restate them and an agent does not waste a turn reading vendored
# dependencies.
DEFAULT_EXCLUDES = [
    "node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
    ".git", ".next", "target", "coverage", ".pytest_cache", "site-packages",
]


class Layout(BaseModel):
    """Where things are. Every field is a hint; agents verify what they find."""

    model_config = ConfigDict(extra="forbid")

    backend: list[str] = Field(default_factory=list)
    frontend: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    migrations: list[str] = Field(default_factory=list)
    spec: str | None = Field(default=None, description="OpenAPI document, if one exists.")
    ownership: str | None = Field(default=None, description="CODEOWNERS or equivalent.")
    docs: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))

    def described(self) -> str:
        """A prose sketch of the layout for an agent's task prompt."""
        bits = []
        for label, paths in (
            ("backend", self.backend), ("frontend", self.frontend),
            ("tests", self.tests), ("migrations", self.migrations),
            ("docs", self.docs),
        ):
            if paths:
                bits.append(f"{label}: {', '.join(paths)}")
        if self.spec:
            bits.append(f"API spec: {self.spec}")
        if self.ownership:
            bits.append(f"ownership: {self.ownership}")
        return "; ".join(bits) or "not recorded — discover it yourself"


class Role(BaseModel):
    """One account an agent can act as.

    Credentials are read from the environment, never stored here: a profile is
    committed to a repository and a password in it is a leak, not a convenience.
    """

    model_config = ConfigDict(extra="forbid")

    username: str
    password_env: str = "QAAS_PASSWORD"
    description: str = ""

    def password(self) -> str | None:
        return os.environ.get(self.password_env)


class Auth(BaseModel):
    """How an agent gets a credential for the running app."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "login", "token"] = "none"
    login_endpoint: str | None = Field(
        default=None, description="e.g. 'POST /v1/auth/login'"
    )
    username_field: str = "email"
    password_field: str = "password"
    token_path: str = "access_token"
    token_env: str | None = Field(
        default=None, description="For mode=token: env var holding a bearer token."
    )
    roles: dict[str, Role] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> "Auth":
        if self.mode == "login" and not self.login_endpoint:
            raise ValueError("auth.mode is 'login' but no login_endpoint is set")
        if self.mode == "login" and not self.roles:
            raise ValueError("auth.mode is 'login' but no roles are defined")
        if self.mode == "token" and not self.token_env:
            raise ValueError("auth.mode is 'token' but no token_env is set")
        return self

    def missing_secrets(self) -> list[str]:
        """Env vars this profile needs that are not set. Checked before a run."""
        missing = []
        if self.mode == "token" and self.token_env and not os.environ.get(self.token_env):
            missing.append(self.token_env)
        if self.mode == "login":
            for role in self.roles.values():
                if not role.password() and role.password_env not in missing:
                    missing.append(role.password_env)
        return missing


class Environment(BaseModel):
    """How to get a running instance of the application.

    Three modes, because real projects differ and pretending otherwise is what
    makes a tool unusable:

      compose  — this system brings the app up and owns its lifecycle.
      external — the app is already running somewhere (staging, a dev server).
                 Agents may read and exercise it but never reset or reseed it.
      none     — there is no reachable instance. Discovery is static only:
                 code, spec and schema. Most first runs against a real repo
                 start here, and that is a perfectly useful mode.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["compose", "external", "none"] = "none"
    api_url: str | None = None
    web_url: str | None = None
    health_path: str = "/health"

    compose_file: str | None = None
    services: list[str] = Field(default_factory=list)
    seed_sql: str | None = None
    db_service: str | None = None
    db_user: str | None = None
    db_name: str | None = None

    startup_timeout_s: int = 180

    @property
    def is_managed(self) -> bool:
        """Whether this system may create, reset and destroy the environment."""
        return self.mode == "compose"

    @property
    def is_reachable(self) -> bool:
        return self.mode in {"compose", "external"}

    @model_validator(mode="after")
    def _coherent(self) -> "Environment":
        if self.mode == "compose" and not self.compose_file:
            raise ValueError("environment.mode is 'compose' but no compose_file is set")
        if self.mode == "external" and not (self.api_url or self.web_url):
            raise ValueError(
                "environment.mode is 'external' but neither api_url nor web_url is set"
            )
        return self


class TargetProfile(BaseModel):
    """One application this system can be pointed at."""

    model_config = ConfigDict(extra="forbid")

    name: str
    root: str = Field(description="Path to the repository, relative to cwd or absolute.")
    description: str = ""
    repo_url: str | None = None
    default_branch: str = "main"

    layout: Layout = Field(default_factory=Layout)
    environment: Environment = Field(default_factory=Environment)
    auth: Auth = Field(default_factory=Auth)

    #: Golden ledger for calibration. Absent for real applications — you only
    #: have one for an app whose defects you planted yourself.
    ledger: str | None = None

    @model_validator(mode="after")
    def _name_is_a_slug(self) -> "TargetProfile":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", self.name):
            raise ValueError("target name must be a lowercase slug, e.g. 'my-app'")
        return self

    def root_path(self, base: Path | None = None) -> Path:
        """Where the application under test actually is, on this machine.

        This is *the* answer to "what am I testing", and it is deliberately not
        the process cwd. `qaas run --repo <url>` clones into
        `.qaas/targets/<slug>`, so the target is routinely somewhere the qaas
        project is not; an absolute `root` in a profile is honoured as written.
        """
        root = Path(self.root)
        if root.is_absolute():
            return root
        return (base if base is not None else project_root()) / root

    def readiness(self, base: Path | None = None) -> list[str]:
        """Everything that would stop a run right now. Empty means ready."""
        problems: list[str] = []
        root = self.root_path(base)
        if not root.exists():
            problems.append(f"repository root does not exist: {root}")
        elif not root.is_dir():
            problems.append(f"repository root is not a directory: {root}")

        for missing in self.auth.missing_secrets():
            problems.append(f"environment variable {missing} is not set")

        if self.environment.mode == "compose":
            compose = root / (self.environment.compose_file or "")
            if not compose.exists():
                problems.append(f"compose file not found: {compose}")
        if self.layout.spec:
            spec = root / self.layout.spec
            if not spec.exists():
                problems.append(f"API spec not found: {spec}")
        return problems

    def capabilities(self) -> dict[str, bool]:
        """What this profile makes possible. Drives which agents can usefully run."""
        return {
            "static_analysis": True,
            "spec_diff": self.layout.spec is not None,
            "live_api": self.environment.is_reachable and self.environment.api_url is not None,
            "live_ui": self.environment.is_reachable and self.environment.web_url is not None,
            "reset_state": self.environment.is_managed,
            "impersonate": self.auth.mode != "none",
            "scored": self.ledger is not None,
        }


def load_target(name: str, targets_dir: Path | str = "config/targets") -> TargetProfile:
    path = Path(targets_dir) / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in Path(targets_dir).glob("*.yaml"))
        raise FileNotFoundError(
            f"no target profile '{name}' at {path}. "
            f"Available: {', '.join(available) or 'none'}. "
            "Create one with `qaas init <path-to-repo>`."
        )
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("name", name)
    return TargetProfile.model_validate(raw)


# `list_targets(one_dir)` lived here and is gone. Listing profiles from a single
# directory is the bug that hid `<project>/config/targets/` the moment anything
# wrote into `.qaas/config/targets/`; profiles layer across every config
# directory, and `config.target_files(dirs)` is the one place that knows it.


def agent_usable(agent_name: str, caps: dict[str, bool]) -> bool:
    """Whether an agent can do useful work with the capabilities available.

    Lives here, beside `capabilities()`, because it has two callers that must
    agree: `qaas doctor` reports it, and the conductor acts on it. They did not
    agree for a while -- doctor would say "agents that cannot: SURFACE" and then
    a run would dispatch SURFACE anyway and spend its whole budget looking for a
    browser that was never there. Being told an agent cannot work and then
    watching it run is worse than not being told.

    Everything except SURFACE can contribute from static analysis alone, at
    lower confidence. SURFACE without a reachable UI has nothing to do at all.
    """
    if agent_name in ("SURFACE", "USHER"):
        # Both drive a browser. USHER's whole method is navigating the product
        # as a person would; with nothing to navigate it has no job at all.
        return caps.get("live_ui", False)
    if agent_name == "GAUGE":
        # Performance work needs something to measure. GAUGE can read query and
        # rendering code statically, but a latency claim about an application it
        # never called is a guess, and this system does not ship guesses.
        return caps.get("live_api", False) or caps.get("live_ui", False)
    return True
