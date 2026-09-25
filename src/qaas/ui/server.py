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
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Mapping

import anyio
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
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

#: Put on a client's queue when it is dropped for being too slow, so its
#: generator wakes, ends the response, and the browser reconnects.
_CLOSED = {"event": "done", "data": {"dropped": True, "reason": "client fell behind"}}


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
        #: When the view stopped following the ledger. See `Dashboard._evict_if_stale`.
        self.done_at: float | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None or self.done:
            return
        if self.view.completed or self.view.interrupted:
            # Nothing to tail. `tail(from_start=False)` seeks to EOF, and this
            # run's `run_finished` is already behind that offset -- so the
            # thread would poll a file that will never change again, forever,
            # for every finished run anyone opened. An interrupted run is the
            # same file with no `run_finished` in it: the same forever, minus
            # even the line that would have ended it.
            self.done, self.done_at = True, time.time()
            return
        # EOF is taken *here*, on the caller's thread, not inside `_tail`.
        # Seeking in the thread leaves a window between the view being built and
        # the thread being scheduled, and any entry appended in it is behind the
        # seek and absent from the view -- dropped from the live stream with
        # nothing reporting it. On a loaded CI runner that window is wide enough
        # to lose a line reliably.
        #
        # And not from `stat()` either, when the replay can say. `stat()` after
        # the replay still left a window -- a line appended between the read and
        # the stat was in neither half -- and it put the offset past a
        # half-written last line the replay had skipped, so the tail skipped it
        # too. Both were lost for good, in the view cached for every tab. The
        # replay's own offset is the last newline it parsed (`state.replay`).
        path = self.view.store.ledger_path
        if self.view.offset is not None:
            self._start_offset = self.view.offset
        else:
            self._start_offset = path.stat().st_size if path.exists() else 0
        self._thread = threading.Thread(target=self._tail, daemon=True)
        self._thread.start()
        self._drain = asyncio.get_running_loop().create_task(self._pump())

    def _tail(self) -> None:
        # `from_start=False`: the view was already built by replaying the file,
        # so starting at EOF is what makes the two halves meet exactly once.
        # Replaying from the start here would double-count every counter.
        try:
            for entry in trace.tail(
                self.view.store, from_start=False, poll=self.poll, stop_on_finish=True,
                start_offset=self._start_offset,
                # The view already read the run's cap, so the tail need not
                # re-read the file to learn when silence means death.
                stale_after=trace.stale_after_s(self.view.wall_clock_s),
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
                self.done, self.done_at = True, time.time()
                # The tail also ends when the ledger goes stale (a killed run
                # never writes `run_finished`). Announcing `completed: True`
                # for that told the page a run had finished cleanly when it had
                # died; the view says which it was.
                if not self.view.completed:
                    self.view.interrupted = True
                self._broadcast(
                    "done",
                    {
                        "run_id": self.view.run_id,
                        "completed": self.view.completed,
                        "interrupted": self.view.interrupted,
                    },
                )
                return
            before = {n: a.status for n, a in self.view.agents.items()}
            self.view.apply(entry)
            self._emit(entry, before)

    def _emit(self, entry, before: Mapping[str, str]) -> None:
        for event, data in events_for(self.view, entry, before):
            self._broadcast(event, data)

    def _broadcast(self, event: str, data: Any) -> None:
        message = {"event": event, "data": data}
        for client in list(self.clients):
            try:
                client.put_nowait(message)
            except asyncio.QueueFull:
                # A tab that cannot keep up is dropped rather than allowed to
                # back-pressure the tail thread into unbounded memory -- but it
                # has to be *told*. Discarding it alone left the per-client
                # generator parked on `await client.get()` with nothing that
                # would ever put to that queue again: the SSE response never
                # completed and never errored, so `EventSource` saw an open
                # connection and never reconnected. A frozen tab that looks live
                # is worse than a broken one, which at least reloads.
                self.clients.discard(client)
                try:
                    client.put_nowait(_CLOSED)
                except asyncio.QueueFull:
                    pass

    def attach(self) -> asyncio.Queue:
        client: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients.add(client)
        return client

    def detach(self, client: asyncio.Queue) -> None:
        self.clients.discard(client)


def events_for(view: state.RunView, entry, before: Mapping[str, str]) -> list[tuple[str, Any]]:
    """What the page is told after `entry` was applied to `view`.

    One function for the live tail and for a replay, so a replayed run is drawn
    by exactly the code that draws a live one -- the page cannot tell them apart
    except by the `replay` flag it is shown.
    """
    out: list[tuple[str, Any]] = []
    by_id = {f.id: f for f in view.findings}
    if entry.kind not in trace.QUIET_KINDS:
        out.append(("ledger", view.recent[-1]))
    # A finding is sent by id and the page replaces it if it has one. It sent
    # `findings[-1]` for every `envelope` line -- but an envelope is re-logged
    # whenever it is revised (a ticket key stamped, a reproduction recorded),
    # so the latest finding was pushed again as new: one defect listed three
    # times until the page was reloaded.
    if entry.kind == LedgerKind.ENVELOPE:
        finding = by_id.get(entry.detail.get("envelope_id"))
        if finding is not None:
            out.append(("finding", finding.to_json()))
    if entry.kind == LedgerKind.DENIAL and view.denials:
        # The counter and the guardrails tab read this list, and nothing sent
        # it: both said 0 on a live run refusing things in the feed beside them.
        out.append(("denial", view.denials[-1].to_json()))
    if entry.kind in (LedgerKind.TICKET, LedgerKind.VERDICT, LedgerKind.VERIFIED):
        key = entry.detail.get("key") or entry.detail.get("ticket_key")
        if key and str(key) in view.tickets:
            ticket = view.tickets[str(key)]
            out.append(("ticket", ticket.to_json()))
            # The finding it files now carries the key; say so on its row.
            filed = by_id.get(ticket.envelope_id)
            if filed is not None:
                out.append(("finding", filed.to_json()))
    # Agent cards and the header change on almost every line, so the patch
    # carries only the agents whose row actually moved.
    moved = {
        n: a.to_json()
        for n, a in view.agents.items()
        if before.get(n) != a.status or (entry.agent == n)
    }
    out.append((
        "patch",
        {
            "elapsed_s": round(view.elapsed_s, 1),
            "cost_usd": round(view.cost_usd, 4),
            "phase": view.phase,
            "phase_status": view.phase_status,
            "counts": view.counts,
            "completed": view.completed,
            "interrupted": view.interrupted,
            "stopped_early": view.stopped_early,
            "escalations": view.escalations,
            "replay": view.replay_speed,
            "last_at": state._iso(view.last_at),
            # The header's run facts. A live page has them from its snapshot; a
            # replay starts from an empty one and learns them from `run_started`,
            # and without `started` the timeline had no origin and drew nothing.
            "started": state._iso(view.started),
            "mode": view.mode,
            "wall_clock_s": view.wall_clock_s,
            "target_name": view.target_name,
            "target_sha": view.target_sha,
            "target_dirty": view.target_dirty,
            "agents": moved,
        },
    ))
    return out


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
        cfg: Any = None,
        config_dirs: list[Path] | None = None,
        state_root: Path | str | None = None,
        target_root: Path | str | None = None,
    ) -> None:
        self.root = Path(root)
        self.specs = dict(specs or {})
        #: The whole SystemConfig, for the configuration half of the page. None
        #: is a legitimate state: `pip install` then `qaas dashboard` in a
        #: directory holding only `.qaas/runs/` must still open, exactly as the
        #: agent grid renders without specs.
        self.cfg = cfg
        #: The config search path, nearest first. The override file is written
        #: to the nearest writable layer, which is the project's own `.qaas`.
        #:
        #: *This* list, everywhere on the page: the override route validates and
        #: writes against it and `/api/config` reports it. `/api/config` used to
        #: re-resolve the workspace from the cwd instead, so a dashboard started
        #: with `--config` showed one search path and wrote to another.
        self.config_dirs = list(config_dirs or [])
        #: Where `.qaas/config` is created when no layer on the path may take an
        #: override -- see `config_write.override_layer`. The run root by
        #: default, which is the state directory the page is reading.
        self.state_root = Path(state_root) if state_root is not None else self.root
        self.min_confidence = min_confidence
        #: The golden ledger of the active target, when it has one. Most targets
        #: never will -- it is a property of a calibration app, not of an
        #: application (cli.py:1229-1237).
        self.ledger_path = ledger_path
        #: The application that golden ledger describes. A run records the root
        #: it ran against, and one against any other application is refused a
        #: score rather than measured against the wrong oracle (`_score`).
        if target_root is None and cfg is not None:
            try:
                profile = getattr(cfg, "profile", None)
                target_root = profile.root_path() if profile is not None else None
            except Exception:
                target_root = None
        self.target_root = Path(target_root) if target_root is not None else None
        self.poll = poll
        self.watchers: dict[str, RunWatcher] = {}

    def workspace(self):
        """The resolved workspace, with this dashboard's config path in it."""
        import dataclasses

        from qaas.paths import Workspace

        workspace = Workspace.resolve()
        # The state root shown is the one this page reads runs from, which is
        # not the cwd's `.qaas` when `--root` said otherwise.
        workspace = dataclasses.replace(workspace, state_root=self.state_root.resolve())
        if self.config_dirs:
            workspace = dataclasses.replace(
                workspace, config_dirs=tuple(Path(d) for d in self.config_dirs)
            )
        return workspace

    def store(self, run_id: str) -> RunStore | None:
        store = RunStore(run_id, root=self.root, create=False)
        # `create=False` matters: constructing a store used to mkdir, so one
        # mistyped URL left a permanent empty run in `qaas runs` (store.py:126).
        return store if store.dir.exists() else None

    def _evict_if_stale(self, run_id: str) -> None:
        """Drop a finished watcher whose run has started moving again.

        `RunWatcher.start` deliberately spawns no tail thread for a completed
        run, so that watcher's view is frozen at the moment it was built --
        correct, until `qaas run --run-id <existing>` resumes the run and appends
        to the same ledger. Nothing ever removed it, so from then on the
        dashboard served a dead snapshot of a live run, for the lifetime of the
        process, and a reload did not help because the cache was hit first.
        """
        watcher = self.watchers.get(run_id)
        if watcher is None or not watcher.done:
            return
        store = self.store(run_id)
        if store is None:
            return
        # Live now, or written since the view stopped following it. The second
        # half is the case the first missed: a resume that had already
        # *finished* by the next page load is not live, so a real resumed run
        # -- a fix cycle appended to the run that filed its tickets -- kept
        # showing the first invocation's $5 and three agents until the
        # dashboard was restarted.
        try:
            grown = watcher.done_at is not None and store.ledger_path.stat().st_mtime > watcher.done_at
        except OSError:
            grown = False
        if grown or state.is_live(store):
            self.watchers.pop(run_id, None)

    def view(self, run_id: str) -> state.RunView | None:
        """The live view if one is being watched, else a fresh replay."""
        self._evict_if_stale(run_id)
        watcher = self.watchers.get(run_id)
        if watcher is not None:
            return watcher.view
        store = self.store(run_id)
        if store is None:
            return None
        return state.load(store, self.specs, min_confidence=self.min_confidence)

    def watcher(self, run_id: str) -> RunWatcher | None:
        self._evict_if_stale(run_id)
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


def _int_param(raw: str | None, default: int, *, low: int = 0, high: int | None = None) -> int:
    """A query-string integer, clamped, never an exception.

    `int(request.query_params.get(...))` was unguarded in three places, so
    `?limit=abc` raised `ValueError` out of the handler as a 500 with a
    traceback -- while the branch immediately above it returned a tidy 400
    naming the legal values for a bad `kind`. A negative `after` was worse than
    untidy: it slices from the end of the list and then reports the resulting
    cursors as positive offsets, so paging walked backwards through the ledger
    and said it was going forwards.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    value = max(low, value)
    return min(value, high) if high is not None else value


async def _index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html")


async def _runs(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    limit = _int_param(request.query_params.get("limit"), 50, low=1, high=1000)
    # Off the event loop. This is file I/O across every listed run, and while it
    # ran inline every SSE stream on the page stopped delivering -- a frozen
    # live view is indistinguishable from a stalled run.
    return _ok(await asyncio.to_thread(state.list_runs_summary, dash.root, limit))


async def _live(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    run_id = await asyncio.to_thread(state.pick_run, dash.root)
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
    after = _int_param(params.get("after"), 0, low=0)
    limit = _int_param(params.get("limit"), 500, low=1, high=5000)
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
    # The golden ledger is the *configured* target's, and a run records the
    # target it actually ran against. They differ for every `qaas run --repo`
    # run, and this scored all of them against the demo's `defects.yaml` anyway:
    # recall and precision measured against another application's defect list,
    # rendered as metric tiles. A wrong oracle is worse than none, because its
    # numbers look like numbers -- the same reason `qaas score` grew `--target`.
    ran_against = state.run_started_detail(store).get("target_root")
    if dash.target_root is not None and ran_against:
        if not _same_path(ran_against, dash.target_root):
            return _err(
                f"this run was against {ran_against}, but the golden ledger belongs "
                f"to the configured target at {dash.target_root}. Scoring one "
                "application's findings against another's defect list would produce "
                "numbers that mean nothing. Score it with `qaas score "
                f"{store.run_id} --target <profile>` using that application's own "
                "profile, if it has a ledger.",
                409,
            )
    from qaas.scorecard import GoldenLedger, GoldenLedgerError, score as score_run

    try:
        golden = GoldenLedger.load(Path(dash.ledger_path))
    except (GoldenLedgerError, ValueError, OSError) as exc:
        # A hand-edited `defects.yaml` that is empty or not a mapping was an
        # AttributeError here, and a 500 with nothing on the tab.
        return _err(f"the golden ledger at {dash.ledger_path} cannot be read: {exc}", 422)
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


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return str(a) == str(b)


async def _map(request: Request) -> Response:
    dash: Dashboard = request.app.state.dash
    try:
        payload = state.system_map(dash.root, request.query_params.get("version"))
    except state.UnknownMapVersion as exc:
        # `?version=../../secret` returned `<root>/../secret.json`. A version is
        # now one the map store lists, or it is refused.
        return _err(str(exc), 404)
    return _ok(payload) if payload is not None else _err("no system map yet — run MAPPER")


async def _stream(request: Request) -> Response:
    from sse_starlette.sse import EventSourceResponse

    dash: Dashboard = request.app.state.dash
    run_id = request.path_params["run_id"]
    watcher = dash.watcher(run_id)
    if watcher is None:
        return _err("no such run")
    # Attach and snapshot together, with no `await` between them. The snapshot
    # used to be serialised later, inside the generator, after the response had
    # started -- and `_pump` runs on this loop, so every entry it applied in
    # that gap was both in the snapshot *and* already queued for this client as
    # a delta. The page then pushed it twice: a doubled feed line, a finding
    # listed twice. On one loop turn nothing can be applied between the two.
    client = watcher.attach()
    snapshot = json.dumps(watcher.view.to_json(), default=_json_default)

    async def events():
        try:
            # The snapshot comes from the view the watcher already holds, so a
            # tab joining at line 40,000 costs one serialisation, not one reparse.
            yield {"event": "snapshot", "data": snapshot}
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


#: A replay compresses the run by `speed`, and caps any single pause at this.
#: A real run holds hours of nothing -- a provider limit waited out, an agent
#: thinking -- and replayed faithfully that is a frozen screen.
REPLAY_MAX_GAP_S = 1.5


async def _pause(seconds: float) -> None:
    """The replay's pacing, separate so a test can measure it without waiting."""
    await asyncio.sleep(seconds)


async def _replay(request: Request) -> Response:
    """Play a finished run back as if it were live, `speed` times faster.

    Built from the ledger the way the live view is -- an empty `RunView`, the
    same `apply`, the same events -- so what it shows is what the run showed.
    Read-only like every other GET: it paces the file, and writes nothing.
    """
    from sse_starlette.sse import EventSourceResponse

    dash: Dashboard = request.app.state.dash
    run_id = request.path_params["run_id"]
    store = dash.store(run_id)
    if store is None:
        return _err("no such run")
    speed = _int_param(request.query_params.get("speed"), 60, low=1, high=10_000)
    entries = state.read_ledger(store)
    view = state.RunView(
        run_id=run_id, store=store, specs=dash.specs,
        min_confidence=dash.min_confidence, replay_speed=speed,
    )

    async def events():
        yield {"event": "snapshot", "data": json.dumps(view.to_json(), default=_json_default)}
        previous = None
        for entry in entries:
            if previous is not None:
                pause = (entry.at - previous).total_seconds() / speed
                if pause > 0:
                    await _pause(min(pause, REPLAY_MAX_GAP_S))
            previous = entry.at
            before = {n: a.status for n, a in view.agents.items()}
            view.apply(entry)
            for event, data in events_for(view, entry, before):
                yield {"event": event, "data": json.dumps(data, default=_json_default)}
        yield {"event": "done", "data": json.dumps({"run_id": run_id, "replay": True})}

    return EventSourceResponse(events())


async def _config(request: Request) -> Response:
    """Everything this installation is configured to do, in one payload.

    Rebuilt per request rather than cached: a dashboard left open while someone
    edits `system.yaml` should show the edit on reload. It is a GET like every
    other route here -- the page reports configuration, it cannot change it.
    """
    from qaas.ui.config_view import ConfigView

    dash: Dashboard = request.app.state.dash
    return JSONResponse(ConfigView(dash.cfg, dash.workspace()).to_json())


async def _set_override(request: Request) -> Response:
    """The one route on this page that writes, and the only one that ever will.

    It changes tuning -- which model an agent runs, a turn cap, a threshold --
    by merging into `overrides.yaml`. It cannot change what an agent is
    *allowed to do*: `config_write` refuses anything outside
    `TUNABLE_AGENT_FIELDS`, so the write-permission matrix stays a file that a
    person edits. That distinction is the whole reason a POST is acceptable
    here at all, and `test_the_write_route_cannot_touch_a_policy` is what keeps
    it true.
    """
    from qaas.ui.config_write import OverrideError, effective_dirs, reset, set_values

    dash: Dashboard = request.app.state.dash
    if not dash.config_dirs:
        return JSONResponse({"error": "no config directory is writable here"}, status_code=409)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)
    # `[]`, `"x"` and `null` are all valid JSON, and `body.get` on them was an
    # AttributeError and a 500. The shape is checked here and the field types in
    # `set_values`, so a malformed request is always a 400 that says why.
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "expected a JSON object: {section, agent, values} or {reset: true}"},
            status_code=400,
        )
    if "reset" in body and not isinstance(body["reset"], bool):
        return JSONResponse({"error": "reset must be true or false"}, status_code=400)

    values = body.get("values")
    try:
        if body.get("reset"):
            data = reset(dash.config_dirs, state_root=dash.state_root)
        else:
            data = set_values(
                dash.config_dirs,
                section=body.get("section") or "",
                key=body.get("agent"),
                values={} if values is None else values,
                state_root=dash.state_root,
            )
    except OverrideError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        # Unwritable is a fact about this machine, not a crash: say which file.
        return JSONResponse(
            {"error": f"could not write the overrides file: {exc}"}, status_code=500
        )

    # The layer written to may be one this list did not have yet -- the
    # `<state_root>/config` a first override creates -- and the reload below and
    # `/api/config` must both see it.
    dash.config_dirs = effective_dirs(dash.config_dirs, state_root=dash.state_root)

    # Reload so the response is what a run would now see, not what was asked
    # for. They differ whenever a nearer layer still shadows the field.
    from qaas.config import load_config

    try:
        dash.cfg = load_config(search=dash.config_dirs)
    except Exception:
        pass
    return JSONResponse({"overrides": data})


def build_app(dash: Dashboard) -> Starlette:
    """The whole HTTP surface, in one readable table."""
    routes = [
        Route("/", _index),
        Route("/api/runs", _runs),
        Route("/api/runs/live", _live),
        Route("/api/runs/{run_id}", _run),
        Route("/api/runs/{run_id}/events", _events),
        Route("/api/runs/{run_id}/stream", _stream),
        Route("/api/runs/{run_id}/replay", _replay),
        Route("/api/runs/{run_id}/findings/{envelope_id}", _finding),
        Route("/api/runs/{run_id}/artifacts", _artifacts),
        Route("/api/runs/{run_id}/artifacts/{name:path}", _artifact),
        Route("/api/runs/{run_id}/score", _score),
        Route("/api/runs/{run_id}/map", _map),
        Route("/api/config", _config),
        Route("/api/config/override", _set_override, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(LocalOnly)])
    app.state.dash = dash
    return app


#: Hosts this page may be reached as. A loopback literal, with or without a
#: port. `localhost` is included because that is what a person types.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0"})


def _host_name(host: str) -> str:
    """The name in a Host header or an origin's netloc, without its port.

    An IPv6 literal is bracketed precisely so its colons are not a port
    separator -- `[::1]:7777`. This split on a single colon only, so a
    bracketed address with a port kept its port, matched nothing in the set,
    and a dashboard served on `--host ::1` answered its own page with 421.
    """
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        # Only a port may follow the bracket: `[::1].evil.example` is a name,
        # not the loopback literal it starts with.
        if end == -1 or not _PORT_RE.fullmatch(host[end + 1 :]):
            return host
        return host[1:end]
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name if _PORT_RE.fullmatch(":" + port) else host
    return host  # a bare IPv6 literal, or a name with no port


#: What may follow a host name in a Host header: nothing, or `:<digits>`.
_PORT_RE = re.compile(r"(?::\d*)?")


def _host_is_loopback(host: str) -> bool:
    name = _host_name(host)
    return name in _LOOPBACK_HOSTS or host.lower() in _LOOPBACK_HOSTS


class LocalOnly(BaseHTTPMiddleware):
    """Two checks, both about a page that is not as private as it looks.

    The server binds 127.0.0.1, which stops a *network* peer and does nothing
    about the browser already running as this user. It holds the run ledger --
    agent-authored prose, target source excerpts, ticket keys -- and a POST route
    that rewrites `overrides.yaml`. Both were reachable from any page the user
    happened to have open:

      * **DNS rebinding.** A site on attacker-controlled DNS with a short TTL
        rebinds its own name to 127.0.0.1 and becomes same-origin with this app,
        at which point every GET is readable. Starlette answers on whatever
        `Host` it is given, so the fix is to require a loopback literal --
        `evil.example` never is one, however it resolves.
      * **CSRF on the one write route.** `_set_override` reads
        `await request.json()`, which does not check `Content-Type`; a
        cross-origin `fetch` with `text/plain` is a CORS-*simple* request, so it
        is sent without a preflight and the write lands. The attacker cannot
        read the reply and does not need to -- setting `min_confidence_to_file`
        to 0 or repointing an agent's model is the whole payload.

    This does not make the dashboard a security boundary, and it is not meant
    to. It closes the two doors that were open by default.
    """

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host and not _host_is_loopback(host):
            return JSONResponse(
                {"error": f"refusing a request for host '{host}'. This dashboard "
                          "answers on loopback only."},
                status_code=421,
            )

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and not _host_is_loopback(urlsplit(origin).netloc):
                return JSONResponse(
                    {"error": "cross-origin writes are refused."}, status_code=403
                )
            site = request.headers.get("sec-fetch-site")
            if site is not None and site not in ("same-origin", "none"):
                return JSONResponse(
                    {"error": f"refusing a {site} write."}, status_code=403
                )
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
            if content_type != "application/json":
                # A simple request cannot set this header, so requiring it costs
                # an honest caller nothing and denies a cross-origin form post.
                return JSONResponse(
                    {"error": "writes must be sent as application/json."}, status_code=415
                )
        return await call_next(request)
