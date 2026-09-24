"""Shared state the in-process MCP servers close over.

Every tool call lands in this process, so the servers can reach the run store,
the config and the target app directly. That is the point of building them
in-process: validation, guardrails and persistence happen where the state
already lives, with no serialisation boundary to reason about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qaas.config import AgentSpec, SystemConfig
from qaas.store import RunStore, SystemMapStore


@dataclass
class ToolContext:
    """One agent's view of the run. Rebuilt per agent invocation."""

    store: RunStore
    maps: SystemMapStore
    config: SystemConfig
    agent: AgentSpec

    #: The application under test, on disk. Named `repo_root` once, and that
    #: name was the bug: it read as "the qaas checkout", it was filled with
    #: `Path.cwd()`, and every consumer -- the write-path allowlist, the test
    #: runner's cwd, the vcs sandbox, the SDK subprocess cwd -- actually wanted
    #: the target. That only coincided while the target sat inside the qaas
    #: checkout, which is true of the bundled demo and of nothing else.
    #: Comes from `SystemConfig.target_root()`, i.e. the profile.
    target_root: Path
    map_version: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    #: What this dispatch is working on -- a ticket key for FIXER -- so the §8.2
    #: diff budget bounds one fix rather than the whole run. None is the run.
    scope: str | None = None
    #: Where a new branch starts when the agent names no base: the ticket's
    #: reproduction branch for FIXER, the ref the run started on otherwise.
    #: None falls back to whatever is checked out.
    base_ref: str | None = None

    @property
    def touched_files(self) -> set[str]:
        """Files this agent has modified for this piece of work (§8.2).

        It used to be a field on this context, which is rebuilt per dispatch — so
        FIXER's budget reset on every FIXER/REVIEWER round trip. A budget that
        resets whenever the thing it is bounding loops is not a budget, so the
        store holds the set. It is keyed by `scope` too: without that, the one
        set was shared by every *ticket* in the run, which bounded the run rather
        than the diff §8.2 is about.
        """
        return self.store.touched_files(self.agent.name, self.scope)

    def bump(self, key: str) -> int:
        counters = self._run_counters()
        counters[key] = counters.get(key, 0) + 1
        return counters[key]

    def count(self, key: str) -> int:
        return self._run_counters().get(key, 0)

    def _run_counters(self) -> dict[str, int]:
        """The tally that spans the run, falling back to this context's own.

        Delegated to the store the way `touched_files` already is: a context is
        built per dispatch and a run outlives many of them, so a counter living
        here bounded one invocation rather than one run. The local `counters`
        dict stays as the fallback for a context built without a real store.
        """
        store = getattr(self, "store", None)
        getter = getattr(store, "counters", None)
        return getter(self.agent.name) if callable(getter) else self.counters


#: Largest JSON block `ok()` will append. A run_suite over a large repository
#: can carry thousands of rows, and an unbounded block would spend an agent's
#: context on one tool result.
STRUCTURED_TEXT_LIMIT = 60_000

#: A string this long that already appears in the text is not repeated in the
#: JSON block -- `vcs.diff` returns the patch as its text and would otherwise
#: pay for it twice.
_DUPLICATE_MIN_CHARS = 200


def ok(text: str, **structured: Any) -> dict[str, Any]:
    """A successful MCP tool result.

    The structured payload is also rendered into the *text*, and that is the
    fix rather than redundancy. The SDK's `run_tool` builds the result the model
    sees from `content` and `is_error` only -- `structuredContent` is dropped on
    the floor (the same fact `registry._was_held` already works around for one
    flag). So everything a server put only in `structured` never reached an
    agent: `impersonate` said "Send header: Authorization: Bearer <token>" while
    the token itself was discarded, `run_single` said "failed in 0.1s" with the
    failure output gone, and a truncated diff carried no sign of it. Every test
    read the handler's dict directly, which is how 200 tests agreed with a tool
    surface the model never saw. `structuredContent` stays for any reader that
    speaks the wire format.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if structured:
        result["structuredContent"] = structured
        block = structured_text(text, structured)
        if block:
            result["content"].append({"type": "text", "text": block})
    return result


def structured_text(text: str, structured: dict[str, Any]) -> str:
    """The structured payload as a JSON text block the model can read."""
    shown = {
        key: value
        for key, value in structured.items()
        if not (
            isinstance(value, str)
            and len(value) >= _DUPLICATE_MIN_CHARS
            and value in text
        )
    }
    if not shown:
        return ""
    rendered = json.dumps(shown, default=str, ensure_ascii=False)
    if len(rendered) > STRUCTURED_TEXT_LIMIT:
        rendered = (
            rendered[:STRUCTURED_TEXT_LIMIT]
            + f"\n[structured result truncated: {len(rendered)} chars, "
            f"showing the first {STRUCTURED_TEXT_LIMIT}]"
        )
    return "Structured result (JSON):\n" + rendered


def err(text: str) -> dict[str, Any]:
    """A failed MCP tool result.

    Tool errors are returned, not raised: the agent should read the reason and
    correct itself rather than have the turn die.

    Both spellings of the flag are set, and that is not belt-and-braces -- it is
    the bug. The MCP wire format spells it `isError`, and this returned only
    that; the SDK reads the handler's dict with `result.get("is_error", False)`
    (`claude_agent_sdk.__init__`, `run_tool`) and drops anything else. So every
    refusal from all seven in-process servers -- a guardrail denial, "you may not
    file", "not reproducible" -- was delivered to the model as a *successful*
    tool result, and the whole "read the reason and correct itself" contract had
    never once run. Keep `isError` for any reader that speaks the wire format,
    and `is_error` because that is the key the thing on the other end reads.
    """
    return {"content": [{"type": "text", "text": text}], "isError": True, "is_error": True}


def handlers(tools: list) -> dict[str, Any]:
    """Map tool name -> handler. Used by tests to exercise a server directly."""
    return {t.name: t.handler for t in tools}
