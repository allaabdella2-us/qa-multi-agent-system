"""qaas — command line for the multi-agent QA system."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from qaas import trace as trace_mod
from qaas.paths import Workspace, package_root, packaged_prompts, project_root
from qaas.config import MAX_MCP_SERVERS_PER_AGENT, load_config, target_files
from qaas.store import DEFAULT_ROOT, RunStore, SystemMapStore, list_runs
from qaas.ui.serve import DEFAULT_PORT

#: Colour by what a line *means*, not by which subsystem wrote it: a refusal and
#: a regression should catch the eye at the same speed in a 2000-line timeline.
KIND_STYLE = {
    "run_started": "bold", "run_finished": "bold",
    "agent_started": "cyan", "agent_finished": "cyan",
    "denial": "yellow", "stop_blocked": "yellow", "contract_unmet": "yellow",
    "skipped": "dim", "tool_call": "dim", "dry_run": "dim",
    "escalation": "red", "agent_error": "red", "tool_error": "red",
    "regression": "red", "reopened": "red",
    "envelope": "magenta", "reproduction": "magenta", "ticket": "magenta",
    "verdict": "green", "verified": "green", "review": "green",
}

#: Where skills are found, in precedence order. This used to be
#: `Path(__file__).resolve().parents[2] / ".claude" / "skills"` -- a climb that
#: lands on the repo root from a source checkout and on
#: `site-packages/../..` from an install. So `qaas validate` failed for every
#: pip user (it checks all 30 skills exist), and agents ran with no skills at
#: all, silently, because a missing skill is an empty listing rather than an
#: error. Resolved through the workspace now, which searches the project first
#: and the packaged copy last.
def _skill_dirs() -> tuple[Path, ...]:
    return Workspace.resolve().skill_dirs


def _skill_path(name: str) -> Path | None:
    for d in _skill_dirs():
        if (d / name / "SKILL.md").is_file():
            return d / name
    return None

def _activate_target(system_yaml_text: str, target_name: str) -> str:
    """Set `target:` in a system.yaml, preserving every comment around it.

    A regex rather than a YAML round-trip because PyYAML discards comments, and
    this file is more comment than configuration -- the comments are what make
    it editable by someone who has never read the source.
    """
    import re

    if re.search(r"^target:.*$", system_yaml_text, re.M):
        return re.sub(r"^target:.*$", f"target: {target_name}", system_yaml_text, count=1, flags=re.M)
    return f"target: {target_name}\n" + system_yaml_text


def _ledger_path(cfg) -> Path | None:
    """The golden ledger for this target, if it has one.

    `profile.ledger` has existed since the schema was written; this used to be
    hardcoded to `<target>/defects.yaml`. Most targets have no ledger at all --
    a golden ledger is a property of a *calibration* target, not of every
    application -- so None is the ordinary answer, not a failure.
    """
    profile = getattr(cfg, "profile", None)
    declared = getattr(profile, "ledger", None) if profile else None
    root = cfg.target_root()
    if declared:
        return root / declared
    fallback = root / "defects.yaml"
    return fallback if fallback.exists() else None


def _system_yaml(config_dir: Path | str | None) -> Path:
    """The system.yaml actually in force, for messages that name it."""
    if config_dir is not None:
        return Path(config_dir) / "system.yaml"
    ws = Workspace.resolve()
    found = ws.config_file("system.yaml")
    return found or (ws.state_root / "config" / "system.yaml")


def _writable_targets_dir(config_dir: Path | str | None) -> Path:
    """Where `qaas init` and `qaas run --repo` write a generated profile.

    The project (`.qaas/config/targets/`), never an installed package: `qaas
    init` must not try to write inside site-packages. Reading does NOT come
    through here -- profiles layer across every config directory, see
    `_target_files`.
    """
    if config_dir is not None:
        return Path(config_dir) / "targets"
    return Workspace.resolve().state_root / "config" / "targets"


def _target_files(config_dir: Path | str | None) -> dict[str, Path]:
    """Every target profile visible, by name, nearest config layer winning.

    Reading used to be "the first config layer that has a `targets/` directory",
    which is not layering at all: the moment a generated profile landed in
    `.qaas/config/targets/`, every profile in `<project>/config/targets/`
    disappeared from `qaas targets` and from `--target`. Profiles union by
    filename, exactly as agents and skills do.
    """
    dirs = [Path(config_dir)] if config_dir is not None else list(Workspace.resolve().config_dirs)
    return target_files(dirs)


def _load_target(name: str, config_dir: Path | str | None):
    """One profile by name, from wherever the layers put it."""
    from qaas.target import load_target

    found = _target_files(config_dir)
    if name not in found:
        raise typer.BadParameter(
            f"no target profile '{name}'. Available: {', '.join(sorted(found)) or 'none'}. "
            "Create one with `qaas init <path-to-repo>`."
        )
    return load_target(name, found[name].parent)


#: A repo argument that is a URL rather than a directory. `git@` has no scheme,
#: so this cannot be a urlparse.
_REPO_URL = re.compile(r"^(https?://|git@|ssh://)")


def _clone_root(clone_to: Path | str | None) -> Path:
    """Where a cloned target goes.

    Under `.qaas/targets/`, never into the caller's source tree. `qaas init`
    used to default to a bare `targets/` -- relative to the process cwd -- so
    pointing the tool at a URL from inside your own repository dropped a foreign
    checkout in the middle of it. Run state belongs in the state directory, and
    a clone is run state.
    """
    if clone_to is not None:
        return Path(clone_to).expanduser()
    return Workspace.resolve().state_root / "targets"


def _slug(text: str) -> str:
    """A target name: lowercase, sluggified, bounded. Also names the clone dir."""
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:40] or "target"


def _materialise_repo(repo: str, clone_to: Path | str | None) -> tuple[Path, str | None]:
    """A repo argument -> (local directory, origin url or None), cloning a URL.

    Shared by `qaas init` and `qaas run --repo` so a URL means exactly the same
    thing to both: one clone, in one place, reused on the next invocation. A
    second implementation of this would be a second set of rules about where
    someone else's code lands on your disk.
    """
    if not _REPO_URL.match(repo):
        root = Path(repo).expanduser()
        if not root.is_dir():
            console.print(f"[red]not a directory:[/red] {root}")
            raise typer.Exit(1)
        return root, None

    slug = _slug(re.sub(r"\.git$", "", repo.rstrip("/").split("/")[-1]))
    root = _clone_root(clone_to) / slug
    if root.exists():
        console.print(f"[dim]using existing clone at {root}[/dim]")
        return root, repo

    root.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"cloning {repo} -> {root}")
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "50", repo, str(root)],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        # git leaves the partial tree behind, and `root.exists()` above then
        # reports "using existing clone" on the next run -- so a clone that timed
        # out was silently reused as a complete checkout, and every finding after
        # it described a repository that was never fully there.
        shutil.rmtree(root, ignore_errors=True)
        console.print(f"[red]clone timed out[/red] after 600s; removed the partial checkout at {root}")
        raise typer.Exit(1) from None
    if result.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        console.print(f"[red]clone failed:[/red] {result.stderr.strip()[:400]}")
        raise typer.Exit(1)
    return root, repo


def _default_branch(root: Path) -> str:
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "main"


def _profile_root_value(root: Path) -> str:
    """How a generated profile should spell its `root`.

    Relative when the target sits inside the qaas project (portable, and what a
    committed profile wants), absolute otherwise. `build_profile` records
    whatever path it was handed, which may be `../thing` or `./thing` -- and a
    target root that means different things from different working directories
    is not acceptable, because it is what the write-path allowlist is anchored
    on.
    """
    resolved = root.resolve()
    base = project_root()
    return (
        resolved.relative_to(base).as_posix()
        if resolved.is_relative_to(base)
        else str(resolved)
    )


def _write_profile(profile, out: Path) -> None:
    """Persist a generated profile."""
    import yaml as _yaml

    payload = profile.model_dump(exclude_none=True, exclude_defaults=False)
    payload.pop("ledger", None)
    payload["root"] = _profile_root_value(Path(profile.root))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Target profile. Everything here was guessed by inspection — review it.\n"
        "# Credentials never belong in this file: reference environment variables.\n\n"
        + _yaml.safe_dump(payload, sort_keys=False, width=88)
    , encoding="utf-8")


def _provision_target(
    repo: str,
    *,
    name: str | None = None,
    api_url: str | None = None,
    web_url: str | None = None,
    clone_to: Path | str | None = None,
    config_dir: Path | str | None = None,
    force: bool = False,
    reuse_existing: bool = False,
) -> tuple[Any, str, Path, list[str], bool]:
    """Make sure a target profile exists for `repo`, cloning it if it is a URL.

    Returns `(profile, target_name, profile_path, notes, wrote)`.

    `reuse_existing` is the whole difference between the two callers. `qaas
    init` is a setup command and refuses to clobber a profile you may have spent
    time correcting; `qaas run --repo <url>` has to be idempotent, because
    pointing it at the same URL twice should run twice rather than fail the
    second time. Both go through here so a URL, a clone location and a target
    name mean one thing in this system rather than two.
    """
    from qaas.discover import build_profile
    from qaas.target import Environment, load_target

    root, repo_url = _materialise_repo(repo, clone_to)
    target_name = _slug(name or root.resolve().name)
    out = _writable_targets_dir(config_dir) / f"{target_name}.yaml"

    # Any layer, not just the writable one: a profile the user hand-wrote in
    # `<project>/config/targets/` is exactly the kind that must not be clobbered.
    existing = _target_files(config_dir).get(target_name)
    if existing is not None and not force:
        if reuse_existing:
            return load_target(target_name, existing.parent), target_name, existing, [], False
        console.print(f"[red]{existing} already exists.[/red] Use --force to overwrite.")
        raise typer.Exit(1)

    profile, notes = build_profile(
        target_name, root, repo_url=repo_url, default_branch=_default_branch(root)
    )
    if api_url or web_url:
        profile = profile.model_copy(
            update={"environment": Environment(mode="external", api_url=api_url, web_url=web_url)}
        )
    _write_profile(profile, out)
    # Re-read it: the file is what every later command loads, and a profile that
    # round-trips differently from the one in memory is a bug that only shows up
    # on the *next* invocation.
    return load_target(target_name, out.parent), target_name, out, notes, True


app = typer.Typer(add_completion=False, help="Multi-agent QA & remediation system.")
console = Console()


@app.callback()
def _bootstrap() -> None:
    """Runs before every command. Loads credentials from `.env`, if there is one.

    Credentials come from the environment and never from `config/`, which is
    committed -- that rule stands. This only makes it liveable: four exports in
    every new shell is how a real token ends up pasted into a config file. An
    already-exported variable always wins, so nothing here can override what an
    operator typed on the command line.
    """
    from qaas.envfile import load_env_file

    load_env_file()

#: None means "let the workspace decide" -- an explicit --config, then the
#: project, then the defaults that shipped in the wheel. A literal "config"
#: default meant every command outside this repo died on a missing directory.
ConfigDir = typer.Option(None, "--config", "-c", help="Config directory.")
Root = typer.Option(DEFAULT_ROOT, "--root", help="Runtime state directory.")


@app.command()
def init(
    repo: str = typer.Argument(..., help="Path to a local repository, or a git URL to clone."),
    name: str = typer.Option(None, "--name", "-n", help="Target name. Defaults to the directory name."),
    api_url: str = typer.Option(None, "--api-url", help="Base URL of a running API, if there is one."),
    web_url: str = typer.Option(None, "--web-url", help="Base URL of a running UI, if there is one."),
    clone_to: Path = typer.Option(None, "--clone-to", help="Where to clone, for a git URL. Default: <state>/targets/."),
    config_dir: Path | None = ConfigDir,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing profile."),
) -> None:
    """Point this system at a repository by writing a target profile.

    Everything it writes is a guess you are expected to review. Nothing runs and
    nothing is called until you do.
    """
    profile, target_name, out, notes, _ = _provision_target(
        repo,
        name=name,
        api_url=api_url,
        web_url=web_url,
        clone_to=clone_to,
        config_dir=config_dir,
        force=force,
    )

    # Activate the profile. This used to be step 3 of a printed checklist --
    # "set `target: x` in config/system.yaml" -- which was impossible outside
    # this repo, because there was no system.yaml to edit and no way to make
    # one. A setup step that ends by asking the human to go and edit a file has
    # not set anything up.
    project_config = out.parent.parent
    system_yaml = project_config / "system.yaml"
    if not system_yaml.exists():
        shipped = Workspace.resolve().config_file("system.yaml")
        base = shipped.read_text(encoding="utf-8") if shipped else "project: qaas\n"
        system_yaml.write_text(
            _activate_target(base, target_name)
            if shipped
            else f"project: qaas\ntarget: {target_name}\n"
        , encoding="utf-8")
        wrote_system = True
    else:
        system_yaml.write_text(_activate_target(system_yaml.read_text(encoding="utf-8"), target_name))
        wrote_system = False

    # `.qaas/` now holds a user's committed config next to their disposable run
    # state, so the obvious `.gitignore` line for `.qaas/` would drop the
    # configuration too. Spell out which half is which.
    gitignore = project_config.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Run state: regenerated every run, never worth committing.\n"
            "runs/\ntickets/\ngenerated/\nsystem-map/\nmemory.db\nartifacts/\n"
            "\n# config/ is NOT ignored -- it is yours, and it is the point.\n"
        , encoding="utf-8")

    console.print(f"\n[green]wrote {out}[/green]")
    console.print(
        f"[green]{'wrote' if wrote_system else 'updated'} {system_yaml}[/green]  "
        f"[dim](target: {target_name})[/dim]\n"
    )
    table = Table(header_style="bold", show_header=True)
    table.add_column("detected")
    table.add_column("value")
    for label, value in (
        ("backend", ", ".join(profile.layout.backend) or "-"),
        ("frontend", ", ".join(profile.layout.frontend) or "-"),
        ("tests", ", ".join(profile.layout.tests) or "-"),
        ("api spec", profile.layout.spec or "-"),
        ("ownership", profile.layout.ownership or "-"),
        ("environment", profile.environment.mode),
    ):
        table.add_row(label, value)
    console.print(table)


    for note in notes:
        console.print(f"[yellow]note:[/yellow] {note}")

    console.print(
        f"\n[bold]next[/bold]\n"
        f"  1. Read {out} and correct anything wrong.\n"
        f"  2. If the app runs somewhere, set environment.mode and the URLs, and fill in auth.\n"
        f"  3. `qaas doctor` to check readiness, then `qaas run --mode pr-check --dry-run`.\n"
    )


@app.command()
def targets(config_dir: Path | None = ConfigDir) -> None:
    """List the target profiles this system knows about."""
    from qaas.target import load_target

    found = _target_files(config_dir)
    if not found:
        console.print("[dim]no targets yet — run `qaas init <path-to-repo>`[/dim]")
        return
    active = load_config(config_dir).target
    table = Table(header_style="bold")
    for col in ("target", "root", "environment", "scored"):
        table.add_column(col)
    for n in sorted(found):
        p = load_target(n, found[n].parent)
        table.add_row(
            f"[bold]{n}[/bold] (active)" if n == active else n,
            p.root,
            p.environment.mode,
            "yes" if p.ledger else "no",
        )
    console.print(table)


@app.command()
def doctor(
    config_dir: Path | None = ConfigDir,
    target: str = typer.Option(None, "--target", "-t", help="Check this profile instead of the active one."),
) -> None:
    """Check whether a target is ready to run against."""
    cfg = load_config(config_dir, target=target)
    profile = _load_target(target, config_dir) if target else cfg.profile
    if profile is None:
        console.print("[red]no target profile loaded[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{profile.name}[/bold]  {profile.root}")
    if profile.description:
        console.print(f"[dim]{profile.description.strip()}[/dim]")

    caps = profile.capabilities()
    table = Table(header_style="bold")
    table.add_column("capability")
    table.add_column("", justify="center")
    table.add_column("meaning")
    meanings = {
        "static_analysis": "read the code, schema and spec",
        "spec_diff": "compare the implementation against a declared contract",
        "live_api": "call the API and observe real responses",
        "live_ui": "drive the UI in a browser",
        "reset_state": "seed and reset between checks",
        "impersonate": "act as different roles",
        "scored": "measure recall against a golden ledger",
    }
    for cap, ok in caps.items():
        table.add_row(cap, "[green]yes[/green]" if ok else "[dim]no[/dim]", meanings[cap])
    console.print(table)

    usable = [name for name, spec in sorted(cfg.agents.items()) if _agent_usable(spec, caps)]
    blocked = [n for n in sorted(cfg.agents) if n not in usable]
    console.print(f"\nagents that can work here: [green]{', '.join(usable)}[/green]")
    if blocked:
        console.print(f"agents that cannot: [yellow]{', '.join(blocked)}[/yellow]")

    problems = profile.readiness()
    if problems:
        console.print("\n[red]not ready:[/red]")
        for p in problems:
            console.print(f"  - {p}")
        raise typer.Exit(1)
    console.print("\n[green]ready[/green]")


def _agent_usable(spec, caps: dict[str, bool]) -> bool:
    """Delegates to `target.agent_usable`, which the router also uses.

    Two copies of this rule meant `qaas doctor` could report an agent unusable
    while a run dispatched it anyway.
    """
    from qaas.target import agent_usable

    return agent_usable(spec.name, caps)


@app.command()
def validate(config_dir: Path | None = ConfigDir) -> None:
    """Check config, prompts, and tool allowlists without calling the API."""
    try:
        cfg = load_config(config_dir)
    except Exception as exc:
        console.print(f"[red]config invalid:[/red] {exc}")
        raise typer.Exit(1)

    # Every prompt layer, not just the first. This used to check `prompt_dirs[0]`
    # alone, so a project that overrode one prompt -- putting a `.qaas/prompts/`
    # directory at the head of the search path -- made `qaas validate` report the
    # other seven as missing, when they resolve perfectly well from the package.
    from qaas.registry import SHARED_PROMPT

    ws = Workspace.resolve()
    problems: list[str] = []
    notes: list[str] = []
    if ws.prompt_file(SHARED_PROMPT) is None:
        problems.append(f"no {SHARED_PROMPT} on the prompt search path")
    for name, spec in sorted(cfg.agents.items()):
        if ws.prompt_file(spec.prompt) is None:
            problems.append(f"{name}: missing prompt file {spec.prompt}")
        if not spec.mcp_servers and not spec.builtin_tools:
            problems.append(f"{name}: has no tools at all")
        if spec.policy.may_create_tickets and spec.policy.max_tickets_per_run <= 0:
            problems.append(f"{name}: may create tickets but has no per-run cap")
        for skill in spec.skills:
            if _skill_path(skill) is None:
                problems.append(f"{name}: names skill '{skill}' with no SKILL.md")
        for tool in spec.must_call:
            if tool.startswith("mcp__") and tool.split("__")[1] not in spec.mcp_servers:
                problems.append(f"{name}: must_call '{tool}' but lacks that server")

    # A mode whose agents cannot fit inside its cap is a mode that stops
    # partway through, every time, and looks like it worked: agents run,
    # findings reach the ledger, nothing errors. `pr-check` shipped that way --
    # $6 cap against a $15 roster, so discovery spent $6.09 and REPRODUCER and TRIAGE
    # never dispatched. The mode meant for every pull request could not file a
    # ticket. It took a live run to notice; this check makes it free.
    for mode_name, mode in sorted(cfg.run_modes.items()):
        # Only meaningful when both sides declare a cap. The shipped config
        # declares none, so this check simply does not fire there.
        agent_caps = [
            cfg.agents[a].max_budget_usd for a in mode.agents
            if a in cfg.agents and cfg.agents[a].max_budget_usd is not None
        ]
        needed = sum(agent_caps)
        if mode.max_budget_usd is not None and agent_caps and needed > mode.max_budget_usd:
            missing = [a for a in mode.agents if a in cfg.agents][-1]
            problems.append(
                f"mode '{mode_name}': the agents' caps exceed the mode's cap, so "
                f"its cap, so the run stops before it reaches "
                f"{missing} and files nothing. Raise max_budget_usd or drop an agent"
            )

    # Skills nobody uses are a note, not a problem. Thirty skills ship in the
    # wheel; someone running a two-agent roster would otherwise see twenty
    # "orphans" and a non-zero exit from `qaas validate` on a fresh install --
    # which is the exact failure this whole exercise exists to remove. An agent
    # naming a skill that is NOT on disk stays a hard error, above.
    referenced = {s for spec in cfg.agents.values() for s in spec.skills}
    on_disk = {name for d in _skill_dirs() for p in d.glob("*/SKILL.md") for name in [p.parent.name]}
    orphans = on_disk - referenced
    if orphans:
        notes.append(f"{len(orphans)} skill(s) on disk that no agent in this config uses")

    table = Table(title="Agents", header_style="bold")
    for col in ("agent", "layer", "model", "servers", "skills", "must call", "writes"):
        table.add_column(col)
    for name, spec in sorted(cfg.agents.items()):
        writes = "read-only" if spec.policy.read_only else _describe_writes(spec)
        table.add_row(
            name,
            spec.layer,
            spec.model,
            f"{len(spec.mcp_servers)}/{MAX_MCP_SERVERS_PER_AGENT}",
            str(len(spec.skills)),
            ", ".join(t.rsplit("__", 1)[-1] for t in spec.must_call) or "-",
            writes,
        )
    console.print(table)

    # Show every subprocess a run would spawn, and every URL it would reach.
    #
    # Declaring a server grants nothing on its own -- an agent receives one only
    # by naming it in its own `mcp_servers:` list -- but once it does, the tools
    # of that server are allowed wholesale: `build_allowed_tools` grants
    # `mcp__<server>` and the guardrail's only question is whether the agent
    # declared it. qaas cannot police what a third-party server's tools do. So
    # the least this command can do is print what will run, before it runs.
    if cfg.mcp_servers:
        spawn = Table(title="Declared MCP servers", header_style="bold")
        for col in ("name", "kind", "what it runs", "used by"):
            spawn.add_column(col)
        for name, decl in sorted(cfg.mcp_servers.items()):
            users = [a for a, sp in sorted(cfg.agents.items()) if name in sp.mcp_servers]
            if decl.type == "stdio":
                what = " ".join([decl.command, *decl.args])
            else:
                what = decl.url
            spawn.add_row(name, decl.type, what, ", ".join(users) or "[dim]nobody[/dim]")
        console.print(spawn)
        console.print(
            "[dim]These run with this process's environment. A server's tools are "
            "allowed wholesale once an agent names it.[/dim]"
        )

    for mode, rm in sorted(cfg.run_modes.items()):
        filing = "" if rm.files_tickets else "  [dim](no filing)[/dim]"
        console.print(
            f"[bold]{mode}[/bold]: {', '.join(rm.agents)}  "
            f"[dim]{rm.max_wall_clock_s}s[/dim]{filing}"
        )

    if notes:
        console.print("\n[dim]notes:[/dim]")
        for note in notes:
            console.print(f"  [dim]{note}[/dim]")

    if problems:
        console.print("\n[red]problems:[/red]")
        for p in problems:
            console.print(f"  - {p}")
        raise typer.Exit(1)
    console.print("\n[green]config ok[/green]")


def _describe_writes(spec) -> str:
    bits = []
    if spec.policy.write_paths:
        bits.append("paths:" + ",".join(spec.policy.write_paths))
    if spec.policy.branch_patterns:
        bits.append("branch:" + ",".join(spec.policy.branch_patterns))
    if spec.policy.may_create_tickets:
        bits.append(f"tickets<={spec.policy.max_tickets_per_run}")
    if spec.policy.may_transition_tickets:
        bits.append("transition")
    if spec.policy.may_open_pr:
        bits.append("open-pr")
    return " ".join(bits)


# -- prompts ----------------------------------------------------------------
#
# A prompt is where an agent's judgement is set, and it is the first thing a
# real user wants to change. Before this group the only way to do that after a
# `pip install` was to edit site-packages: invisible to git, lost on the next
# upgrade, and impossible to diff. These three commands make the same edit a
# file in the project, and `diff` makes an upgrade's divergence visible instead
# of silent.

prompts_app = typer.Typer(
    add_completion=False,
    help="Inspect and override the agent prompts.",
    no_args_is_help=True,
)
app.add_typer(prompts_app, name="prompts")

#: The name `_shared.md` answers to on the command line.
SHARED_LABEL = "_shared"


def _prompt_origin(path: Path, ws: Workspace) -> str:
    """Which layer a resolved prompt came from, for a human reading a table."""
    parent = path.resolve().parent
    if parent.is_relative_to(package_root()):
        return "packaged"
    if ws.project and parent.is_relative_to(ws.project.resolve()):
        return "project"
    return "override"


def _eject_dir(ws: Workspace) -> Path:
    """Where `eject` writes. Never inside the installed package.

    Enforcement, not advice. `state_root` comes from QAAS_HOME, the project, or
    the cwd, and nothing else stops one of those from landing in site-packages
    -- where an edit would survive exactly until the next `pip install
    --upgrade` and then vanish with no trace of ever having been made.
    """
    dest = (ws.state_root / "prompts").resolve()
    if dest.is_relative_to(package_root()):
        console.print(
            f"[red]refusing to write inside the installed package:[/red] {dest}\n"
            "[dim]run this from your project, or set QAAS_HOME.[/dim]"
        )
        raise typer.Exit(1)
    return dest


def _prompt_index(cfg) -> dict[str, str]:
    """Name -> prompt filename, for every agent plus the shared house rules."""
    index = {name: spec.prompt for name, spec in sorted(cfg.agents.items())}
    from qaas.registry import SHARED_PROMPT

    index[SHARED_LABEL] = SHARED_PROMPT
    return index


def _select_prompts(cfg, agent: str | None) -> list[tuple[str, str]]:
    """Resolve a command-line name to (label, filename) pairs. Everything if None."""
    index = _prompt_index(cfg)
    if agent is None:
        return list(index.items())
    wanted = agent[:-3] if agent.endswith(".md") else agent
    for label, filename in index.items():
        if label.lower() == wanted.lower():
            return [(label, filename)]
    console.print(
        f"[red]unknown prompt '{agent}'[/red] — known: {', '.join(index)}"
    )
    raise typer.Exit(1)


@prompts_app.command("list")
def prompts_list(config_dir: Path | None = ConfigDir) -> None:
    """Show which prompt file each agent is actually given, and from where."""
    from qaas.registry import SHARED_PROMPT, append_name, append_paths, build_system_prompt

    cfg = load_config(config_dir)
    ws = Workspace.resolve()

    table = Table(title="Prompts in force", header_style="bold")
    for col in ("agent", "file", "source", "appended", "total chars"):
        table.add_column(col)
    for name, spec in sorted(cfg.agents.items()):
        found = ws.prompt_file(spec.prompt)
        if found is None:
            table.add_row(name, spec.prompt, "[red]missing[/red]", "-", "-")
            continue
        appends = append_paths(ws.prompt_dirs, spec.prompt)
        table.add_row(
            name,
            spec.prompt,
            _prompt_origin(found, ws),
            append_name(spec.prompt) if appends else "-",
            str(len(build_system_prompt(spec, ws.prompt_dirs))),
        )
    shared = ws.prompt_file(SHARED_PROMPT)
    table.add_row(
        "[dim]every agent[/dim]",
        SHARED_PROMPT,
        _prompt_origin(shared, ws) if shared else "[red]missing[/red]",
        "-",
        str(len(shared.read_text(encoding="utf-8"))) if shared else "-",
    )
    console.print(table)

    for i, d in enumerate(ws.prompt_dirs):
        console.print(f"[dim]{'*' if i == 0 else ' '} {d}[/dim]")
    console.print(
        "\n[dim]`qaas prompts eject <AGENT>` to edit one outright, or drop a "
        "`<AGENT>.append.md` beside it to add lines without forking the file.[/dim]"
    )


@prompts_app.command("eject")
def prompts_eject(
    agent: str = typer.Argument(None, help=f"Agent name, or {SHARED_LABEL}. Omit with --all."),
    all_prompts: bool = typer.Option(False, "--all", help="Eject every prompt."),
    force: bool = typer.Option(False, "--force", help="Overwrite a file already there."),
    config_dir: Path | None = ConfigDir,
) -> None:
    """Copy a packaged prompt into the project so it can be edited."""
    if agent is None and not all_prompts:
        console.print("[red]name an agent, or pass --all[/red]")
        raise typer.Exit(1)
    if agent is not None and all_prompts:
        console.print("[red]name an agent or pass --all, not both[/red]")
        raise typer.Exit(1)

    cfg = load_config(config_dir)
    ws = Workspace.resolve()
    dest_dir = _eject_dir(ws)
    selected = _select_prompts(cfg, agent)

    written: list[Path] = []
    skipped: list[Path] = []
    for _label, filename in selected:
        # The *packaged* bytes, deliberately: eject means "give me the house
        # version to edit". Copying whatever already won the search would make
        # a second eject a no-op that looks like it did something.
        src = packaged_prompts() / filename
        if not src.is_file():
            console.print(f"[red]nothing to eject: {src} does not exist[/red]")
            raise typer.Exit(1)
        dest = dest_dir / filename
        if dest.exists() and not force:
            skipped.append(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"))
        written.append(dest)

    for path in written:
        console.print(f"[green]wrote[/green] {path}")
    for path in skipped:
        console.print(f"[yellow]exists, left alone:[/yellow] {path}")

    if skipped and not written:
        console.print("[dim]use --force to overwrite.[/dim]")
    # One named prompt that was refused is a failed command; --all skipping the
    # files you already ejected is the normal, successful case.
    if skipped and agent is not None:
        raise typer.Exit(1)
    if written:
        console.print(
            "\n[dim]edit them, then `qaas prompts diff` to see what you changed.[/dim]"
        )


@prompts_app.command("diff")
def prompts_diff(
    agent: str = typer.Argument(None, help="Agent name, or omit for all of them."),
    config_dir: Path | None = ConfigDir,
) -> None:
    """Show local prompt edits against the bytes that shipped.

    Run it after an upgrade: a forked prompt does not conflict, it just quietly
    stops tracking the package, and this is the only place that shows it.
    """
    import difflib

    from qaas.registry import append_name, append_paths

    cfg = load_config(config_dir)
    ws = Workspace.resolve()
    changed = 0

    for label, filename in _select_prompts(cfg, agent):
        in_force = ws.prompt_file(filename)
        packaged = packaged_prompts() / filename
        # Only agent prompts take an addendum; `_shared.append.md` is composed
        # by nothing, so reporting one would describe a file that has no effect.
        appends = [] if label == SHARED_LABEL else append_paths(ws.prompt_dirs, filename)

        if in_force is not None and packaged.is_file() and in_force != packaged.resolve():
            diff = list(
                difflib.unified_diff(
                    packaged.read_text(encoding="utf-8").splitlines(),
                    in_force.read_text(encoding="utf-8").splitlines(),
                    fromfile=f"packaged/{filename}",
                    tofile=str(in_force),
                    lineterm="",
                )
            )
            if diff:
                changed += 1
                console.print(f"\n[bold]{label}[/bold]")
                for line in diff:
                    style = None
                    if line.startswith("+") and not line.startswith("+++"):
                        style = "green"
                    elif line.startswith("-") and not line.startswith("---"):
                        style = "red"
                    elif line.startswith("@@"):
                        style = "cyan"
                    console.print(line, style=style, markup=False, highlight=False)

        for path in appends:
            changed += 1
            console.print(f"\n[bold]{label}[/bold] [dim]+ {append_name(filename)}[/dim]")
            console.print(f"--- {path}", markup=False, highlight=False)
            for line in path.read_text(encoding="utf-8").splitlines():
                console.print(f"+{line}", style="green", markup=False, highlight=False)

    if not changed:
        console.print("[dim]no local prompt edits — every prompt is the packaged one[/dim]")


@app.command()
def runs(root: Path = Root, limit: int = 10) -> None:
    """List recent runs with their cost and finding count."""
    ids = list_runs(root)[:limit]
    if not ids:
        console.print("[dim]no runs yet[/dim]")
        return
    table = Table(header_style="bold")
    for col in ("run", "envelopes", "agents"):
        table.add_column(col)
    for run_id in ids:
        store = RunStore(run_id, root, create=False)
        results = store.results()
        table.add_row(
            run_id,
            str(len(store.envelopes())),
            str(len(results)),
        )
    console.print(table)


#: A verdict is the answer to "did the fix work"; colour it like one.
VERDICT_STYLE = {
    "VERIFIED": "green",
    "NOT_FIXED": "yellow",
    "REGRESSED": "red",
}


@app.command()
def show(run_id: str, root: Path = Root) -> None:
    """Show one run's findings and ledger: cost, duration, tickets, escalations."""
    store = RunStore(run_id, root, create=False)
    # One pass over the ledger, shared by the header and the denial list -- each
    # `store.ledger(kind)` call is a full-file scan of a file that reaches tens
    # of thousands of lines on a real run.
    entries = trace_mod.read_ledger(store)
    summary = trace_mod.summarise(store, entries)

    console.print(f"[bold]{summary.run_id}[/bold]")
    header = [f"mode {summary.mode or '?'}"]
    if summary.started:
        header.append(f"started {summary.started:%Y-%m-%d %H:%M:%S}Z")
    if summary.duration_s is not None:
        header.append(f"duration {summary.duration_s:.0f}s")
    console.print("  " + "  ".join(header))
    if summary.target_sha:
        dirty = " [yellow](dirty tree)[/yellow]" if summary.target_dirty else ""
        console.print(f"  target {summary.target_sha[:12]}{dirty}")
    else:
        # Not a warning: `environment.mode: none` targets and non-git checkouts
        # are supported, and runs recorded before this was added have no sha.
        console.print("  [dim]target commit not recorded[/dim]")
    if not summary.completed:
        console.print("  [yellow]no run_finished — this run did not complete[/yellow]")
    if summary.stopped_early:
        console.print(f"  [yellow]stopped early: {summary.stopped_early}[/yellow]")

    envelopes = store.envelopes()
    if envelopes:
        console.print(f"\n[bold]findings ({len(envelopes)})[/bold]")
    for env in envelopes:
        ok, reason = env.is_fileable()
        gate = "[green]fileable[/green]" if ok else f"[yellow]held: {reason}[/yellow]"
        console.print(
            f"[bold]{env.severity.value:8s}[/bold] {env.domain.value:12s} "
            f"{env.title}  [dim]({env.discovered_by}, conf {env.confidence:.2f})[/dim]  {gate}"
        )

    if summary.tickets:
        console.print(f"\n[bold]tickets filed ({len(summary.tickets)})[/bold]")
        for key, verdict in summary.tickets.items():
            if verdict is None:
                console.print(f"  {key}  [dim]no verdict[/dim]")
            else:
                style = VERDICT_STYLE.get(verdict, "white")
                console.print(f"  {key}  [{style}]{verdict}[/{style}]")

    if summary.escalations:
        console.print(f"\n[bold red]escalations ({len(summary.escalations)})[/bold red]")
        for reason in summary.escalations:
            console.print(f"  {reason}")

    denials = [e for e in entries if e.kind == "denial"]
    if denials:
        console.print(f"\n[bold]guardrail denials ({len(denials)})[/bold]")
        for d in denials:
            console.print(f"  {d.agent}: {d.detail.get('tool')} — {d.detail.get('reason')}")

    console.print(f"\n[dim]{len(entries)} ledger entries — qaas trace {run_id}[/dim]")


def _follow(store, *, agent: str | None, kinds, as_json: bool, quiet: bool = False) -> None:
    """Stream a live run's ledger until it finishes or the operator stops.

    Deliberately line-by-line rather than a redrawn table: a run lasts minutes,
    the interesting lines are denials and verdicts, and they must survive being
    scrolled past, piped, and pasted into a bug report. A `rich.Live` view that
    repaints would lose all three.
    """
    from qaas.store import LedgerKind

    wanted = set(kinds) if kinds else None
    name = agent.upper() if agent else None
    console.print(f"[dim]following {store.run_id} — ctrl-c to stop[/dim]")
    hidden = trace_mod.QUIET_KINDS if quiet else frozenset()
    seen = 0
    try:
        for entry in trace_mod.tail(store):
            if entry.kind in hidden:
                continue
            if wanted is not None and entry.kind not in wanted:
                continue
            if name is not None and (entry.agent or "").upper() != name:
                continue
            seen += 1
            if as_json:
                # Newline-delimited, flushed per line: `--follow --json` exists
                # to be piped into something that reacts, and a buffered pipe
                # that only speaks at the end is not a live feed.
                typer.echo(json.dumps(entry.model_dump(mode="json")), nl=True)
                sys.stdout.flush()
                continue
            style = KIND_STYLE.get(str(entry.kind), "white")
            console.print(
                f"[dim]{entry.at.strftime('%H:%M:%S')}[/dim] "
                f"[cyan]{(entry.agent or '-'):<12}[/cyan] "
                f"[{style}]{str(entry.kind):<16}[/] {trace_mod.describe(entry)}"
            )
            if entry.kind == LedgerKind.RUN_FINISHED:
                console.print("[dim]run finished[/dim]")
    except KeyboardInterrupt:
        console.print(f"\n[dim]stopped following after {seen} entries[/dim]")


@app.command()
def trace(
    run_id: str,
    root: Path = Root,
    agent: str | None = typer.Option(None, "--agent", "-a", help="Only this agent's entries."),
    kind: list[str] = typer.Option(None, "--kind", "-k", help="Only these ledger kinds (repeatable)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the filtered entries as JSON."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new entries as the run produces them."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Drop tool_call lines and show only what an agent decided."),
) -> None:
    """Print one run's ledger as a timeline: dispatches, tools, denials, verdicts, cost."""
    store = RunStore(run_id, root, create=False)
    if not store.ledger_path.exists() and not follow:
        console.print(f"[red]no ledger for run {run_id}[/red] — try `qaas runs`")
        raise typer.Exit(1)

    try:
        kinds = trace_mod.parse_kinds(kind or [])
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None

    if follow:
        _follow(store, agent=agent, kinds=kinds, as_json=as_json, quiet=quiet)
        return

    entries = trace_mod.select(trace_mod.read_ledger(store), agent=agent, kinds=kinds, quiet=quiet)

    if as_json:
        # Plain stdout, not `console.print_json`: rich soft-wraps at the console
        # width, which puts newlines inside long string values and hands the
        # caller JSON that no parser will accept. `--json` exists to be piped.
        typer.echo(json.dumps([e.model_dump(mode="json") for e in entries], indent=2))
        return

    if not entries:
        console.print("[dim]no ledger entries match[/dim]")
        return

    table = Table(header_style="bold", box=None, pad_edge=False)
    table.add_column("t+", justify="right", style="dim")
    table.add_column("agent", style="cyan")
    table.add_column("kind")
    table.add_column("detail", overflow="fold")
    for row in trace_mod.timeline(entries):
        label = f"{row.kind} ×{row.count}" if row.count > 1 else row.kind
        table.add_row(
            f"{row.offset_s:.0f}s",
            row.agent,
            f"[{KIND_STYLE.get(row.kind, 'white')}]{label}[/]",
            row.detail,
        )
    console.print(table)
    console.print(f"\n[dim]{len(entries)} entries[/dim]")


@app.command()
def map(root: Path = Root, version: str | None = None) -> None:
    """Show the system map Mapper produced."""
    maps = SystemMapStore(root)
    payload = maps.get(version)
    if payload is None:
        console.print("[dim]no system map yet — run MAPPER[/dim]")
        raise typer.Exit(1)
    console.print(f"[bold]version[/bold] {version or maps.latest_version()}")
    console.print_json(data=payload)


def _start_dashboard(root: Path, config_dir: Path | None) -> str | None:
    """Bring the dashboard up *before* the first agent dispatches.

    The same reasoning as the Jira board above: a view that appears once the run
    is over is a report, not something to watch it on. It never fails a run --
    the findings matter more than the view -- so a missing `[ui]` extra or a
    busy port is a line of output, not an exit.
    """
    from qaas.ui import MissingUIExtra
    from qaas.ui.serve import build, free_port, serve_in_background

    try:
        app_ = build(root, **_dashboard_kwargs(config_dir))
        port = free_port("127.0.0.1", DEFAULT_PORT)
    except (MissingUIExtra, OSError) as exc:
        console.print(f"[yellow]dashboard not started: {exc}[/yellow]")
        return None
    serve_in_background(app_, "127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/"
    console.print(f"[dim]dashboard:[/dim] {url}")
    return url


@app.command()
def dashboard(
    run_id: str = typer.Argument(None, help="Run to open. Defaults to the live run, else the newest."),
    config_dir: Path | None = ConfigDir,
    root: Path = Root,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Interface to bind. Leave it on localhost: the ledger carries agent task "
        "previews, refused command lines and target paths.",
    ),
    port: int = typer.Option(DEFAULT_PORT, "--port", help="Port to bind; the next few are tried if it is taken."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser at the URL."),
) -> None:
    """Watch a run in a browser: agents, phases, findings, refusals, cost.

    Read-only. It shows a run; it cannot start one.
    """
    from qaas.ui import MissingUIExtra
    from qaas.ui.serve import build, free_port, open_browser as launch, serve
    from qaas.ui.state import pick_run

    chosen = pick_run(root, run_id)
    if chosen is None:
        console.print("[dim]no runs yet — qaas run --mode pr-check[/dim]")
        raise typer.Exit(1)

    try:
        app_ = build(root, **_dashboard_kwargs(config_dir))
    except MissingUIExtra as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(1) from None

    bound = free_port(host, port)
    url = f"http://{host}:{bound}/#{chosen}"
    live = "[green]live[/green]" if _is_live(root, chosen) else "[dim]finished[/dim]"
    console.print(f"[bold]qaas dashboard[/bold]  {url}")
    console.print(f"[dim]showing[/dim] {chosen} ({live})  [dim]ctrl-c to stop[/dim]")
    if open_browser:
        launch(url)
    try:
        serve(app_, host, bound)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass


def _is_live(root: Path, run_id: str) -> bool:
    from qaas.ui.state import is_live

    return is_live(RunStore(run_id, root=root, create=False))


def _dashboard_kwargs(config_dir: Path | None) -> dict:
    """Agent specs and the golden ledger, when a config can be loaded.

    A project with no config is a legitimate state -- `pip install` then `qaas
    dashboard` in a directory holding only `.qaas/runs/` should still open. The
    specs only enrich the view with each agent's layer and model; without them
    the grid still renders, exactly as it does for a run naming agents from a
    roster that no longer exists.
    """
    try:
        cfg = load_config(config_dir)
    except Exception:
        return {}
    ledger = _ledger_path(cfg)
    return {
        "specs": dict(cfg.agents),
        "min_confidence": cfg.thresholds.min_confidence_to_file,
        "ledger_path": ledger if ledger and ledger.exists() else None,
    }


@app.command()
def run(
    mode: str = typer.Option(..., "--mode", "-m", help="Run mode from system.yaml."),
    config_dir: Path | None = ConfigDir,
    root: Path = Root,
    only: list[str] = typer.Option(None, "--only", help="Restrict the run to these agents."),
    target: str = typer.Option(None, "--target", "-t", help="Target profile to run against. Overrides system.yaml."),
    repo: str = typer.Option(None, "--repo", help="A local path or git URL to run against directly. Clones and profiles it if needed."),
    clone_to: Path = typer.Option(None, "--clone-to", help="Where to clone, for a git URL. Default: <state>/targets/."),
    run_id: str = typer.Option(None, "--run-id", help="Continue an existing run rather than starting one."),
    ticket: list[str] = typer.Option(None, "--ticket", help="Restrict a fix-cycle to these tickets."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render the plan without calling the API."),
    dashboard_: bool = typer.Option(
        False, "--dashboard", help="Serve the live dashboard alongside the run."
    ),
    force: bool = typer.Option(False, "--force", help="With --repo: regenerate the target profile instead of reusing it."),
) -> None:
    """Execute a run. Costs real money unless --dry-run."""
    import asyncio

    from qaas.router import Router
    from qaas.registry import describe

    if repo and target:
        console.print("[red]--repo and --target name two different targets.[/red] Pass one.")
        raise typer.Exit(1)

    # `--repo` is sugar over `--target`, not a second way to run. It provisions
    # a profile the same way `qaas init` does and then falls into the ordinary
    # path, so a URL gets exactly the guardrails, readiness checks and target
    # root that a hand-written profile gets. It deliberately does NOT rewrite
    # system.yaml: a one-off run against someone else's repository is not a
    # decision to repoint the whole installation at it.
    #
    # Before the config load, because provisioning is what decides which target
    # this run is about -- and a stale `target:` in system.yaml naming a profile
    # that no longer exists would otherwise kill the run inside `load_config`,
    # before the override just typed on the command line was ever read.
    profile = None
    if repo:
        profile, target, path, notes, wrote = _provision_target(
            repo,
            clone_to=clone_to,
            config_dir=config_dir,
            force=force,
            reuse_existing=True,
        )
        for note in notes:
            console.print(f"[yellow]note:[/yellow] {note}")
        console.print(
            f"[green]wrote {path}[/green]" if wrote
            else f"[dim]reusing the existing profile at {path} (--force to regenerate)[/dim]"
        )

    cfg = load_config(config_dir, target=target)
    if target:
        # The profile object we already hold, rather than a second lookup by
        # name: `_provision_target` may have written into the writable config
        # layer (`.qaas/config/targets/`) while `load_config` resolves profiles
        # from a different one, and this run must be about the repository the
        # operator named, not a same-named profile from another layer.
        cfg = cfg.model_copy(
            update={
                "target": target,
                "profile": profile or _load_target(target, config_dir),
            }
        )
    if cfg.profile:
        problems = cfg.profile.readiness()
        blocking = [p for p in problems if "does not exist" in p or "not a directory" in p]
        if blocking:
            console.print(f"[red]target '{cfg.target}' is not usable:[/red]")
            for p in blocking:
                console.print(f"  - {p}")
            raise typer.Exit(1)
        for p in problems:
            console.print(f"[yellow]warning:[/yellow] {p}")
        console.print(f"[dim]target: {cfg.target} ({cfg.profile.environment.mode})[/dim]")
    if only:
        wanted = {a.upper() for a in only}
        unknown = wanted - set(cfg.agents)
        if unknown:
            console.print(f"[red]unknown agents: {', '.join(sorted(unknown))}[/red]")
            raise typer.Exit(1)
        mode_cfg = cfg.run_modes[mode]
        cfg = cfg.model_copy(
            update={
                "run_modes": {
                    **cfg.run_modes,
                    mode: mode_cfg.model_copy(
                        update={"agents": [a for a in mode_cfg.agents if a in wanted]}
                    ),
                }
            }
        )
    specs = cfg.enabled_agents(mode)
    rm = cfg.run_modes[mode]
    console.print(
        f"[bold]{mode}[/bold] — {len(specs)} agents, "
        f"concurrency {rm.max_concurrency}"
    )

    if dry_run:
        # The same search path the run itself would use, so `prompt: N chars`
        # counts any override rather than always reporting the packaged bytes.
        prompt_dirs = Workspace.resolve().prompt_dirs
        for spec in specs:
            d = describe(spec, prompt_dirs)
            console.print(
                f"  [bold]{spec.name:14s}[/bold] {spec.model:18s} effort={spec.effort:7s} "
                f"turns<={spec.max_turns}"
            )
            console.print(f"    tools: {', '.join(d['allowed_tools'])}")
            console.print(f"    prompt: {d['prompt_chars']} chars")
        return

    # Before the first agent, not after the first ticket: the board is what
    # someone watches a run *on*, and one that appears at the end is a report.
    board_info = _ensure_board(cfg)
    dash_url = _start_dashboard(root, config_dir) if dashboard_ else None

    def on_event(kind: str, detail: dict) -> None:
        if kind == "agent_started":
            console.print(f"[dim]->[/dim] {detail.get('agent')}")
        elif kind == "finished":
            console.print(
                f"[dim]<-[/dim] {detail.get('agent')} "
                f"[dim]{detail.get('envelopes', 0)} findings[/dim]"
            )
        elif kind == "stopped":
            console.print(f"[yellow]stopped: {detail.get('reason')}[/yellow]")

    router = Router(cfg, root=root, on_event=on_event, tickets=list(ticket) if ticket else None)
    report = asyncio.run(router.run(mode, run_id=run_id))

    console.print()
    console.print_json(data=report.summary())
    console.print(f"\n[dim]watch it back:[/dim] qaas trace {report.run_id}")
    if dash_url:
        console.print(f"[dim]dashboard:[/dim] {dash_url}#{report.run_id}")
    if board_info is not None and board_info.url:
        console.print(f"[dim]board:[/dim] {board_info.url}")
    if report.failed or report.stopped_early:
        raise typer.Exit(1)


@app.command()
def score(
    run_id: str = typer.Argument(None, help="Run to score. Defaults to the most recent."),
    config_dir: Path | None = ConfigDir,
    root: Path = Root,
    phase: int = typer.Option(1, help="Score against defects seeded for this phase and earlier."),
    domains: list[str] = typer.Option(
        None, "--domain", help="Restrict scoring to these domains. Use it when a run covered only part of the surface."
    ),
    target: str = typer.Option(
        None, "--target", "-t", help="Score against this profile's ledger rather than the active one's."
    ),
) -> None:
    """Score a run against the golden ledger. This is the honest number."""
    from qaas.scorecard import GoldenLedger, score as score_run

    cfg = load_config(config_dir)
    # Every other command that reads a run takes `--target`; this one did not,
    # so a `--repo` run was scored against whatever profile `system.yaml`
    # happened to name -- in practice the bundled demo's ledger, which describes
    # a different application entirely. Recall and precision against the wrong
    # oracle are worse than no number, because they look like a number.
    if target:
        cfg = cfg.model_copy(update={"target": target, "profile": _load_target(target, config_dir)})
    ledger_path = _ledger_path(cfg)
    if ledger_path is None or not ledger_path.exists():
        console.print(
            "[yellow]this target has no golden ledger, so there is nothing to score against.[/yellow]\n"
            "[dim]A golden ledger lists known defects with their expected domain and severity, and\n"
            "`qaas score` measures recall and precision against it. It is a property of a\n"
            "calibration target, not of an ordinary application -- most targets will never\n"
            "have one. Set `ledger:` in the target profile if yours does; the bundled demo app\n"
            "ships in the project's git repository, not in the wheel.[/dim]"
        )
        raise typer.Exit(1)

    if run_id is None:
        ids = list_runs(root)
        if not ids:
            console.print("[dim]no runs to score[/dim]")
            raise typer.Exit(1)
        run_id = ids[0]

    store = RunStore(run_id, root, create=False)
    card = score_run(
        store.envelopes(),
        GoldenLedger.load(ledger_path),
        phase=phase,
        domains=set(domains) if domains else None,
        cost_usd=store.total_cost_usd(),
    )
    s = card.summary()

    console.print(f"[bold]{run_id}[/bold]")
    table = Table(header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("found", f"{s['found']} of {s['of']}")
    table.add_row("recall", f"{s['recall']:.0%}")
    table.add_row("precision", f"{s['precision']:.0%}")
    table.add_row("false positives", f"{s['false_positives']} ({s['false_positive_rate']:.0%})")
    table.add_row("duplicates", f"{s['duplicates']} ({s['duplicate_rate']:.0%})")
    table.add_row("severity agreement", f"{s['severity_agreement']:.0%}")
    console.print(table)

    if card.matches:
        console.print("\n[bold]found[/bold]")
        for m in card.matches:
            flag = "" if abs(m.severity_delta) <= 1 else f"  [yellow]severity off by {abs(m.severity_delta)}[/yellow]"
            console.print(f"  [green]{m.golden_id}[/green] (match {m.score}){flag}")
    if card.missed:
        console.print(f"\n[bold]missed[/bold]: {', '.join(card.missed)}")
    if card.regressions_on_planted:
        console.print("\n[red]reported deliberately-correct behaviour as a defect[/red]")
        for env_id, planted in card.regressions_on_planted:
            console.print(f"  {planted}  [dim]({env_id})[/dim]")


@app.command()
def sweep(
    mode: str = typer.Option("nightly", "--mode", "-m"),
    config_dir: Path | None = ConfigDir,
    root: Path = Root,
    min_precision: float = typer.Option(
        0.70, help="Quality gate. §11 stops the rollout below 70% accepted."
    ),
) -> None:
    """Run, then score, then gate. This is the command to put in cron.

    Exits non-zero when precision falls below the gate, so a scheduled sweep
    that starts producing noise fails loudly instead of quietly filling a
    backlog nobody reads.
    """
    import asyncio

    from qaas.router import Router
    from qaas.scorecard import GoldenLedger, score as score_run

    cfg = load_config(config_dir)
    router = Router(cfg, root=root)
    report = asyncio.run(router.run(mode))
    console.print_json(data=report.summary())

    ledger_path = _ledger_path(cfg)
    if ledger_path is None or not ledger_path.exists():
        console.print("[yellow]no golden ledger for this target; ran without scoring[/yellow]")
        return

    store = RunStore(report.run_id, root)
    card = score_run(
        store.envelopes(), GoldenLedger.load(ledger_path), cost_usd=store.total_cost_usd()
    )
    console.print_json(data=card.summary())

    if card.precision < min_precision:
        console.print(
            f"[red]precision {card.precision:.0%} is below the {min_precision:.0%} gate[/red] — "
            "tune before adding agents (§11)"
        )
        raise typer.Exit(1)
    console.print(f"[green]precision {card.precision:.0%}, above the gate[/green]")


# -- tracker-check ----------------------------------------------------------
# Everything below exists so that the first live run is not also the first time
# anyone finds out whether the configuration works. It makes read-only calls
# only: an operator must be able to run it against the team's real board
# without wondering what it left behind.

#: Every environment variable the Jira backend reads, and what breaks without
#: it. The order is the order they are needed in.
_JIRA_ENV_HELP: dict[str, str] = {
    "JIRA_BASE_URL": "site root, e.g. https://acme.atlassian.net (no /jira, no /rest path)",
    "JIRA_EMAIL": "the bot account's Atlassian email — the one the token was minted for",
    "JIRA_API_TOKEN": "an API token, not a password",
    "JIRA_PROJECT_KEY": "default project for ordinary findings",
    "JIRA_SECURITY_PROJECT_KEY": "restricted project; without it security findings are refused",
    "JIRA_ISSUE_TYPE": "issue type to create (default: Bug)",
}

#: Never rendered, ever, in any form but a four-character tail.
_JIRA_SECRET_ENV = frozenset({"JIRA_API_TOKEN"})

#: House statuses this system actually drives. An unmapped one here is a real
#: failure: VERIFIER asks for 'closed', nothing in the workflow matches, and the
#: ticket stays open while the run reports a clean close. The rest of `STATUSES`
#: are human dispositions — worth reporting, not worth failing on.
_DRIVEN_STATUSES = ("open", "in_progress", "resolved", "closed")

#: What `--dry-run-ticket` renders. A realistic TRIAGE ticket rather than a
#: placeholder, because the point is to see the ADF, the labels and the
#: fingerprint an engineer will actually receive.
_SAMPLE_TICKET: dict[str, object] = {
    "title": "Refund endpoint accepts any authenticated user",
    "body": (
        "## Repro\n"
        "\n"
        "- authenticate as an ordinary customer account\n"
        "- POST /v1/orders/{order_id}/refund for an order owned by a different account\n"
        "\n"
        "```bash\n"
        "curl -X POST -H \"Authorization: Bearer $CUSTOMER_TOKEN\" \\\n"
        "  https://api.example.com/v1/orders/9001/refund\n"
        "```\n"
        "\n"
        "## Impact\n"
        "\n"
        "Any authenticated user can refund any order. Money moves.\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        "- the endpoint returns 403 when the caller does not own the order\n"
        "- a regression test covers the cross-account case\n"
    ),
    "labels": ["agent-found"],
    "severity": "critical",
    "envelope_id": "env-sample-0001",
    "fingerprint": "sha256:" + "ab12cd34" * 8,
    "reporter": "TRIAGE",
}


def _env_display(name: str, value: str | None, required: tuple[str, ...]) -> str:
    """One environment row. A credential's value never appears here.

    The last four characters of a token are enough to tell two tokens apart
    when you have both in front of you, and useless to anyone who does not.
    """
    text = (value or "").strip()
    if not text:
        return "[red]MISSING[/red]" if name in required else "[dim]unset[/dim]"
    if name in _JIRA_SECRET_ENV:
        if len(text) < 12:
            return "[green]set[/green] (too short to show a tail safely)"
        return f"[green]set[/green] (ends ...{text[-4:]})"
    return f"[green]set[/green] ({text})"


def _print_checks(rows: list[tuple[str, bool, str]]) -> None:
    table = Table(title="connection", header_style="bold")
    table.add_column("check")
    table.add_column("", justify="center")
    table.add_column("detail")
    for label, passed, detail in rows:
        table.add_row(label, "[green]ok[/green]" if passed else "[red]fail[/red]", detail)
    console.print(table)


def _check_jira_project(
    tracker, key: str, role: str
) -> tuple[list[tuple[str, bool, str]], list[str], list[Table]]:
    """Verify one project: it exists, this account may write to it, the issue
    type is available, and the house statuses map onto its workflow.

    Returns its rows, its problems and any table to render, rather than
    printing, so the caller controls the order the report reads in.
    """
    from qaas.adapters.tracker import TrackerError

    rows: list[tuple[str, bool, str]] = []
    problems: list[str] = []
    tables: list[Table] = []

    try:
        info = tracker.project_info(key)
    except TrackerError as exc:
        rows.append((f"project {key}", False, str(exc)))
        problems.append(f"the {role} project '{key}' could not be read. {exc}")
        return rows, problems, tables
    rows.append((f"project {key}", True, f"{info.get('name') or '?'} ({role})"))

    try:
        held = tracker.project_permissions(key)
    except TrackerError as exc:
        rows.append((f"permissions {key}", False, str(exc)))
        problems.append(f"could not read this account's permissions on '{key}'. {exc}")
    else:
        lacking = [name for name, granted in held.items() if not granted]
        rows.append(
            (
                f"permissions {key}",
                not lacking,
                "Browse, Create, Transition, Link"
                if not lacking
                else f"missing: {', '.join(lacking)}",
            )
        )
        if lacking:
            problems.append(
                f"the account lacks {', '.join(lacking)} on '{key}'. Grant them to this "
                "account's project role, or point the key at a project where it has them; "
                "Browse reads an issue back, Create files, Transition closes, Link dedupes."
            )

    try:
        statuses = tracker.project_statuses(key)
    except TrackerError as exc:
        rows.append((f"workflow {key}", False, str(exc)))
        problems.append(f"could not read the workflow of '{key}'. {exc}")
        return rows, problems, tables

    wanted = tracker.issue_type.strip().lower()
    matched = next((name for name in statuses if name.strip().lower() == wanted), None)
    if matched is None:
        rows.append(
            (
                f"issue type {key}",
                False,
                f"'{tracker.issue_type}' not in {', '.join(sorted(statuses)) or 'none'}",
            )
        )
        problems.append(
            f"'{key}' has no issue type called '{tracker.issue_type}'. It offers: "
            f"{', '.join(sorted(statuses)) or 'nothing this account can see'}. Set "
            "JIRA_ISSUE_TYPE to one of those."
        )
        return rows, problems, tables
    rows.append((f"issue type {key}", True, f"'{matched}' exists in {key}"))

    mapping = tracker.map_house_statuses(statuses[matched])
    table = Table(title=f"workflow — {key} / {matched}", header_style="bold")
    table.add_column("house status")
    table.add_column("maps onto")
    for house, target in mapping.items():
        driven = house in _DRIVEN_STATUSES
        if target:
            table.add_row(house, target)
        else:
            table.add_row(
                f"[red]{house}[/red]" if driven else house,
                "[red]no match[/red]" if driven else "[yellow]no match[/yellow]",
            )
    tables.append(table)

    unmapped = [h for h in _DRIVEN_STATUSES if mapping[h] is None]
    if unmapped:
        problems.append(
            f"'{key}' has no status matching the house status(es) {', '.join(unmapped)}. "
            "A transition to one of those will fail at the moment a ticket should close, "
            f"which is the point at which nobody is watching. This project's statuses are: "
            f"{', '.join(statuses[matched]) or 'none'}. Either rename a workflow status, or "
            "use a project whose workflow speaks these words."
        )
    return rows, problems, tables


def _tracker_check_jira(dry_run_ticket: bool) -> tuple[list[str], list[str]]:
    """Every read-only Jira check. Returns (problems, warnings)."""
    import os

    from qaas.adapters.tracker import JiraTracker, TrackerConfigError, TrackerError

    problems: list[str] = []
    warnings: list[str] = []

    table = Table(title="environment", header_style="bold")
    table.add_column("variable")
    table.add_column("status")
    table.add_column("what it does")
    for name, purpose in _JIRA_ENV_HELP.items():
        table.add_row(name, _env_display(name, os.environ.get(name), JiraTracker.REQUIRED_ENV), purpose)
    console.print(table)

    missing = [n for n in JiraTracker.REQUIRED_ENV if not (os.environ.get(n) or "").strip()]
    if missing:
        for name in missing:
            problems.append(f"{name} is unset or empty — {_JIRA_ENV_HELP[name]}. Export it.")
        problems.append(
            "nothing was contacted: the variables above are read at construction, so there "
            "was nothing to connect with. These are credentials — export them in the shell "
            "that runs qaas, never in config/, which is committed."
        )
        return problems, warnings

    try:
        tracker = JiraTracker()
    except TrackerConfigError as exc:
        problems.append(str(exc))
        return problems, warnings

    if tracker.security_project is None:
        warnings.append(
            "JIRA_SECURITY_PROJECT_KEY is unset. Security-relevant findings will be REFUSED "
            "rather than filed — deliberately, because a vulnerability in a project the "
            "company can read is a disclosure with no undo (§4.12, §10). They will be "
            "escalated to a human instead. Set it to a project with restricted visibility "
            "if you want them filed."
        )

    rows: list[tuple[str, bool, str]] = []
    try:
        who = tracker.whoami()
    except TrackerError as exc:
        rows.append(("auth", False, str(exc)))
        _print_checks(rows)
        problems.append(f"authentication failed, so no other check could run. {exc}")
        return problems, warnings

    account = who.get("displayName") or who.get("emailAddress") or "unknown account"
    rows.append(("auth", True, f"authenticated as {account}"))

    projects = [(tracker.default_project, "default")]
    if tracker.security_project:
        projects.append((tracker.security_project, "restricted"))

    # Collected before anything is printed so the connection summary comes
    # first and the workflow detail it refers to comes after it.
    workflows: list[Table] = []
    for key, role in projects:
        extra_rows, extra_problems, extra_tables = _check_jira_project(tracker, key, role)
        rows.extend(extra_rows)
        problems.extend(extra_problems)
        workflows.extend(extra_tables)
    _print_checks(rows)
    for workflow in workflows:
        console.print(workflow)

    if tracker.security_project:
        warnings.append(
            f"whether '{tracker.security_project}' is actually restricted cannot be checked "
            "over the API — Jira exposes no read-only view of a project's issue-level "
            "security scheme. Open it in a browser and confirm that people outside the "
            "security group cannot see its issues before filing anything real."
        )

    if dry_run_ticket:
        console.print("\n[bold]dry-run ticket[/bold] — this JSON would be POSTed to /rest/api/3/issue")
        payload = tracker.create_payload(project=tracker.default_project, **_SAMPLE_TICKET)  # type: ignore[arg-type]
        console.print_json(data=payload)
        console.print("[dim]nothing was sent.[/dim]")

    return problems, warnings


def _ensure_board(cfg, *, create: bool = True):
    """Find or create the Jira board for this run's target. Never fatal.

    Called at the top of every Jira-backed run so that a person told to "watch
    the board" has one to watch before the first ticket lands, rather than
    after. Returns a `BoardInfo`, or None when there is nothing to do or Jira
    could not be reached.

    Failures are printed and swallowed. A run that found nine defects and could
    not create a board has still done its job; a run that refused to start
    because of a board has thrown the findings away.
    """
    from qaas.adapters.tracker import JiraTracker, TrackerError, repo_label
    from qaas.mcp.tracker import dry_run_enabled

    if cfg.tracker != "jira" or not cfg.target:
        return None

    # The rehearsal rail sends nothing. A board is a container rather than a
    # ticket, but "QAAS_TRACKER_DRY_RUN=1 wrote to my Jira" is exactly the
    # sentence that rail exists to make impossible.
    if create and dry_run_enabled():
        console.print("[dim]dry run: no board was created[/dim]")
        create = False

    label = repo_label(cfg.target)
    if label is None:
        console.print(
            f"[yellow]note:[/yellow] target name '{cfg.target}' cannot be a Jira label, so "
            "tickets will not be grouped onto a per-repository board."
        )
        return None

    try:
        tracker = JiraTracker()
    except TrackerError as exc:
        console.print(f"[yellow]no board:[/yellow] {exc}")
        return None

    if not create:
        from qaas.adapters.tracker import BoardInfo

        return BoardInfo(
            slug=cfg.target,
            label=label,
            jql=f'project = "{tracker.default_project}" AND labels = "{label}" ORDER BY created DESC',
        )

    try:
        info = tracker.ensure_repo_board(cfg.target)
    except TrackerError as exc:
        console.print(f"[yellow]could not provision a board:[/yellow] {exc}")
        return None

    what = "board" if info.board_id and info.url != info.filter_url else "filter"
    verb = "created" if (info.created_board or info.created_filter) else "reused"
    console.print(f"[bold]{what} {verb}[/bold] — {info.url}")
    console.print(f"[dim]every ticket from this run carries the label {info.label}[/dim]")
    if info.note:
        console.print(f"[dim]{info.note}[/dim]")
    return info


@app.command()
def board(
    config_dir: Path | None = ConfigDir,
    target: str = typer.Option(None, "--target", "-t", help="Target profile. Defaults to the configured one."),
    create: bool = typer.Option(True, "--create/--no-create", help="Create the filter and board if they are missing."),
) -> None:
    """Show — or create — the Jira board that collects this repository's defects.

    One board per repository, inside one Jira project. The board is a saved
    filter over the label `repo-<target>`, which every ticket the system files
    carries. That is why it needs no administrator rights: creating a Jira
    *project* per repository does, creating a filter does not.
    """
    cfg = load_config(config_dir, target=target)
    if target:
        cfg = cfg.model_copy(update={"target": target, "profile": _load_target(target, config_dir)})

    if cfg.tracker != "jira":
        console.print(
            f"[yellow]tracker is '{cfg.tracker}', not 'jira'[/yellow] — boards are a Jira "
            "feature. Tickets are written to .qaas/tickets/ instead. Set QAAS_TRACKER=jira "
            "(and the JIRA_* variables) to file into Jira."
        )
        raise typer.Exit(1)
    if not cfg.target:
        console.print("[red]no target configured[/red] — run `qaas init <repo>` first.")
        raise typer.Exit(1)

    info = _ensure_board(cfg, create=create)
    if info is None:
        raise typer.Exit(1)

    table = Table(header_style="bold", box=None, pad_edge=False)
    table.add_column("field", style="dim")
    table.add_column("value", overflow="fold")
    table.add_row("target", cfg.target)
    table.add_row("label", info.label)
    table.add_row("jql", info.jql)
    table.add_row("filter", str(info.filter_id or "not created"))
    table.add_row("board", str(info.board_id or "not created"))
    table.add_row("url", info.url or "-")
    console.print(table)
    if info.note:
        console.print(f"\n[dim]{info.note}[/dim]")


@app.command("tracker-check")
def tracker_check(
    config_dir: Path | None = ConfigDir,
    root: Path = Root,
    dry_run_ticket: bool = typer.Option(
        False, "--dry-run-ticket", help="Also render the JSON that would be POSTed for a sample finding."
    ),
) -> None:
    """Validate the tracker configuration without creating anything.

    Read-only: it authenticates, reads the projects, their permissions and their
    workflows, and says what would break. Run it before the first live run, when
    the alternative is discovering a wrong project key by watching real tickets
    appear in front of real people.
    """
    cfg = load_config(config_dir)
    console.print(
        f"[bold]tracker backend[/bold]: {cfg.tracker}   "
        f"[dim](tracker: in {_system_yaml(config_dir)})[/dim]\n"
    )

    if cfg.tracker == "local":
        tickets = Path(root) / "tickets"
        count = len(list(tickets.glob("*.json"))) if tickets.is_dir() else 0
        console.print(f"tickets are written as JSON under [bold]{tickets}[/bold] ({count} so far)")
        console.print(
            "[dim]no credentials are needed and nothing leaves this machine. Read what it "
            "files there before switching to tracker: jira.[/dim]"
        )
        if dry_run_ticket:
            console.print(
                "\n[yellow]--dry-run-ticket renders the Jira REST payload[/yellow], which the "
                "local backend does not use — it writes the house Issue model straight to "
                "disk. Set tracker: jira to preview it."
            )
        console.print("\n[green]ready[/green]")
        return

    problems, warnings = _tracker_check_jira(dry_run_ticket)

    for warning in warnings:
        console.print(f"\n[yellow]warning:[/yellow] {warning}")
    if problems:
        console.print("\n[red]not ready:[/red]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(1)
    console.print("\n[green]ready[/green] — nothing was created by this check.")


if __name__ == "__main__":
    app()
