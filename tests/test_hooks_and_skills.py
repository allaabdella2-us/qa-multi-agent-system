"""The output contract, enforced by the Stop hook, and the skills wiring.

`can_use_tool` can only ever see calls that happen, so it cannot notice the call
that did not. These cover the gap: an agent that produces a confident summary and
no artifact must be caught while it still has a turn to fix it.
"""

from pathlib import Path

import pytest

from qaas.config import load_config
from qaas.guardrails import ALWAYS_GRANTED, Guardrail
from qaas.mcp.context import ToolContext
from qaas.registry import TurnRecord, build_hooks, build_options
from qaas.sdk_compat import POST_TOOL_USE, PRE_TOOL_USE, STOP
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO / ".claude" / "skills"
AGENTS = ["CARTOGRAPHER", "CONDUIT", "SURFACE", "FORGE", "CLERK", "PROOF"]


@pytest.fixture(scope="module")
def cfg():
    return load_config(REPO / "config")


@pytest.fixture
def wire(cfg, tmp_path):
    def build(agent_name: str):
        ctx = ToolContext(
            store=RunStore.new(root=tmp_path), maps=SystemMapStore(tmp_path),
            config=cfg, agent=cfg.agents[agent_name], repo_root=REPO,
        )
        record = TurnRecord()
        return ctx, record, build_hooks(Guardrail(ctx), ctx, record)
    return build


async def fire(hooks, event, payload):
    out = {}
    for hook in hooks[event][0].hooks:
        out = (await hook(payload, "tu_1", None)) or out
    return out


# -- the Stop hook ----------------------------------------------------------


async def test_an_agent_that_skipped_its_deliverable_is_blocked(wire):
    ctx, record, hooks = wire("CARTOGRAPHER")
    record.record("Read")
    record.record("Grep")

    out = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": False})
    assert out["decision"] == "block"
    assert "put_system_map" in out["reason"]
    assert [e.kind for e in ctx.store.ledger("stop_blocked")] == ["stop_blocked"]


async def test_an_agent_that_did_its_job_stops_freely(wire):
    ctx, record, hooks = wire("CARTOGRAPHER")
    record.record("mcp__envelope__put_system_map")

    out = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": False})
    assert out.get("decision") != "block"
    assert not list(ctx.store.ledger("stop_blocked"))


async def test_blocking_never_loops_forever(wire):
    """An agent that genuinely cannot comply must not burn the budget retrying."""
    ctx, record, hooks = wire("FORGE")

    first = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": False})
    assert first["decision"] == "block"

    second = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": True})
    assert second.get("decision") != "block"
    unmet = list(ctx.store.ledger("contract_unmet"))
    assert len(unmet) == 1
    assert unmet[0].detail["missing"] == ["mcp__envelope__record_reproduction"]


async def test_a_discovery_agent_may_legitimately_find_nothing(wire):
    """Requiring an emission would manufacture findings to satisfy the hook."""
    ctx, record, hooks = wire("CONDUIT")
    out = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": False})
    assert out.get("decision") != "block"


async def test_the_block_reason_names_the_tool_not_the_internal_id(wire):
    _, _, hooks = wire("PROOF")
    out = await fire(hooks, STOP, {"hook_event_name": "Stop", "stop_hook_active": False})
    assert "transition" in out["reason"]
    assert "mcp__tracker__" not in out["reason"], "the agent calls it by its short name"


# -- the PostToolUse reminder ----------------------------------------------


async def test_a_held_envelope_tells_the_agent_immediately(wire):
    ctx, record, hooks = wire("CONDUIT")
    out = await fire(hooks, POST_TOOL_USE, {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__envelope__emit_envelope",
        "tool_response": {"structuredContent": {"fileable": False}},
    })
    assert "held from filing" in out["systemMessage"]
    assert "put_artifact" in out["systemMessage"]
    assert record.held_envelopes == 1


async def test_a_fileable_envelope_produces_no_nagging(wire):
    _, record, hooks = wire("CONDUIT")
    out = await fire(hooks, POST_TOOL_USE, {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__envelope__emit_envelope",
        "tool_response": {"structuredContent": {"fileable": True}},
    })
    assert "systemMessage" not in out
    assert record.held_envelopes == 0


async def test_a_tool_error_is_logged(wire):
    ctx, _, hooks = wire("CONDUIT")
    await fire(hooks, POST_TOOL_USE, {
        "hook_event_name": "PostToolUse",
        "tool_name": "mcp__contract_diff__diff_openapi",
        "tool_response": {"isError": True, "content": []},
    })
    assert [e.detail["tool"] for e in ctx.store.ledger("tool_error")] == [
        "mcp__contract_diff__diff_openapi"
    ]


# -- PreToolUse still enforces ----------------------------------------------


async def test_the_pre_tool_hook_still_denies_and_still_counts(wire):
    ctx, record, hooks = wire("CONDUIT")
    await fire(hooks, PRE_TOOL_USE, {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": "x.py", "content": "y"},
    })
    assert list(ctx.store.ledger("denial")), "enforcement survived the hook rewrite"
    assert "Write" in record.called, "and the attempt still counts toward the contract"


# -- skills wiring ----------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_every_configured_skill_exists_on_disk(cfg, agent):
    for skill in cfg.agents[agent].skills:
        assert (SKILLS_DIR / skill / "SKILL.md").exists(), f"{agent} names missing skill {skill}"


@pytest.mark.parametrize("agent", AGENTS)
def test_skills_reach_the_agents_options(wire, cfg, agent):
    ctx, _, _ = wire(agent)
    assert build_options(cfg.agents[agent], ctx).skills == cfg.agents[agent].skills


def test_every_skill_on_disk_is_used_by_someone(cfg):
    """An unreferenced skill is either a missing wiring or dead weight."""
    on_disk = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    referenced = {s for spec in cfg.agents.values() for s in spec.skills}
    assert not on_disk - referenced, f"unreferenced skills: {sorted(on_disk - referenced)}"


@pytest.mark.parametrize("path", sorted(SKILLS_DIR.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_each_skill_description_is_a_strong_trigger(path):
    """A weak description is a skill that never fires — the usual failure mode.

    The model chooses skills by reading `description`, so it must name a concrete
    trigger and say when to skip, not merely describe a topic.
    """
    text = path.read_text()
    header, _, _ = text.partition("\n---\n")
    assert f"name: {path.parent.name}" in header, "frontmatter name must match the directory"

    description = header.split("description:", 1)[1]
    assert "TRIGGER" in description, "must state when it fires"
    assert "SKIP" in description, "must state when it does not"
    assert "BEFORE" in description, "must load ahead of the work, not after it"
    assert len(description) > 200, "too terse to discriminate against 22 other skills"


def test_the_rubric_lives_in_one_place_only(cfg):
    """The severity table was duplicated between CLERK's prompt and the skill.

    Duplicated rules drift, and the two copies then disagree about what a
    blocker is. The skill is the authority; the prompt defers to it.
    """
    rubric = (SKILLS_DIR / "severity-rubric" / "SKILL.md").read_text()
    assert "blocker" in rubric and "critical" in rubric

    for name in ("CLERK", "PROOF"):
        assert "severity-rubric" in cfg.agents[name].skills


def test_harness_plumbing_is_granted_but_grants_nothing(cfg):
    """ToolSearch and Skill are in every allowlist; neither is a capability."""
    assert ALWAYS_GRANTED >= {"ToolSearch", "Skill"}
    assert not ALWAYS_GRANTED & {"Write", "Edit", "Bash", "WebFetch"}
