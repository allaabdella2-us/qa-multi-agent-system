"""What the model actually receives from an in-process tool, over the SDK's wire.

Every other test in this directory calls a handler and reads its dict. That is
the double, not the consumer: the SDK's `run_tool` rebuilds the result from
`content` and `is_error` only, and a field that lived solely in
`structuredContent` never reached an agent. `impersonate` told the model to send
"Bearer <token>" while the token was discarded; `run_single` said "failed" with
the failure output gone. These tests push a real JSON-RPC `tools/call` through
`SdkMcpBridge` -- the object the CLI's MCP client talks to -- and assert on the
JSON that comes back.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

import pytest
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

from qaas.mcp import env_control
from qaas.mcp.context import STRUCTURED_TEXT_LIMIT, ok


async def call_over_wire(server_config: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One `tools/call`, as the CLI sends it, answered as the CLI receives it."""
    bridge = SdkMcpBridge(server_config["name"], server_config["instance"])
    try:
        await bridge.handle({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "qaas-test", "version": "0"},
            },
        })
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = await bridge.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
    finally:
        await bridge.aclose()
    assert response is not None and "result" in response, response
    return response["result"]


def wire_text(result: dict[str, Any]) -> str:
    return "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")


async def test_structured_fields_reach_the_model():
    @tool("probe", "returns a structured payload", {"type": "object", "properties": {}})
    async def probe(args):
        return ok("summary only", secret="value-the-agent-needs", rows=[{"id": "t::a", "outcome": "failed"}])

    server = create_sdk_mcp_server(name="probe", version="0", tools=[probe])
    result = await call_over_wire(server, "probe", {})
    text = wire_text(result)
    assert "summary only" in text
    assert "value-the-agent-needs" in text
    assert "t::a" in text


def test_a_long_string_already_in_the_text_is_not_repeated():
    patch = "+" * 5_000
    result = ok(patch, truncated=True, lines=12)
    joined = "\n".join(b["text"] for b in result["content"])
    assert joined.count(patch) == 1
    assert '"truncated": true' in joined


def test_the_json_block_is_bounded():
    result = ok("big", rows=["x" * 100] * 5_000)
    block = result["content"][-1]["text"]
    assert len(block) < STRUCTURED_TEXT_LIMIT + 500
    assert "truncated" in block


@pytest.fixture
def login_server(monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            # Drain the body first: closing a socket with unread request bytes
            # sends a TCP reset, which the client reads as "connection reset".
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"access_token": "TOKEN-FROM-LOGIN", "role": "admin"}).encode())

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("QAAS_TARGET_BASE_URL", f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv("CORVID_PASSWORD", "not-a-real-password")
    yield
    httpd.shutdown()


async def test_impersonate_hands_the_model_a_usable_token(make_ctx, login_server):
    result = await call_over_wire(env_control.build(make_ctx("AUDITOR")), "impersonate", {"role": "admin"})
    assert not result.get("isError"), wire_text(result)
    assert "TOKEN-FROM-LOGIN" in wire_text(result)
