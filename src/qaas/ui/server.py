"""The dashboard's HTTP surface: JSON snapshots, artifacts, and one SSE stream.

Starlette rather than FastAPI on purpose. `claude-agent-sdk` already depends on
`mcp`, which already depends on starlette, uvicorn and sse-starlette, so this
adds no package to a working install -- and FastAPI's contribution here would be
request-model validation over an API with no request bodies at all.

Everything served is read-only. There is no route that dispatches an agent,
files a ticket or writes to a target, and there is deliberately no way to add
one without it being obvious in this table.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from qaas import trace
from qaas.config import AgentSpec
from qaas.store import DEFAULT_ROOT, LedgerKind, RunStore
from qaas.ui import state

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: What an artifact may be served as. Anything not listed is served as plain
#: text -- never `text/html`. Artifacts are written by agents, and a page that
#: renders agent-authored HTML on the dashboard's own origin would let a finding
#: script the view that is meant to be auditing it.
_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".zip": "application/zip",
}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _ok(payload: Any, status: int = 200) -> Response:
    # Starlette's JSONResponse cannot serialise a datetime, and ledger `detail`
    # dicts carry whatever an agent put in them.
    return Response(
        json.dumps(payload, default=_json_default),
        status_code=status,
        media_type="application/json",
    )


def _err(message: str, status: int = 404) -> Response:
    return _ok({"error": message}, status)


#: Distinct from None, which the tail thread uses to mean "the run is over".
_EMPTY = object()


class RunWatcher:
    """One tail thread per run, shared by every connected client.

    `trace.tail` is a synchronous generator that sleeps between polls
    (trace.py:128), so it runs on a thread and hands entries over a queue. The
    important word is *one*: a real ledger runs to tens of thousands of lines and
    ~93% of them are `tool_call`, so replaying it once per open browser tab is
    the difference between a dashboard and a load test. Clients attach to the
    view this watcher is already maintaining and receive a snapshot plus deltas.
    """

    def __init__(self, view: state.RunView, *, poll: float = trace.POLL_INTERVAL_S) -> None:
        self.view = view
        self.poll = poll
        self.clients: set[asyncio.Queue] = set()
        self._inbox: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._drain: asyncio.Task | None = None
        self.done = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self.done:
            return
        if self.view.completed:
            # Nothing to tail. `tail(from_start=False)` seeks to EOF, and this
            # run's `run_finished` is already behind that offset -- so the
            # thread would poll a file that will never change again, forever,
            # for every finished run anyone opened.
            self.done = True
            return
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()
        self._drain = asyncio.get_running_loop().create_task(self._pump())

    def _tail(self) -> None:
        # `from_start=False`: the view was already built by replaying the file,
        # so starting at EOF is what makes the two halves meet exactly once.
        # Replaying from the start here would double-count every counter.
        try:
            for entry in trace.tail(
                self.view.store, from_start=False, poll=self.poll, stop_on_finish=True
            ):
                self._inbox.put(entry)
        finally:
            self._inbox.put(None)

    #: How long a worker thread may sit inside `queue.get` before returning
    #: empty-handed. It is not a latency budget -- an entry wakes the get
    #: immediately -- it is a cancellation window. A bare blocking `get` in an
    #: `anyio.to_thread` worker cannot be cancelled, and anyio waits for its
    #: workers when the loop closes, so the whole process hung on shutdown with
    #: one live run being watched.
    _TAKE_TIMEOUT_S = 0.25

    def _take(self) -> Any:
        try:
            return self._inbox.get(timeout=self._TAKE_TIMEOUT_S)
        except queue.Empty:
            return _EMPTY

    async def _pump(self) -> None:
        while True:
            entry = await anyio.to_thread.run_sync(self._take)
            if entry is _EMPTY:
                continue
            if entry is None:
                self.done = True
                self._broadcast("done", {"run_id": self.view.run_id, "completed": True})
                return
            before = {n: a.status for n, a in self.view.agents.items()}
            self.view.apply(entry)
            self._emit(entry, before)

    def _emit(self, entry, before: Mapping[str, str]) -> None:
        view = self.view
        if entry.kind not in trace.QUIET_KINDS:
            self._broadcast("ledger", view.recent[-1])
        if entry.kind == LedgerKind.ENVELOPE and view.findings:
            self._broadcast("finding", view.findings[-1].to_json())
        if entry.kind in (LedgerKind.TICKET, LedgerKind.VERDICT, LedgerKind.VERIFIED):
            key = entry.detail.get("key") or entry.detail.get("ticket_key")
            if key and str(key) in view.tickets:
                self._broadcast("ticket", view.tickets[str(key)].to_json())
        # Agent cards and the header change on almost every line, so the patch
        # carries only the agents whose row actually moved.
        moved = {
            n: a.to_json()
            for n, a in view.agents.items()
            if before.get(n) != a.status or (entry.agent == n)
        }
        self._broadcast(
            "patch",
            {
                "elapsed_s": round(view.elapsed_s, 1),
                "cost_usd": round(view.cost_usd, 4),
                "phase": view.phase,
                "phase_status": view.phase_status,
                "counts": view.counts,
                "completed": view.completed,
                "stopped_early": view.stopped_early,
                "escalations": view.escalations,
                "agents": moved,
            },
        )

    def _broadcast(self, event: str, data: Any) -> None:
        message = {"event": event, "data": data}
        for client in list(self.clients):
            try:
                client.put_nowait(message)
            except asyncio.QueueFull:
                # A tab that cannot keep up is dropped rather than allowed to
                # back-pressure the tail thread into unbounded memory.
                self.clients.discard(client)

    def attach(self) -> asyncio.Queue:
        client: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients.add(client)
        return client

    def detach(self, client: asyncio.Queue) -> None:
        self.clients.discard(client)


class Dashboard:
    """Holds the run root, the agent specs, and one watcher per live run."""

    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        *,
        specs: Mapping[str, AgentSpec] | None = None,
        min_confidence: float = 0.6,
        ledger_path: Path | None = None,
        poll: float = trace.POLL_INTERVAL_S,
    ) -> None:
        self.root = Path(root)
        self.specs = dict(specs or {})
        self.min_confidence = min_confidence
        #: The golden ledger of the active target, when it has one. Most targets
        #: never will -- it is a property of a calibration app, not of an
        #: application (cli.py:1229-1237).
        self.ledger_path = ledger_path
        self.poll = poll
        self.watchers: dict[str, RunWatcher] = {}

    def store(self, run_id: str) -> RunStore | None:
        store = RunStore(run_id, root=self.root, create=False)
        # `create=False` matters: constructing a store used to mkdir, so one
        # mistyped URL left a permanent empty run in `qaas runs` (store.py:126).
        return store if store.dir.exists() else None

    def view(self, run_id: str) -> state.RunView | None:
        """The live view if one is being watched, else a fresh replay."""
        watcher = self.watchers.get(run_id)
        if watcher is not None:
            return watcher.view
        store = self.store(run_id)
        if store is None:
            return None
        return state.load(store, self.specs, min_confidence=self.min_confidence)

    def watcher(self, run_id: str) -> RunWatcher | None:
        watcher = self.watchers.get(run_id)
        if watcher is not None:
            return watcher
        store = self.store(run_id)
        if store is None:
            return None
        view = state.load(store, self.specs, min_confidence=self.min_confidence)
        watcher = RunWatcher(view, poll=self.poll)
        self.watchers[run_id] = watcher
        watcher.start()
        return watcher


# -- routes ----------------------------------------------------------------


async def _index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def _runs(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    limit = int(request.query_params.get("limit", 50))
    return _ok(state.list_runs_summary(dash.root, limit))


async def _live(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    run_id = state.pick_run(dash.root)
    if run_id is None:
        return _err("no runs yet — qaas run --mode pr-check")
    return _ok({"run_id": run_id})


async def _run(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    view = dash.view(request.path_params["run_id"])
    if view is None:
        return _err(f"no such run: {request.path_params['run_id']}")
    return _ok(view.to_json())


async def _events(request: Request) -> Response:
    """The raw ledger, paged. The scrollback in a snapshot is not the ledger."""
    dash: Dashboard = request.app.state.dash
    store = dash.store(request.path_params["run_id"])
    if store is None:
        return _err("no such run")
    params = request.query_params
    entries = state.read_ledger(store)
    try:
        kinds = trace.parse_kinds(params.getlist("kind")) or None
    except ValueError as exc:
        return _err(str(exc), 400)
    selected = trace.select(
        entries,
        agent=params.get("agent"),
        kinds=kinds,
        # Quiet by default, like `qaas trace --quiet`: ~93% of a real ledger is
        # `tool_call`, and the decisions are what someone opened this to read.
        quiet=params.get("quiet", "1") not in ("0", "false"),
    )
    after = int(params.get("after", 0))
    limit = min(int(params.get("limit", 500)), 5000)
    window = selected[after : after + limit]
    return _ok(
        {
            "total": len(selected),
            "after": after,
            "entries": [
                {
                    "seq": after + i,
                    "at": e.at.isoformat(),
                    "agent": e.agent,
                    "kind": str(e.kind),
                    "detail": e.detail,
                    "text": trace.describe(e),
                }
                for i, e in enumerate(window)
            ],
        }
    )


async def _finding(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    store = dash.store(request.path_params["run_id"])
    if store is None:
        return _err("no such run")
    payload = state.envelope_json(store, request.path_params["envelope_id"])
    return _ok(payload) if payload is not None else _err("no such envelope")


async def _artifacts(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    store = dash.store(request.path_params["run_id"])
    if store is None:
        return _err("no such run")
    return _ok(state.artifact_names(store))


async def _artifact(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    run_id = request.path_params["run_id"]
    store = dash.store(run_id)
    if store is None:
        return _err("no such run")
    try:
        # The containment check lives in the store, not here. One implementation
        # of "does this path escape the artifact directory", used by every caller.
        path = store.resolve_artifact(f"artifact://{run_id}/{request.path_params['name']}")
    except ValueError as exc:
        return _err(str(exc), 400)
    if not path.is_file():
        return _err("no such artifact")
    return FileResponse(
        path,
        media_type=_CONTENT_TYPES.get(path.suffix.lower(), "text/plain; charset=utf-8"),
        headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
    )


async def _score(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    store = dash.store(request.path_params["run_id"])
    if store is None:
        return _err("no such run")
    if dash.ledger_path is None or not Path(dash.ledger_path).exists():
        return _err(
            "this target has no golden ledger, so there is nothing to score against"
        )
    from qaas.scorecard import GoldenLedger, score as score_run

    golden = GoldenLedger.load(Path(dash.ledger_path))
    card = score_run(store.envelopes(), golden, cost_usd=store.total_cost_usd())
    payload = card.summary()
    payload["matches"] = [
        {
            "golden_id": m.golden_id,
            "envelope_id": m.envelope_id,
            "score": m.score,
            "reported_severity": m.reported_severity,
            "expected_severity": m.expected_severity,
            "severity_delta": m.severity_delta,
        }
        for m in card.matches
    ]
    # Under its own key: `summary()` already uses `false_positives` for the
    # *count*, and overwriting an int with a list turns the metric tile into a
    # rendered array.
    payload["false_positive_ids"] = card.false_positives
    payload["missed_ids"] = card.missed
    return _ok(payload)


async def _map(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    payload = state.system_map(dash.root, request.query_params.get("version"))
    return _ok(payload) if payload is not None else _err("no system map yet — run MAPPER")


async def _stream(request: Request) -> Response:
    from sse_starlette.sse import EventSourceResponse

    dash: Dashboard = request.app.state.dash
    run_id = request.path_params["run_id"]
    watcher = dash.watcher(run_id)
    if watcher is None:
        return _err("no such run")
    client = watcher.attach()

    async def events():
        try:
            # The snapshot comes from the view the watcher already holds, so a
            # tab joining at line 40,000 costs one serialisation, not one reparse.
            yield {
                "event": "snapshot",
                "data": json.dumps(watcher.view.to_json(), default=_json_default),
            }
            # A run that is already over has nothing further to say, and the
            # stream must *end* rather than idle: an EventSource held open on a
            # finished run is a connection per tab that never closes, and the
            # browser reconnects to it forever.
            if watcher.done:
                yield {"event": "done", "data": json.dumps({"run_id": run_id})}
                return
            while True:
                message = await client.get()
                yield {
                    "event": message["event"],
                    "data": json.dumps(message["data"], default=_json_default),
                }
                if message["event"] == "done":
                    return
        finally:
            watcher.detach(client)

    return EventSourceResponse(events())


def build_app(dash: Dashboard) -> Starlette:
    """The whole HTTP surface, in one readable table."""
    routes = [
        Route("/", _index),
        Route("/api/runs", _runs),
        Route("/api/runs/live", _live),
        Route("/api/runs/{run_id}", _run),
        Route("/api/runs/{run_id}/events", _events),
        Route("/api/runs/{run_id}/stream", _stream),
        Route("/api/runs/{run_id}/findings/{envelope_id}", _finding),
        Route("/api/runs/{run_id}/artifacts", _artifacts),
        Route("/api/runs/{run_id}/artifacts/{name:path}", _artifact),
        Route("/api/runs/{run_id}/score", _score),
        Route("/api/runs/{run_id}/map", _map),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes)
    app.state.dash = dash
    return app
