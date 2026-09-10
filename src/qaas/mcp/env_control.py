"""The `env_control` MCP server — the environment an agent can actually name.

A defect envelope claims `reproduction.environment = {branch, fixture, flags}`.
That claim is only worth something if some component owns those three things and
can put them back. This server is that component: every tool here exists so a
later run (REPRODUCER re-running a repro, VERIFIER verifying a fix) can stand the world
up in the same shape and get the same answer.

Two deliberate properties:

* **Nothing shells out through a shell.** Every subprocess is an argv list built
  here from validated pieces, so a fixture name or a flag key coming out of a
  model can never become a command.
* **Nothing hangs and nothing raises.** Docker can be absent, the daemon can be
  down, the target app can be half-built. Each of those is a normal Tuesday, and
  each returns an `err` naming the cause rather than a traceback or a stall.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas.mcp.context import ToolContext, err, ok

#: Fallbacks only. Every one of these has a field on the target profile
#: (`environment.compose_file`, `environment.seed_sql`) that had existed since
#: the schema was written and that nothing read -- so the "portable" seam was
#: fiction, and anyone pointing this at their own repository hit a hardcoded
#: `target-app/api/seed/fixtures.sql` on their first run. These now apply only
#: when the profile is silent.
DEFAULT_COMPOSE_FILENAME = "docker-compose.yml"
DEFAULT_SEED_DIR = "api/seed"
DEFAULT_FIXTURE_FILE = "fixtures.sql"

# Kept as the old names so nothing importing them breaks.
COMPOSE_FILENAME = DEFAULT_COMPOSE_FILENAME
SEED_DIR = DEFAULT_SEED_DIR

#: Lets a test point the server at a docker that is not there, and lets an
#: operator point it at a non-PATH install, without either one editing code.
DOCKER_BIN_ENV = "QAAS_DOCKER_BIN"

DEFAULT_UP_TIMEOUT_S = 180
POLL_INTERVAL_S = 2.0
SEED_TIMEOUT_S = 120
SHORT_TIMEOUT_S = 30
LOGIN_TIMEOUT_S = 15

#: Impersonation is a real login against the running API rather than a
#: hand-minted JWT, so the token an agent receives is exactly the token a
#: browser would receive — an auth bug in issuance is visible to the agent
#: instead of bypassed by it. That part was always right.
#:
#: What was wrong: the accounts and the password were hardcoded to the bundled
#: demo. `profile.auth.roles` has existed all along, and `Role.password()`
#: already reads from an environment variable precisely so that a committed
#: profile never carries a credential. Until this was wired up, pointing qaas at
#: a real application with `auth.mode: login` would POST the literal string
#: below at that application's login endpoint. These are fallbacks for the
#: bundled demo now, used only when the profile declares no roles.
FALLBACK_USERS: dict[str, str] = {
    "admin": "admin@northwind.test",
    "member": "member@northwind.test",
    "viewer": "viewer@northwind.test",
}
FALLBACK_PASSWORD = "password123"

SEEDED_USERS = FALLBACK_USERS  # old name, kept for importers
SEEDED_PASSWORD = FALLBACK_PASSWORD

_FIXTURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_FLAG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DML_RE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.I)
_TAG_RE = re.compile(r"^(INSERT)\s+\d+\s+(\d+)$|^(UPDATE|DELETE)\s+(\d+)$")


# --------------------------------------------------------------------------
# subprocess plumbing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Proc:
    """The result of one subprocess run. Never an exception."""

    argv: list[str]
    code: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def okay(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, limit: int = 1200) -> str:
        blob = (self.err or self.out).strip()
        return blob[-limit:] if blob else "(no output)"


async def _exec(argv: list[str], timeout: float, *, stdin: bytes | None = None, cwd: Path | None = None) -> _Proc:
    """Run argv with a hard deadline, capturing output. Kills on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )
    except (OSError, ValueError) as exc:  # binary vanished between check and exec
        return _Proc(argv, 127, "", str(exc))

    try:
        out, errout = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001 - the process is already gone; nothing to salvage
            pass
        return _Proc(argv, -1, "", f"timed out after {timeout:.0f}s", timed_out=True)

    return _Proc(argv, proc.returncode or 0, out.decode(errors="replace"), errout.decode(errors="replace"))


def docker_bin() -> str | None:
    """Path to the docker binary, or None if there is nothing usable."""
    override = os.environ.get(DOCKER_BIN_ENV)
    if override:
        path = Path(override)
        return override if path.is_file() and os.access(override, os.X_OK) else None
    return shutil.which("docker")


# --------------------------------------------------------------------------
# compose introspection
# --------------------------------------------------------------------------


def _environment(ctx: ToolContext):
    """The target's environment block, or None when no profile is loaded."""
    profile = getattr(ctx.config, "profile", None)
    return getattr(profile, "environment", None) if profile else None


def _compose_path(ctx: ToolContext) -> Path:
    """The compose file this target declares, falling back to the usual name."""
    env = _environment(ctx)
    declared = getattr(env, "compose_file", None) if env else None
    return ctx.target_root / (declared or DEFAULT_COMPOSE_FILENAME)


def _seed_dir(ctx: ToolContext) -> Path:
    """Where fixture SQL lives for this target.

    `environment.seed_sql` names a file; its directory is the fixture
    directory, which lets a project keep fixtures anywhere without this server
    having an opinion about `api/seed`.
    """
    env = _environment(ctx)
    declared = getattr(env, "seed_sql", None) if env else None
    if declared:
        return (ctx.target_root / declared).parent.resolve()
    return (ctx.target_root / DEFAULT_SEED_DIR).resolve()


def _login_path(ctx: ToolContext) -> str:
    """The login path this target declares. `auth.login_endpoint` is written as
    "POST /v1/auth/login", so the verb is stripped off."""
    profile = getattr(ctx.config, "profile", None)
    auth = getattr(profile, "auth", None) if profile else None
    declared = getattr(auth, "login_endpoint", None) if auth else None
    if not declared:
        return "/v1/auth/login"
    path = declared.split(None, 1)[-1].strip() if " " in declared else declared.strip()
    return path if path.startswith("/") else f"/{path}"


def _roles(ctx: ToolContext) -> dict[str, tuple[str, str | None]]:
    """role -> (username, password), from the profile where one declares them.

    `profile.auth.roles` and `Role.password()` have existed since the schema was
    written; nothing read them, so impersonation was welded to three demo
    accounts and a literal password. Pointing qaas at a real application with
    `auth.mode: login` would have POSTed that literal at its login endpoint.

    Passwords come from the environment variable each role names, never from the
    profile itself, because a profile is committed to a repository.
    """
    profile = getattr(ctx.config, "profile", None)
    auth = getattr(profile, "auth", None) if profile else None
    declared = getattr(auth, "roles", None) or {}
    if declared:
        return {name: (role.username, role.password()) for name, role in declared.items()}
    # No roles declared: the bundled demo, whose fixture password is public and
    # whose accounts exist only inside a throwaway container.
    return {name: (email, FALLBACK_PASSWORD) for name, email in FALLBACK_USERS.items()}


def _default_fixture(ctx: ToolContext) -> str:
    """The filename `seed(fixture="default")` means for this target."""
    env = _environment(ctx)
    declared = getattr(env, "seed_sql", None) if env else None
    return Path(declared).name if declared else DEFAULT_FIXTURE_FILE


def _load_compose(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _host_port(entry: Any) -> int | None:
    """First host port from a compose `ports` entry, in any of its spellings."""
    if isinstance(entry, int):
        return entry
    if isinstance(entry, dict):
        published = entry.get("published")
        return int(published) if str(published).isdigit() else None
    if isinstance(entry, str):
        # compose accepts "8000", "8000:8000", "127.0.0.1:8000:8000" and
        # "8000-8002:8000-8002". Taking the first colon-segment read the *host
        # address* as the port in the three-part form and answered None, so a
        # service bound to an explicit interface reported no URL at all. The
        # host port is the second-from-last segment when there are three, the
        # first when there are two, and the whole thing when there is one; a
        # range contributes its lower bound.
        body = entry.split("/")[0]
        parts = body.split(":")
        head = parts[-2] if len(parts) >= 3 else parts[0]
        head = head.split("-")[0]
        return int(head) if head.isdigit() else None
    return None


def _service_urls(compose: dict[str, Any]) -> dict[str, str]:
    """Reachable URL per service, derived from the published ports.

    A service with POSTGRES_* environment gets a libpq URL rather than an http
    one, because that is what a caller would actually connect with.
    """
    urls: dict[str, str] = {}
    for name, spec in (compose.get("services") or {}).items():
        if not isinstance(spec, dict):
            continue
        ports = spec.get("ports") or []
        port = next((p for p in (_host_port(e) for e in ports) if p), None)
        if port is None:
            continue
        env = spec.get("environment") or {}
        env = env if isinstance(env, dict) else {}
        if "POSTGRES_DB" in env:
            urls[name] = (
                f"postgresql://{env.get('POSTGRES_USER', 'postgres')}:"
                f"{env.get('POSTGRES_PASSWORD', '')}@localhost:{port}/{env.get('POSTGRES_DB')}"
            )
        else:
            urls[name] = f"http://localhost:{port}"
    return urls


def _db_service(compose: dict[str, Any]) -> tuple[str, dict[str, str]] | None:
    """The postgres service and its credentials, if the compose file has one."""
    for name, spec in (compose.get("services") or {}).items():
        env = (spec or {}).get("environment") or {}
        if isinstance(env, dict) and "POSTGRES_DB" in env:
            return name, {str(k): str(v) for k, v in env.items()}
    return None


def _parse_ps(raw: str) -> list[dict[str, Any]]:
    """`docker compose ps --format json` — array in some versions, JSONL in others."""
    text = raw.strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, list) else [doc]
    except json.JSONDecodeError:
        pass
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _service_state(row: dict[str, Any]) -> tuple[str, str]:
    state = str(row.get("State") or row.get("Status") or "unknown").lower()
    health = str(row.get("Health") or "").lower()
    return state, health


def _is_ready(state: str, health: str) -> bool:
    # No healthcheck means compose reports an empty Health; running is the best
    # signal available, and pretending otherwise would hang forever on `web`.
    if not state.startswith("running") and not state.startswith("up"):
        return False
    return health in ("", "healthy")


# --------------------------------------------------------------------------
# run-scoped state
# --------------------------------------------------------------------------


def _flags_path(ctx: ToolContext) -> Path:
    """The flags file for this run.

    Kept inside the run directory rather than in the app tree so a run cannot
    leave residue in the repository under test, and so two runs never fight over
    one file. The target API does not read it yet — see the note in `set_flag`.
    """
    return ctx.store.dir / "flags.json"


def _state_path(ctx: ToolContext) -> Path:
    return ctx.store.dir / "env-state.json"


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return doc if isinstance(doc, dict) else dict(default)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_flags(ctx: ToolContext) -> dict[str, Any]:
    return _read_json(_flags_path(ctx), {"flags": {}, "clock": None})


def _read_state(ctx: ToolContext) -> dict[str, Any]:
    return _read_json(_state_path(ctx), {"fixture": None, "branch": None, "services": []})


def _current_branch(target_root: Path) -> str | None:
    """Best-effort git branch. Absent git is not an error here."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(target_root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def build_tools(ctx: ToolContext) -> list:
    """The env_control tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """

    compose_file = _compose_path(ctx)

    def preflight(*, needs_docker: bool = True) -> dict[str, Any] | None:
        """One gate for the two ways this server can be unusable."""
        if not compose_file.exists():
            return err(
                f"No compose file at {compose_file}. This server drives the target app "
                "through docker compose; without that file there is no environment to control."
            )
        if needs_docker and docker_bin() is None:
            override = os.environ.get(DOCKER_BIN_ENV)
            where = f"{DOCKER_BIN_ENV}={override}" if override else "PATH"
            return err(
                f"The `docker` binary was not found ({where}). Every environment tool here "
                "bottoms out in `docker compose`, so nothing can be started, seeded or reset. "
                "Install Docker or unset the override, then retry."
            )
        return None

    def compose_argv(*args: str) -> list[str]:
        return [docker_bin() or "docker", "compose", "-f", str(compose_file), *args]

    async def ps_rows(timeout: float = SHORT_TIMEOUT_S) -> tuple[list[dict[str, Any]], _Proc]:
        proc = await _exec(compose_argv("ps", "--all", "--format", "json"), timeout, cwd=ctx.target_root)
        return (_parse_ps(proc.out) if proc.okay else []), proc

    async def wait_ready(services: list[str], deadline: float) -> tuple[dict[str, dict[str, str]], str | None]:
        """Poll compose until every named service is ready or the deadline passes."""
        loop = asyncio.get_running_loop()
        observed: dict[str, dict[str, str]] = {}
        while True:
            rows, proc = await ps_rows()
            if not proc.okay:
                return observed, f"`docker compose ps` failed: {proc.tail()}"
            observed = {}
            for row in rows:
                name = str(row.get("Service") or row.get("Name") or "")
                if not name:
                    continue
                state, health = _service_state(row)
                observed[name] = {"state": state, "health": health}

            dead = [
                n for n in services
                if n in observed and observed[n]["state"].startswith(("exited", "dead"))
            ]
            if dead:
                return observed, f"service(s) exited during startup: {', '.join(sorted(dead))}"

            pending = [
                n for n in services
                if n not in observed or not _is_ready(observed[n]["state"], observed[n]["health"])
            ]
            if not pending:
                return observed, None
            if loop.time() >= deadline:
                return observed, f"timed out waiting for: {', '.join(sorted(pending))}"
            await asyncio.sleep(POLL_INTERVAL_S)

    async def run_seed(fixture: str) -> dict[str, Any]:
        """Pipe a fixture file into psql inside the db container."""
        compose = _load_compose(compose_file)
        db = _db_service(compose)
        if db is None:
            return err(f"{compose_file} declares no postgres service, so there is nothing to seed.")
        db_name, db_env = db

        if not _FIXTURE_RE.match(fixture):
            return err(
                f"Fixture name '{fixture}' is not a bare filename. "
                "Pass a name like 'default' or 'refunds', never a path."
            )
        filename = _default_fixture(ctx) if fixture == "default" else (
            fixture if fixture.endswith(".sql") else f"{fixture}.sql"
        )
        seed_dir = _seed_dir(ctx)
        path = (seed_dir / filename).resolve()
        if not path.is_relative_to(seed_dir):
            return err(f"Fixture '{fixture}' resolves outside {seed_dir}.")
        if not path.exists():
            available = sorted(p.stem for p in seed_dir.glob("*.sql")) if seed_dir.exists() else []
            return err(
                f"No fixture file at {path}. "
                + (f"Available: {', '.join(available)}." if available else "The seed directory is empty or absent.")
            )

        sql = path.read_bytes()
        proc = await _exec(
            compose_argv(
                "exec", "-T", db_name,
                "psql", "-U", db_env.get("POSTGRES_USER", "postgres"),
                "-d", db_env.get("POSTGRES_DB", "postgres"),
                "-v", "ON_ERROR_STOP=1",
            ),
            SEED_TIMEOUT_S,
            stdin=sql,
            cwd=ctx.target_root,
        )
        if proc.timed_out:
            return err(f"Seeding timed out after {SEED_TIMEOUT_S}s. Is the '{db_name}' service healthy?")
        if not proc.okay:
            return err(f"Seeding '{fixture}' failed: {proc.tail()}")

        rows = _rows_affected(sql.decode(errors="replace"), proc.out)
        state = _read_state(ctx)
        state["fixture"] = fixture
        _write_json(_state_path(ctx), state)
        ctx.store.log("env", agent=ctx.agent.name, action="seed", fixture=fixture, rows=rows)
        summary = ", ".join(f"{t}={n}" for t, n in sorted(rows.items())) or "no row counts reported by psql"
        return ok(f"Seeded fixture '{fixture}' from {path.name}. Rows affected: {summary}.",
                  fixture=fixture, rows_affected=rows, path=str(path))

    @tool(
        "spin_up",
        "Start the target app with docker compose and wait for its health checks. "
        "Returns each service's status and URL. Call this before anything that touches the app.",
        {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch this environment is meant to represent. Checked against the working tree, never checked out for you."},
                "services": {"type": "array", "items": {"type": "string"}, "description": "Subset of services to start. Default: all of them."},
                "timeout_s": {"type": "integer", "minimum": 10, "maximum": 900, "description": f"Overall budget. Default {DEFAULT_UP_TIMEOUT_S}s."},
            },
        },
    )
    async def spin_up(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight()
        if gate:
            return gate

        compose = _load_compose(compose_file)
        declared = list((compose.get("services") or {}).keys())
        if not declared:
            return err(f"{compose_file} declares no services; it is empty or malformed.")

        requested = args.get("services") or declared
        unknown = [s for s in requested if s not in declared]
        if unknown:
            return err(f"Unknown service(s): {', '.join(unknown)}. This compose file has: {', '.join(declared)}.")

        branch = args.get("branch")
        if branch:
            actual = _current_branch(ctx.target_root)
            if actual and actual != branch:
                return err(
                    f"You asked for branch '{branch}' but the working tree is on '{actual}'. "
                    "This server builds whatever is checked out; it will not move your tree. "
                    "Check the branch out first, or call spin_up without `branch` and accept "
                    f"'{actual}' as the environment identity."
                )

        timeout = float(args.get("timeout_s") or DEFAULT_UP_TIMEOUT_S)
        deadline = asyncio.get_running_loop().time() + timeout

        up = await _exec(compose_argv("up", "-d", *requested), timeout, cwd=ctx.target_root)
        if up.timed_out:
            return err(
                f"`docker compose up -d` did not finish within {timeout:.0f}s. "
                "The image build is the usual culprit; build it once by hand "
                "(`docker compose build`) and retry, or raise timeout_s."
            )
        if not up.okay:
            return err(
                "`docker compose up -d` failed. The target app may not be built yet. "
                f"Compose said: {up.tail()}"
            )

        observed, problem = await wait_ready(list(requested), deadline)
        urls = _service_urls(compose)
        report = {
            name: {
                "state": observed.get(name, {}).get("state", "missing"),
                "health": observed.get(name, {}).get("health") or "n/a",
                "url": urls.get(name),
            }
            for name in requested
        }

        state = _read_state(ctx)
        state["services"] = list(requested)
        state["branch"] = branch or _current_branch(ctx.target_root)
        _write_json(_state_path(ctx), state)
        ctx.store.log("env", agent=ctx.agent.name, action="spin_up", services=list(requested), problem=problem)

        if problem:
            logs = await _exec(compose_argv("logs", "--tail", "40", *requested), SHORT_TIMEOUT_S, cwd=ctx.target_root)
            return err(
                f"Environment did not come up cleanly: {problem}. "
                f"Status: {json.dumps(report)}. Last log lines: {logs.tail(2000)}"
            )

        lines = "; ".join(f"{n}: {v['state']}/{v['health']} {v['url'] or ''}".strip() for n, v in report.items())
        return ok(f"Environment up ({lines}).", services=report, branch=state["branch"])

    @tool(
        "seed",
        "Load a named fixture into the database. 'default' means fixtures.sql. "
        "Reports rows affected per table where psql tells us.",
        {
            "type": "object",
            "required": ["fixture"],
            "properties": {"fixture": {"type": "string", "description": "Bare fixture name, e.g. 'default'."}},
        },
    )
    async def seed(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight()
        if gate:
            return gate
        return await run_seed(str(args["fixture"]))

    @tool(
        "reset",
        "Return the database to its post-seed state: recreate the db container (its data is tmpfs, "
        "so this really is a wipe), then re-apply the fixture that was last seeded.",
        {"type": "object", "properties": {"fixture": {"type": "string", "description": "Override the fixture to re-apply. Default: whatever seed() last loaded."}}},
    )
    async def reset(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight()
        if gate:
            return gate

        compose = _load_compose(compose_file)
        db = _db_service(compose)
        if db is None:
            return err(f"{compose_file} declares no postgres service, so there is nothing to reset.")
        db_name, _ = db

        state = _read_state(ctx)
        fixture = str(args.get("fixture") or state.get("fixture") or "default")

        deadline = asyncio.get_running_loop().time() + DEFAULT_UP_TIMEOUT_S
        down = await _exec(compose_argv("rm", "--stop", "--force", "--volumes", db_name), SHORT_TIMEOUT_S * 2, cwd=ctx.target_root)
        if not down.okay:
            # `and not down.timed_out` used to be here, so a `compose rm` that
            # hung was treated as success and the run carried on to report a
            # wipe that had not happened. Every later finding then rests on
            # whatever state survived, which is the one outcome a reset exists to
            # rule out. A timeout is a failure with a different reason attached,
            # not an exemption.
            why = "timed out" if down.timed_out else down.tail()
            return err(
                f"Could not remove the '{db_name}' container: {why}. The database was NOT "
                "reset, so do not treat what follows as a clean environment."
            )

        up = await _exec(compose_argv("up", "-d", db_name), SHORT_TIMEOUT_S * 4, cwd=ctx.target_root)
        if not up.okay:
            return err(f"Could not restart '{db_name}': {up.tail()}")

        observed, problem = await wait_ready([db_name], deadline)
        if problem:
            return err(f"'{db_name}' did not become healthy after reset: {problem}. Status: {json.dumps(observed)}")

        # The API holds a connection pool that the recreated database just
        # invalidated. Restarting it is part of "back to post-seed state"; a
        # stale pool would surface as spurious 500s and a bogus finding.
        restarted = False
        rows, _ = await ps_rows()
        api_services = [
            str(r.get("Service") or "")
            for r in rows
            if str(r.get("Service") or "") not in ("", db_name) and _service_state(r)[0].startswith(("running", "up"))
        ]
        if api_services:
            restart = await _exec(compose_argv("restart", *api_services), SHORT_TIMEOUT_S * 2, cwd=ctx.target_root)
            restarted = restart.okay
            _, problem = await wait_ready(api_services, asyncio.get_running_loop().time() + DEFAULT_UP_TIMEOUT_S)
            if problem:
                return err(f"Dependent services did not recover after the db reset: {problem}")

        seeded = await run_seed(fixture)
        if seeded.get("isError"):
            return seeded

        ctx.store.log("env", agent=ctx.agent.name, action="reset", fixture=fixture)
        detail = seeded.get("structuredContent", {})
        return ok(
            f"Reset complete: '{db_name}' recreated, "
            f"{'dependents restarted, ' if restarted else ''}fixture '{fixture}' re-applied.",
            fixture=fixture,
            rows_affected=detail.get("rows_affected", {}),
            restarted=api_services if restarted else [],
        )

    @tool(
        "set_flag",
        "Set one feature flag for this run. Flags are part of the environment an envelope names, "
        "so anything you toggle here belongs in reproduction.environment.flags.",
        {
            "type": "object",
            "required": ["key", "value"],
            "properties": {
                "key": {"type": "string", "description": "Flag name, e.g. 'checkout.new_review_step'."},
                "value": {"description": "Any JSON value: boolean, string, number."},
            },
        },
    )
    async def set_flag(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight(needs_docker=False)
        if gate:
            return gate
        key = str(args["key"])
        if not _FLAG_KEY_RE.match(key):
            return err(f"Flag key '{key}' is not a plain identifier (letters, digits, . _ -, max 64 chars).")
        doc = _read_flags(ctx)
        doc.setdefault("flags", {})[key] = args["value"]
        _write_json(_flags_path(ctx), doc)
        ctx.store.log("env", agent=ctx.agent.name, action="set_flag", key=key, value=args["value"])
        return ok(
            f"Flag '{key}' = {json.dumps(args['value'])} recorded in {_flags_path(ctx)}. "
            "Note: the target API does not read this file yet, so treat the flag as run metadata "
            "until the app is wired to it — do not claim behaviour changed because of it.",
            flags=doc["flags"], path=str(_flags_path(ctx)),
        )

    @tool(
        "get_flags",
        "Read the flags and clock override in force for this run.",
        {"type": "object", "properties": {}},
    )
    async def get_flags(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight(needs_docker=False)
        if gate:
            return gate
        doc = _read_flags(ctx)
        return ok(
            json.dumps(doc, indent=2, sort_keys=True),
            flags=doc.get("flags", {}), clock=doc.get("clock"), path=str(_flags_path(ctx)),
        )

    @tool(
        "set_clock",
        "Record a clock override for this run, so a time-dependent repro names the time it assumed.",
        {
            "type": "object",
            "required": ["iso_timestamp"],
            "properties": {"iso_timestamp": {"type": "string", "description": "ISO-8601, e.g. '2026-01-31T23:59:59Z'."}},
        },
    )
    async def set_clock(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight(needs_docker=False)
        if gate:
            return gate
        raw = str(args["iso_timestamp"]).strip()
        try:
            datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return err(f"'{raw}' is not an ISO-8601 timestamp. Use e.g. '2026-01-31T23:59:59Z'.")
        doc = _read_flags(ctx)
        doc["clock"] = raw
        _write_json(_flags_path(ctx), doc)
        ctx.store.log("env", agent=ctx.agent.name, action="set_clock", clock=raw)
        return ok(
            f"Clock override for this run recorded as {raw} in {_flags_path(ctx)}. "
            "The target app does not consume the override yet; record it in the envelope environment "
            "rather than asserting the app moved in time.",
            clock=raw, path=str(_flags_path(ctx)),
        )

    @tool(
        "impersonate",
        "Get a real bearer token for a seeded user with the given role, by logging in against the "
        "running API. Use the token in an Authorization header exactly as a browser would.",
        {
            "type": "object",
            "required": ["role"],
            "properties": {"role": {"type": "string", "description": "A role this target declares under auth.roles."}},
        },
    )
    async def impersonate(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight(needs_docker=False)
        if gate:
            return gate
        role = str(args["role"]).lower()
        roles = _roles(ctx)
        entry = roles.get(role)
        if entry is None:
            return err(
                f"No role '{role}' for this target. Declared roles: "
                f"{', '.join(sorted(roles)) or '(none -- set auth.roles in the target profile)'}."
            )
        email, password = entry
        if not password:
            profile = getattr(ctx.config, "profile", None)
            auth = getattr(profile, "auth", None) if profile else None
            var = getattr(getattr(auth, "roles", {}).get(role, None), "password_env", "QAAS_PASSWORD")
            return err(
                f"Role '{role}' names environment variable {var} for its password and it is unset. "
                "Export it and try again -- credentials are never read from the profile itself."
            )

        compose = _load_compose(compose_file)
        urls = _service_urls(compose)
        base = os.environ.get("QAAS_TARGET_BASE_URL") or next(
            (u for n, u in urls.items() if u.startswith("http") and n != "web"), None
        )
        if base is None:
            return err("No HTTP service with a published port in the compose file; nowhere to log in.")

        # Field names come from the profile: not every API calls them
        # "email" and "password".
        profile = getattr(ctx.config, "profile", None)
        auth = getattr(profile, "auth", None) if profile else None
        user_field = getattr(auth, "username_field", None) or "email"
        pass_field = getattr(auth, "password_field", None) or "password"
        payload = json.dumps({user_field: email, pass_field: password}).encode()
        request = urllib.request.Request(
            f"{base.rstrip('/')}{_login_path(ctx)}", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )

        def _post() -> tuple[int, str]:
            try:
                with urllib.request.urlopen(request, timeout=LOGIN_TIMEOUT_S) as resp:  # noqa: S310 - fixed http scheme
                    return resp.status, resp.read().decode(errors="replace")
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode(errors="replace")
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                return 0, str(exc)

        status, body = await asyncio.to_thread(_post)
        if status == 0:
            return err(
                f"Could not reach the API at {base} ({body}). Call spin_up first; "
                "if it is up, the api service is not healthy."
            )
        if status != 200:
            return err(
                f"Login as {email} returned HTTP {status}: {body[:400]}. "
                "The database may not be seeded — call seed('default')."
            )
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            return err(f"Login returned HTTP 200 but not JSON: {body[:200]}")
        token = doc.get("access_token")
        if not token:
            return err(f"Login response has no access_token: {body[:200]}")

        ctx.store.log("env", agent=ctx.agent.name, action="impersonate", role=role, email=email)
        return ok(
            f"Signed in as {email} ({doc.get('role', role)}). Send header: Authorization: Bearer <token>.",
            role=doc.get("role", role), email=email, token=token, base_url=base,
        )

    @tool(
        "status",
        "What is running, which fixture is loaded, which flags are set. "
        "Read this before assuming the environment is in the state you left it.",
        {"type": "object", "properties": {}},
    )
    async def status(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight()
        if gate:
            return gate
        compose = _load_compose(compose_file)
        urls = _service_urls(compose)
        rows, proc = await ps_rows()
        if not proc.okay:
            return err(
                "`docker compose ps` failed, so the environment state is unknown: "
                f"{proc.tail()}"
            )
        services = {}
        for row in rows:
            name = str(row.get("Service") or row.get("Name") or "")
            if not name:
                continue
            state, health = _service_state(row)
            services[name] = {"state": state, "health": health or "n/a", "url": urls.get(name)}

        env_state = _read_state(ctx)
        flags = _read_flags(ctx)
        payload = {
            "services": services,
            "fixture": env_state.get("fixture"),
            "branch": env_state.get("branch") or _current_branch(ctx.target_root),
            "flags": flags.get("flags", {}),
            "clock": flags.get("clock"),
        }
        running = [n for n, v in services.items() if v["state"].startswith(("running", "up"))]
        headline = f"{len(running)} service(s) running: {', '.join(sorted(running))}." if running else "Nothing is running."
        return ok(f"{headline} Fixture: {payload['fixture'] or 'none loaded'}.", **payload)

    @tool(
        "tear_down",
        "Stop the target app and delete its volumes. Call when you are done; leaving it up costs "
        "the next run a confusing dirty state.",
        {"type": "object", "properties": {}},
    )
    async def tear_down(args: dict[str, Any]) -> dict[str, Any]:
        gate = preflight()
        if gate:
            return gate
        proc = await _exec(compose_argv("down", "-v"), SHORT_TIMEOUT_S * 4, cwd=ctx.target_root)
        if proc.timed_out:
            return err(f"`docker compose down -v` timed out after {SHORT_TIMEOUT_S * 4}s; containers may still be up.")
        if not proc.okay:
            return err(f"`docker compose down -v` failed: {proc.tail()}")
        state = _read_state(ctx)
        state.update({"services": [], "fixture": None})
        _write_json(_state_path(ctx), state)
        ctx.store.log("env", agent=ctx.agent.name, action="tear_down")
        return ok("Environment torn down and volumes removed.")

    return [spin_up, seed, reset, set_flag, get_flags, set_clock, impersonate, status, tear_down]


def _rows_affected(sql: str, psql_output: str) -> dict[str, int]:
    """Pair psql's command tags with the DML statements that produced them.

    psql reports `INSERT 0 12` without naming the table, so the only way to
    attribute counts is positionally: the nth tag belongs to the nth DML
    statement. If the two sequences disagree in length we report the prefix we
    can trust rather than guessing — a wrong row count is worse than none.
    """
    targets = [m.group(2) for m in _DML_RE.finditer(sql)]
    counts: list[int] = []
    for line in psql_output.splitlines():
        match = _TAG_RE.match(line.strip())
        if match:
            counts.append(int(match.group(2) or match.group(4) or 0))
    rows: dict[str, int] = {}
    for table, count in zip(targets, counts):
        rows[table] = rows.get(table, 0) + count
    return rows


def build(ctx: ToolContext):
    """Construct the env_control MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="env_control", version="1.0.0", tools=build_tools(ctx))
