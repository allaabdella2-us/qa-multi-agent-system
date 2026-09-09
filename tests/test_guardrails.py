"""M3 verification: the §8.1 permission matrix is enforced, not requested.

These are the tests that matter most in the project. If an agent can write
outside its sandbox or push to main, nothing else here is safe to run.
"""

from pathlib import Path

import pytest

from support import CONFIG_SEARCH

from qaas.config import load_config
from qaas.guardrails import Guardrail
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[1]

#: What a guardrail is anchored on: the application under test, never the qaas
#: checkout. These used to pass `REPO` and paths like
#: `target-app/api/app/routes/orders.py`, which held only because the demo lives
#: inside this repository. Every path below is now target-relative, which is
#: what an agent actually types and what a policy's `write_paths` mean.
TARGET = REPO / "target-app"


@pytest.fixture
def guard_for(tmp_path):
    cfg = load_config(search=CONFIG_SEARCH)

    def build(agent_name: str) -> Guardrail:
        ctx = ToolContext(
            store=RunStore.new(root=tmp_path),
            maps=SystemMapStore(tmp_path),
            config=cfg,
            agent=cfg.agents[agent_name],
            target_root=TARGET,
        )
        return Guardrail(ctx)

    return build


# -- reading is always fine -------------------------------------------------


@pytest.mark.parametrize("agent", ["CARTOGRAPHER", "CONDUIT", "SURFACE", "FORGE", "CLERK", "PROOF"])
def test_every_agent_may_read_what_it_declared(guard_for, agent):
    g = guard_for(agent)
    for tool in g.agent.builtin_tools:
        if tool in {"Read", "Grep", "Glob"}:
            assert g.check(tool, {"file_path": "api/app/main.py"}).allowed


# -- the finder never fixes -------------------------------------------------


@pytest.mark.parametrize("agent", ["CARTOGRAPHER", "CONDUIT", "SURFACE"])
def test_discovery_agents_cannot_write_anywhere(guard_for, agent):
    g = guard_for(agent)
    d = g.check("Write", {"file_path": "api/app/routes/orders.py", "content": "x"})
    assert not d.allowed


def test_a_discovery_agent_is_told_why_not_just_no(guard_for):
    d = guard_for("CONDUIT").check("Write", {"file_path": "anything.py", "content": "x"})
    assert "read-only" in d.reason and "finder never fixes" in d.reason


# -- FORGE's sandbox --------------------------------------------------------


def test_forge_may_write_inside_its_sandbox(guard_for):
    assert guard_for("FORGE").check(
        "Write", {"file_path": "qa/repro/test_orders_limit.py", "content": "..."}
    ).allowed


def test_forge_may_not_write_to_product_code(guard_for):
    d = guard_for("FORGE").check(
        "Write", {"file_path": "api/app/routes/orders.py", "content": "..."}
    )
    assert not d.allowed and "outside" in d.reason


@pytest.mark.parametrize(
    "escape",
    [
        "qa/repro/../../etc/passwd",
        "../../../etc/passwd",
        "/etc/passwd",
        "qa/repro/../../../../tmp/x",
    ],
)
def test_path_traversal_out_of_the_sandbox_is_refused(guard_for, escape):
    assert not guard_for("FORGE").check("Write", {"file_path": escape, "content": "x"}).allowed


def test_edit_is_gated_the_same_way_as_write(guard_for):
    g = guard_for("FORGE")
    assert g.check("Edit", {"file_path": "qa/repro/a.py", "old_string": "a", "new_string": "b"}).allowed
    assert not g.check("Edit", {"file_path": "src/qaas/store.py", "old_string": "a", "new_string": "b"}).allowed


# -- bash --------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin qa/repro/x",
        "git push -f",
        "git push origin main",
        "git merge main",
        "git reset --hard HEAD~3",
        "git checkout main",
        "rm -rf /tmp/something",
        "sudo systemctl restart docker",
        "gh pr merge 42",
        "docker system prune -af",
    ],
)
def test_forbidden_commands_are_refused_for_everyone(guard_for, command):
    assert not guard_for("FORGE").check("Bash", {"command": command}).allowed


def test_forge_may_branch_inside_its_namespace(guard_for):
    assert guard_for("FORGE").check(
        "Bash", {"command": "git checkout -b qa/repro/api-01-limit"}
    ).allowed


def test_forge_may_not_branch_outside_its_namespace(guard_for):
    d = guard_for("FORGE").check("Bash", {"command": "git checkout -b fix/api-01"})
    assert not d.allowed and "outside" in d.reason


def test_an_agent_without_branch_patterns_cannot_touch_git_state(guard_for):
    d = guard_for("PROOF").check("Bash", {"command": "git commit -m 'verified'"})
    assert not d.allowed and "may not modify git state" in d.reason


def test_ordinary_commands_are_allowed(guard_for):
    g = guard_for("FORGE")
    for command in ["pytest -q tests/", "git status", "git diff HEAD", "ls -la"]:
        assert g.check("Bash", {"command": command}).allowed, command


def test_protected_test_file_cannot_be_rewritten_through_the_shell(tmp_path):
    """§10: the test that defines success must not be editable by the fixer."""
    cfg = load_config(search=CONFIG_SEARCH)
    spec = cfg.agents["FORGE"].model_copy(deep=True)
    spec.policy.protected_paths = ["qa/repro/test_defining.py"]
    ctx = ToolContext(
        store=RunStore.new(root=tmp_path), maps=SystemMapStore(tmp_path),
        config=cfg, agent=spec, target_root=TARGET,
    )
    g = Guardrail(ctx)
    assert not g.check("Bash", {"command": "echo pass > qa/repro/test_defining.py"}).allowed
    assert not g.check("Bash", {"command": "sed -i '' s/assert/pass/ qa/repro/test_defining.py"}).allowed


# -- tool and server allowlists ---------------------------------------------


def test_a_tool_outside_the_allowlist_is_refused(guard_for):
    d = guard_for("CLERK").check("Bash", {"command": "ls"})
    assert not d.allowed and "allowlist" in d.reason


def test_an_undeclared_mcp_server_is_refused(guard_for):
    d = guard_for("CONDUIT").check("mcp__tracker__create_issue", {})
    assert not d.allowed and "not connected" in d.reason


def test_a_declared_mcp_server_is_allowed(guard_for):
    assert guard_for("CLERK").check("mcp__tracker__create_issue", {}).allowed


def test_web_access_is_refused_to_discovery_agents(guard_for):
    d = guard_for("CONDUIT").check("WebFetch", {"url": "https://example.com"})
    assert not d.allowed and "network research" in d.reason


# -- the audit trail --------------------------------------------------------


async def test_a_denial_is_recorded_in_the_ledger(guard_for):
    g = guard_for("CONDUIT")
    result = await g.can_use_tool("Write", {"file_path": "x.py", "content": "y"}, None)
    assert result.behavior == "deny"

    denials = list(g.ctx.store.ledger("denial"))
    assert len(denials) == 1
    assert denials[0].agent == "CONDUIT"
    assert denials[0].detail["tool"] == "Write"


async def test_an_allowed_call_is_not_logged_as_a_denial(guard_for):
    g = guard_for("FORGE")
    result = await g.can_use_tool("Write", {"file_path": "qa/repro/t.py", "content": "y"}, None)
    assert result.behavior == "allow"
    assert not list(g.ctx.store.ledger("denial"))


async def test_file_contents_are_not_dumped_into_the_ledger(guard_for):
    g = guard_for("CONDUIT")
    await g.can_use_tool("Write", {"file_path": "x.py", "content": "S" * 5000}, None)
    entry = next(g.ctx.store.ledger("denial"))
    assert entry.detail["args"]["content"] == "<5000 chars>"


# -- the hook is the enforcement path, not the callback ---------------------
#
# An `allowed_tools` entry naming a whole tool auto-approves it before
# `can_use_tool` runs. These tests exist because an earlier version of this
# module put the policy only in that callback, where it was never consulted.


async def test_the_hook_denies_a_write_outside_the_sandbox(guard_for):
    g = guard_for("FORGE")
    out = await g.pre_tool_use(
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd", "content": "x"}},
        "tu_1",
        None,
    )
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "outside" in decision["permissionDecisionReason"]


async def test_the_hook_allows_a_write_inside_the_sandbox(guard_for):
    g = guard_for("FORGE")
    out = await g.pre_tool_use(
        {"tool_name": "Write", "tool_input": {"file_path": "qa/repro/t.py", "content": "x"}},
        "tu_2",
        None,
    )
    assert out == {}, "an allowed call must not carry a permission decision"


async def test_the_hook_denies_a_forbidden_shell_command(guard_for):
    g = guard_for("FORGE")
    out = await g.pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}, None, None
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_the_hook_records_both_the_call_and_the_denial(guard_for):
    g = guard_for("CONDUIT")
    await g.pre_tool_use({"tool_name": "Write", "tool_input": {"file_path": "x.py"}}, None, None)

    calls = list(g.ctx.store.ledger("tool_call"))
    denials = list(g.ctx.store.ledger("denial"))
    assert len(calls) == 1 and calls[0].detail["allowed"] is False
    assert len(denials) == 1 and denials[0].detail["via"] == "hook"


async def test_the_hook_survives_a_malformed_payload(guard_for):
    """A hook that raises would break the turn. It must decide, not explode."""
    g = guard_for("FORGE")
    out = await g.pre_tool_use({"tool_name": "Write"}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await g.pre_tool_use({}, None, None) is not None


def test_the_registry_wires_the_hook_to_pretooluse(tmp_path):
    """Regression guard: if this wiring is lost, nothing is enforced at all."""
    from qaas.mcp.context import ToolContext
    from qaas.registry import build_hooks
    from qaas.sdk_compat import PRE_TOOL_USE
    from qaas.store import RunStore, SystemMapStore

    cfg = load_config(search=CONFIG_SEARCH)
    ctx = ToolContext(
        store=RunStore.new(root=tmp_path), maps=SystemMapStore(tmp_path),
        config=cfg, agent=cfg.agents["FORGE"], target_root=TARGET,
    )
    guard = Guardrail(ctx)
    hooks = build_hooks(guard, ctx)
    assert guard.pre_tool_use in hooks[PRE_TOOL_USE][0].hooks


# -- the §8.2 autonomy envelope ---------------------------------------------
#
# MENDER is the only agent that may change product code. These are the limits
# that make that acceptable, and they are enforced here rather than requested in
# its prompt, because a prompt is a request and this is a decision.


def test_mender_may_fix_ordinary_product_code(guard_for):
    assert guard_for("MENDER").check(
        "Write", {"file_path": "api/app/routes/orders.py", "content": "..."}
    ).allowed


@pytest.mark.parametrize(
    "path,expected",
    [
        ("api/migrations/002_add_index.sql", "migration"),
        ("api/app/auth.py", "authentication"),
        ("api/app/routes/payments.py", "payment"),
        ("infra/main.tf", "infrastructure"),
        ("docker-compose.yml", "container"),
        (".github/workflows/ci.yml", "CI"),
    ],
)
def test_forbidden_classes_stop_at_a_human(guard_for, path, expected):
    """§8.2: these changes need approval however small they look, because their
    blast radius is not something a review can reliably bound."""
    d = guard_for("MENDER").check("Write", {"file_path": path, "content": "..."})
    assert not d.allowed
    assert expected in d.reason
    assert "escalate" in d.reason.lower(), "a refusal must tell the agent what to do instead"


def test_the_defining_test_cannot_be_edited_by_the_fixer(tmp_path):
    """§10, symptom fixes: a fixer that edits the test has hidden the defect."""
    cfg = load_config(search=CONFIG_SEARCH)
    spec = cfg.agents["MENDER"].model_copy(deep=True)
    spec.policy.protected_paths = ["qa/repro/test_orders_limit.py"]
    ctx = ToolContext(
        store=RunStore.new(root=tmp_path), maps=SystemMapStore(tmp_path),
        config=cfg, agent=spec, target_root=TARGET,
    )
    d = Guardrail(ctx).check(
        "Write", {"file_path": "qa/repro/test_orders_limit.py", "content": "assert True"}
    )
    assert not d.allowed
    assert "defines success" in d.reason


def test_the_diff_budget_is_counted_per_file_not_per_edit(guard_for):
    """Editing one file six times is one file's worth of change. Counting calls
    would refuse ordinary iteration on a single fix."""
    g = guard_for("MENDER")
    for _ in range(6):
        assert g.check(
            "Edit", {"file_path": "api/app/routes/orders.py",
                     "old_string": "a", "new_string": "b"}
        ).allowed


def test_a_fix_wider_than_the_envelope_is_refused_with_what_it_touched(guard_for):
    g = guard_for("MENDER")
    limit = g.policy.max_diff_files
    assert limit, "MENDER must have a diff budget"

    for i in range(limit):
        assert g.check(
            "Write", {"file_path": f"api/app/mod_{i}.py", "content": "..."}
        ).allowed, f"file {i} should be within budget"

    d = g.check("Write", {"file_path": "api/app/one_too_many.py", "content": "..."})
    assert not d.allowed
    assert str(limit) in d.reason
    assert "mod_0.py" in d.reason, "the refusal should say what it already touched"


def test_arbiter_cannot_write_anything_at_all(guard_for):
    """The reviewer having no write access is the whole point of the separation."""
    g = guard_for("ARBITER")
    assert not g.check("Write", {"file_path": "api/app/routes/orders.py"}).allowed
    assert not g.check("Bash", {"command": "git commit -m x"}).allowed


def test_mender_may_branch_only_under_fix(guard_for):
    g = guard_for("MENDER")
    assert g.check("Bash", {"command": "git checkout -b fix/CORVID-1-limit"}).allowed
    assert not g.check("Bash", {"command": "git checkout -b feature/rewrite"}).allowed
    assert not g.check("Bash", {"command": "git push origin main"}).allowed


def test_nothing_in_the_system_can_merge(guard_for):
    """§8.4: merge is always human. Not policy — absence of capability."""
    for agent in ["MENDER", "ARBITER", "PROOF", "FORGE"]:
        g = guard_for(agent)
        assert not g.check("Bash", {"command": "git merge fix/x"}).allowed
        assert not g.check("Bash", {"command": "gh pr merge 42 --squash"}).allowed


# -- the sandbox is anchored on the target, not on the cwd -------------------
#
# Phase E split "where qaas lives" from "the application under test". Before it,
# `ToolContext.repo_root` was filled with `Path.cwd()` and served as both, so an
# allowlist entry like `api/app` silently meant `<the qaas checkout>/api/app`.
# With `qaas run --repo <url>` the target is a clone under `.qaas/targets/`, and
# an allowlist anchored on the cwd would both deny every legitimate write and
# permit a sandbox sitting inside qaas's own source. These fail loudly if that
# coupling ever comes back.


def _mender_guard(tmp_path: Path, target_root: Path) -> Guardrail:
    cfg = load_config(search=CONFIG_SEARCH)
    return Guardrail(
        ToolContext(
            store=RunStore.new(root=tmp_path / "state"),
            maps=SystemMapStore(tmp_path / "state"),
            config=cfg,
            agent=cfg.agents["MENDER"],
            target_root=target_root,
        )
    )


def test_the_write_sandbox_follows_the_target_not_the_process_cwd(tmp_path, monkeypatch):
    """One policy, two targets: each sandbox resolves under its own target."""
    one, two = tmp_path / "clone-one", tmp_path / "clone-two"
    for root in (one, two):
        (root / "api" / "app").mkdir(parents=True)

    monkeypatch.chdir(tmp_path)  # a cwd that is neither target
    g_one, g_two = _mender_guard(tmp_path, one), _mender_guard(tmp_path, two)

    assert (g_one.root, g_two.root) == (one.resolve(), two.resolve())
    assert g_one.check("Write", {"file_path": "api/app/x.py", "content": "..."}).allowed
    assert g_two.check("Write", {"file_path": "api/app/x.py", "content": "..."}).allowed

    # An absolute path into the *other* target is outside this agent's sandbox,
    # even though the relative spelling is identical.
    d = g_one.check("Write", {"file_path": str(two / "api" / "app" / "x.py"), "content": "..."})
    assert not d.allowed and "outside" in d.reason


def test_an_agent_cannot_write_into_the_qaas_checkout(tmp_path, monkeypatch):
    """The sharp edge: qaas's own source must not be reachable from a run.

    A target elsewhere on disk puts the qaas project outside the sandbox by
    construction. If `target_root` went back to being the cwd, `src/qaas/` would
    be one relative path away from the one agent that may write product code.
    """
    target = tmp_path / "clone"
    (target / "api" / "app").mkdir(parents=True)
    monkeypatch.chdir(REPO)

    g = _mender_guard(tmp_path, target)

    # Directly on the anchoring, not only on its consequences: every sandbox
    # this agent has must live under the target, and none of them anywhere near
    # the qaas checkout the process is standing in.
    assert g.root == target.resolve()
    assert g._allowed_roots, "MENDER must have somewhere to write"
    for sandbox in g._allowed_roots:
        assert sandbox.is_relative_to(target.resolve()), sandbox
        assert not sandbox.is_relative_to(REPO), f"{sandbox} is inside the qaas checkout"

    for path in (
        str(REPO / "src" / "qaas" / "guardrails.py"),
        str(REPO / "src" / "qaas" / "defaults" / "config" / "agents" / "mender.yaml"),
        "../../src/qaas/guardrails.py",
    ):
        assert not g.check("Write", {"file_path": path, "content": "..."}).allowed, (
            f"{path} was writable from a run against {target}"
        )


def test_a_forbidden_class_at_the_root_of_a_target_is_still_refused(tmp_path):
    """`.github/` sits at a repository's root, which is exactly where these globs
    used to miss it: paths arrived prefixed with `target-app/`, so `*/.github/*`
    matched, and the day the prefix went away it silently stopped matching."""
    target = tmp_path / "clone"
    target.mkdir()
    g = _mender_guard(tmp_path, target)
    for path in (
        ".github/workflows/ci.yml",
        "migrations/001_add_index.sql",
        "infra/main.tf",
        "docker-compose.yml",
    ):
        d = g.check("Write", {"file_path": path, "content": "..."})
        assert not d.allowed, f"{path} was not caught by a forbidden class"
        assert "escalate" in d.reason.lower(), f"{path}: a refusal must say what to do instead"


def test_the_sdk_subprocess_is_started_in_the_target(tmp_path):
    """`registry` hands `cwd` to the SDK. It is the target, and it travels with
    `setting_sources=[]` -- a cloned repository's `.claude/settings.json` must
    never load into a process holding this system's credentials."""
    from qaas.registry import build_options

    cfg = load_config(search=CONFIG_SEARCH)
    target = tmp_path / "clone"
    target.mkdir()
    ctx = ToolContext(
        store=RunStore.new(root=tmp_path / "state"),
        maps=SystemMapStore(tmp_path / "state"),
        config=cfg,
        agent=cfg.agents["CONDUIT"],
        target_root=target,
    )
    options = build_options(cfg.agents["CONDUIT"], ctx)
    assert options.cwd == str(target)
    assert options.setting_sources == []
