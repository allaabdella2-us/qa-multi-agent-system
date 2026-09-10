"""Shared scaffolding for the in-process MCP server tests.

The servers close over a ToolContext, so a test only needs a real store on a
temporary path and an agent spec; no MCP transport is involved anywhere here.

`make_ctx` accepts an agent either positionally or by keyword, and either a real
agent from `config/agents/` or a synthetic one, because the two halves of these
tests want different things: the policy tests need TRIAGE's and REPRODUCER's actual
declared permissions, while the degradation tests just need some agent and an
empty directory to point at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from support import CONFIG_SEARCH

from qaas.config import AgentSpec, SystemConfig, load_config
from qaas.envelope import DefectEnvelope
from qaas.mcp.context import ToolContext
from qaas.store import RunStore, SystemMapStore

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = load_config(search=CONFIG_SEARCH)

#: The application under test, which is what a ToolContext is anchored on. Not
#: the qaas checkout: `ToolContext.target_root` means the target, and pointing
#: it at REPO_ROOT here would let the whole suite pass while every write-path
#: sandbox resolved one directory too high.
TARGET_ROOT = _CONFIG.target_root(base=REPO_ROOT)


def is_error(result: dict[str, Any]) -> bool:
    """Whether a tool result is a refusal. Tools return errors, never raise."""
    return bool(result.get("isError"))


def text_of(result: dict[str, Any]) -> str:
    """The human-readable text of a tool result, for asserting on the reason."""
    return "\n".join(
        block.get("text", "") for block in result.get("content", []) if isinstance(block, dict)
    )


def structured(result: dict[str, Any]) -> dict[str, Any]:
    return result.get("structuredContent", {})


@pytest.fixture
def make_ctx(tmp_path):
    """Build a ToolContext.

    Call it as `make_ctx("TRIAGE")` for a real configured agent, or with
    `target_root=` / `agent=` for a synthetic one. `root=` shares a store root
    between two contexts, which is how the cross-run persistence tests work.
    """

    def _make(
        agent: str = "API",
        *,
        target_root: Path = TARGET_ROOT,
        root: Path | None = None,
        config: SystemConfig | None = None,
    ) -> ToolContext:
        store_root = Path(root) if root is not None else tmp_path / ".qaas"
        spec = _CONFIG.agents.get(agent) or AgentSpec(
            name=agent, layer="discovery", role="test double", prompt=f"{agent}.md"
        )
        return ToolContext(
            store=RunStore.new(root=store_root),
            maps=SystemMapStore(root=store_root),
            config=config or _CONFIG,
            agent=spec,
            target_root=target_root,
        )

    return _make


def make_envelope(ctx: ToolContext, **overrides: Any) -> DefectEnvelope:
    """Persist a plausible finding in `ctx`'s store and return it."""
    payload: dict[str, Any] = dict(
        run_id=ctx.store.run_id,
        discovered_by="API",
        domain="api",
        **{"class": "bug"},
        title="Refund endpoint accepts any authenticated user",
        summary=(
            "POST /v1/orders/{id}/refund never checks the caller's role, so a viewer "
            "can refund an order."
        ),
        severity="critical",
        confidence=0.9,
        location={"endpoint": "POST /v1/orders/{order_id}/refund", "paths": ["api/app/routes/orders.py"]},
        evidence=[{"type": "log", "uri": "artifact://run/refund.log"}],
    )
    payload.update(overrides)
    envelope = DefectEnvelope.model_validate(payload)
    ctx.store.put_envelope(envelope)
    return envelope


@pytest.fixture
def ctx(make_ctx):
    """A TRIAGE context — the agent that holds tracker and memory write access.

    Most of these tests exercise what a privileged agent can do; the refusal
    tests build their own read-only context from `make_ctx` instead.
    """
    return make_ctx("TRIAGE")
