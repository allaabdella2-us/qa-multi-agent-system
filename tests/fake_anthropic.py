"""A scripted stand-in for the Anthropic Messages API, for driving the real CLI.

The bundled Claude Code is pointed at this with `ANTHROPIC_BASE_URL`. Every
request answers with the next scripted Bash `tool_use`, then `end_turn` once the
script runs out; each `tool_result` the CLI sends back is recorded, so a test
can read what a command actually did inside the sandbox. Offline and free: no
request leaves the machine.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAPI:
    def __init__(self, commands: list[str]):
        self.commands = list(commands)
        self.results: list[dict] = []
        self.seen_ids: set = set()
        self.requests: list[str] = []
        self.lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                api.requests.append(f"GET {self.path}")
                self._json(200, {"data": []})

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                api.requests.append(f"POST {self.path}")
                body = self._body()
                if "count_tokens" in self.path:
                    return self._json(200, {"input_tokens": 10})
                if "/v1/messages" not in self.path:
                    return self._json(200, {})
                msgs = body.get("messages") or []
                tool_results = [
                    b for m in msgs for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                with api.lock:
                    for tr in tool_results:
                        if tr.get("tool_use_id") not in api.seen_ids:
                            api.seen_ids.add(tr.get("tool_use_id"))
                            api.results.append(tr)
                    if not api.commands or not body.get("tools"):
                        blocks = [{"type": "text", "text": "done"}]
                        stop = "end_turn"
                    else:
                        cmd = api.commands.pop(0)
                        inp = dict(cmd) if isinstance(cmd, dict) else {"command": cmd, "description": "probe"}
                        blocks = [{
                            "type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}",
                            "name": "Bash", "input": inp,
                        }]
                        stop = "tool_use"
                if body.get("stream"):
                    self._stream(body.get("model", "claude-sonnet-5"), blocks, stop)
                else:
                    self._json(200, {
                        "id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                        "model": body.get("model", "claude-sonnet-5"), "content": blocks,
                        "stop_reason": stop, "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    })

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, model, blocks, stop):
                events = [("message_start", {"type": "message_start", "message": {
                    "id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1}}})]
                for i, b in enumerate(blocks):
                    if b["type"] == "text":
                        events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                       "content_block": {"type": "text", "text": ""}}))
                        events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                       "delta": {"type": "text_delta", "text": b["text"]}}))
                    else:
                        events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                       "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
                        events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
                    events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
                events.append(("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": 5}}))
                events.append(("message_stop", {"type": "message_stop"}))
                payload = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def result_texts(self) -> list[str]:
        out = []
        for tr in self.results:
            c = tr.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            out.append(f"{'ERR ' if tr.get('is_error') else ''}{c}")
        return out
