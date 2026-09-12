"""Fixtures for the dashboard tests.

Runs are built through the real `RunStore` rather than by writing JSON by hand.
A hand-written fixture is a second implementation of the writer, and the day the
writer changes shape the fixture keeps passing while the product breaks.

Note the writer's signature: `store.log(kind, agent=..., **detail)` takes detail
as keyword arguments, not a dict (store.py:152). Passing `detail={...}` nests it
one level down, `trace.describe()` then renders an empty string, and every
assertion about rendered text passes while showing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qaas.config import AgentSpec
from qaas.envelope import (
    DefectEnvelope,
    Dedupe,
    DefectClass,
    Domain,
    Evidence,
    Impact,
    Location,
    Reproduction,
    Severity,
)
from qaas.store import AgentResult, RunStore


def make_spec(name: str, layer: str, model: str = "claude-opus-5") -> AgentSpec:
    return AgentSpec(
        name=name, layer=layer, role=f"{name} role", prompt=f"{name}.md", model=model
    )


#: A roster covering every phase, including the `triage` layer's two members --
#: the one layer that does not map onto a phase 1:1.
SPECS = {
    s.name: s
    for s in (
        make_spec("MAPPER", "control", "claude-sonnet-5"),
        make_spec("API", "discovery"),
        make_spec("BROWSER", "discovery"),
        make_spec("REPRODUCER", "triage"),
        make_spec("TRIAGE", "triage", "claude-sonnet-5"),
        make_spec("VERIFIER", "remediation"),
        make_spec("REPORTER", "reporting", "claude-sonnet-5"),
    )
}


def make_envelope(
    run_id: str,
    *,
    agent: str = "API",
    severity: Severity = Severity.BLOCKER,
    confidence: float = 0.9,
    evidence: bool = True,
    title: str = "Cross-tenant order read",
) -> DefectEnvelope:
    return DefectEnvelope(
        run_id=run_id,
        discovered_by=agent,
        domain=Domain.SECURITY,
        defect_class=DefectClass.VULNERABILITY,
        title=title,
        summary="An org can read another org's orders.",
        location=Location(service="orders-api", paths=["api/app/routes/orders.py:44"]),
        evidence=(
            [Evidence(type="log", uri=f"artifact://{run_id}/cross-tenant.log")]
            if evidence
            else []
        ),
        reproduction=Reproduction(status="reproduced", steps=["GET /v1/orders as org 2"]),
        impact=Impact(user_facing=True, security_relevant=True),
        severity=severity,
        confidence=confidence,
        dedupe=Dedupe(),
    )


def build_run(
    root: Path,
    run_id: str = "run-20260101T000000-aaaaaa",
    *,
    roster: list[str] | None = None,
    finish: bool = True,
) -> RunStore:
    """A small but complete run: two discovery agents, a finding, a refusal, a ticket."""
    store = RunStore(run_id, root=root, create=True)
    roster = roster or ["MAPPER", "API", "BROWSER", "TRIAGE"]
    store.log(
        "run_started",
        mode="pr-check",
        agents=sorted(roster),
        budget_usd=None,
        wall_clock_s=900,
        target_root="/tmp/corvid",
        target_sha="bf40c35766cc",
        target_dirty=True,
        target_branch="main",
    )

    store.log("agent_started", agent="MAPPER", model="claude-sonnet-5", task_chars=10)
    store.put_result(AgentResult(agent="MAPPER", cost_usd=0.5, num_turns=8, duration_s=30.0))

    store.log("agent_started", agent="API", model="claude-opus-5", task_chars=10)
    store.log("tool_call", agent="API", tool="Read", tool_use_id="t1", allowed=True)
    store.log("tool_call", agent="API", tool="Bash", tool_use_id="t2", allowed=False)
    store.log(
        "denial",
        agent="API",
        tool="Bash",
        reason="Bash is not in API's tool allowlist (Read, Grep, Glob).",
        via="hook",
        args={"command": "ls -la"},
    )
    envelope = make_envelope(run_id)
    store.put_envelope(envelope)
    store.put_result(
        AgentResult(
            agent="API",
            cost_usd=1.25,
            num_turns=21,
            duration_s=90.0,
            envelope_ids=[envelope.id],
        )
    )

    store.log("skipped", reason="target has no live_ui", agents=["BROWSER"])

    store.log("agent_started", agent="TRIAGE", model="claude-sonnet-5", task_chars=10)
    store.log(
        "ticket",
        agent="TRIAGE",
        action="created",
        key="QAAS-1",
        project="QAAS",
        severity="blocker",
        envelope_id=envelope.id,
        restricted=False,
    )
    store.put_result(AgentResult(agent="TRIAGE", cost_usd=0.25, num_turns=6, duration_s=20.0))

    if finish:
        store.log(
            "run_finished",
            run_id=run_id,
            mode="pr-check",
            agents_run=3,
            failed=[],
            cost_usd=2.0,
            escalations=[],
            stopped_early=None,
        )
    return store


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    build_run(tmp_path)
    return tmp_path
