"""The shape of an MCP tool result.

Small, and the most consequential file in this directory: `err()` is how
every refusal in the system reaches a model, and it spent the project
setting a key nothing read.
"""

from __future__ import annotations

def test_a_refusal_carries_the_key_the_sdk_reads():
    """The bug this whole helper's docstring is about, pinned directly.

    `err()` set only `isError`, the MCP wire spelling. `claude_agent_sdk`'s
    `run_tool` builds the wire result with `result.get("is_error", False)` and
    drops anything else, so every refusal from all seven in-process servers --
    a guardrail denial, "you may not file", "not reproducible" -- arrived at the
    model marked successful. The contract in CLAUDE.md ("errors are returned,
    not raised, so the agent reads the reason and corrects itself") had never
    once run.

    Asserted against the installed SDK's own source rather than a remembered
    string, so the day it changes spelling again this fails instead of quietly
    going back to being wrong.
    """
    import inspect

    import claude_agent_sdk

    from qaas.mcp.context import err, ok

    result = err("nope")
    assert result["is_error"] is True
    assert result["isError"] is True, "the wire spelling is still worth carrying"
    assert "is_error" not in ok("fine")

    source = inspect.getsource(claude_agent_sdk)
    assert 'result.get("is_error"' in source, (
        "the SDK no longer reads `is_error` from a handler's dict. Find what it "
        "reads now and make `err()` set that -- a refusal the model sees as a "
        "success is the worst shape a failure takes here."
    )
