"""Reading the run ledger back: the timeline behind `qaas trace` and `qaas show`.

The ledger has always been the richest thing a run produces -- every dispatch,
every tool call, every guardrail refusal, every verdict, appended in order (§8).
Nothing could read it. `qaas show` looked at exactly one of the 28 kinds
(`denial`) and printed no cost, no mode, no duration, no verdicts. So the audit
trail existed and the audit did not.

This module is *read-only over an append-only file*. It never writes, and it
holds no opinion about what a run should have done -- it renders what the run
recorded. The one performance rule that shapes it: `RunStore.ledger(kind)` is a
full-file linear scan, so callers read the file **once** here and filter the
list in memory, rather than scanning it once per kind of interest.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from qaas.store import LedgerEntry, LedgerKind, RunStore

#: How much of a rendered detail line to keep before truncating. A `verdict`
#: carries paragraphs of observed behaviour and a `denial` carries the agent's
#: whole Bash command; a timeline that wraps for ten lines is not a timeline.
DETAIL_WIDTH = 110

#: Fields worth showing per kind, in the order they read best. Anything not
#: listed falls back to "every key, in insertion order" -- a new kind is still
#: legible before anyone teaches this table about it.
DETAIL_FIELDS: dict[LedgerKind, tuple[str, ...]] = {
    LedgerKind.RUN_STARTED: ("mode", "agents", "budget_usd", "target_sha", "target_dirty"),
    LedgerKind.RUN_FINISHED: ("agents_run", "cost_usd", "failed", "stopped_early", "resume"),
    LedgerKind.QUOTA_WAIT: ("until", "seconds"),
    LedgerKind.AGENT_STARTED: ("model", "task_chars", "task_preview"),
    LedgerKind.AGENT_FINISHED: ("subtype", "cost_usd", "num_turns", "envelopes", "error"),
    LedgerKind.TOOL_CALL: ("tool", "allowed"),
    LedgerKind.DENIAL: ("tool", "reason"),
    LedgerKind.ENVELOPE: ("severity", "domain", "confidence", "envelope_id"),
    LedgerKind.REPRODUCTION: ("status", "fileable", "flake_rate", "envelope_id"),
    LedgerKind.TICKET: ("action", "key", "severity", "envelope_id"),
    LedgerKind.VERDICT: ("ticket_key", "verdict", "observed"),
    LedgerKind.REVIEW: ("ticket_key", "decision", "reasoning"),
    LedgerKind.VERIFIED: ("ticket_key", "reopens"),
    LedgerKind.REOPENED: ("ticket_key", "attempt"),
    LedgerKind.REVIEW_ROUND_TRIP: ("ticket_key", "trip"),
    LedgerKind.ESCALATION: ("reason",),
    LedgerKind.QUOTA_EXHAUSTED: ("reason", "unfiled_findings", "resume"),
    LedgerKind.HUMAN_DECISION: ("ticket_key", "decision", "note", "author"),
    LedgerKind.SKIPPED: ("reason",),
    LedgerKind.VCS: ("action", "branch", "path", "sha"),
    LedgerKind.ENV: ("action", "services", "role", "fixture"),
    LedgerKind.SYSTEM_MAP: ("version", "sections"),
    LedgerKind.CONTRACT_TEST: ("endpoint", "path"),
    LedgerKind.DEFECT_MEMORY: ("action", "ticket_key", "fingerprint"),
    LedgerKind.AGENT_ERROR: ("error",),
    LedgerKind.STOP_BLOCKED: ("missing",),
    LedgerKind.CONTRACT_UNMET: ("missing", "reason"),
    LedgerKind.SKILLS_MISSING: ("missing", "declared"),
    LedgerKind.TOOL_ERROR: ("tool",),
    LedgerKind.DRY_RUN: ("tool",),
    LedgerKind.REGRESSION: ("fingerprint", "ticket_key"),
}


def read_ledger(store: RunStore) -> list[LedgerEntry]:
    """The whole ledger, in order, in one pass. Filter the result, not the file."""
    return list(store.ledger())


#: How often `tail` looks for new ledger lines. A run writes a line every few
#: seconds at most, so polling faster buys nothing and spins a CPU; polling
#: slower makes `--follow` feel broken while an agent is thinking.
POLL_INTERVAL_S = 0.5


# -- is anything still writing this? ----------------------------------------
#
# "More `run_started` than `run_finished`" is how every reader decides a run is
# live, and it is right for every run that ended in Python. It is wrong for one
# that ended in the operating system: a ^C'd, OOM-killed or closed-laptop run
# never wrote its closing line, so its ledger said "live" forever -- the
# dashboard pulsed over it, picked it as *the* live run ahead of the one
# actually running, and `qaas trace --follow` polled it until someone pressed
# ^C again. The router now closes interrupted runs itself, but the ledgers
# already on disk are what people open, so the reader needs its own answer.
#
# The answer is the file's age. `_dispatch` preempts at the run's wall-clock
# cap and filing and reporting run against the same cap, so no run can go on
# writing for longer than `wall_clock_s` -- a ledger that has been silent for
# longer than that, plus a margin, is not being written by anything.

#: Slack past a run's own wall-clock cap before its silence means it is dead:
#: the finalising work after the cap (outcomes, the report, `run_finished`
#: itself) and a clock that is not quite the router's.
STALE_MARGIN_S = 600

#: The silence that means dead when the run recorded no cap at all -- a ledger
#: from before `run_started` carried `wall_clock_s`. The longest cap any shipped
#: mode sets (`system.yaml`: 28800s), so no run that could still be alive is
#: called dead on this rule.
STALE_WITHOUT_CAP_S = 8 * 3600


def stale_after_s(wall_clock_s: Any) -> float:
    """How long a ledger may sit unwritten and still belong to a live run."""
    numeric = isinstance(wall_clock_s, (int, float)) and not isinstance(wall_clock_s, bool)
    if numeric and wall_clock_s > 0:
        return float(wall_clock_s) + STALE_MARGIN_S
    return float(STALE_WITHOUT_CAP_S)


def is_stale(path: Path, wall_clock_s: Any = None, *, now: float | None = None) -> bool:
    """Whether a ledger has been silent too long to have a live writer.

    Says nothing about whether the run *finished* -- that is the count of
    `run_started` against `run_finished`. This is the second question, asked of
    a run the count calls live: is anything actually still writing it. A file
    that does not exist is not stale; following a run that has not started yet
    is the normal case (see `tail`).
    """
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime > stale_after_s(wall_clock_s)


#: `{"at":"<timestamp>"` -- how every ledger line opens, because `LedgerEntry`
#: declares `at` first and `model_dump_json` writes fields in declaration order.
#: Checked in front of a `kind` match so a *nested* `"kind":"run_started"` in
#: some tool call's arguments is not counted as a run starting.
_LINE_HEAD_RE = re.compile(rb'\{"at":"[^"\n]*"')


def run_edges(data: bytes) -> tuple[list[int], list[int]]:
    """Line offsets of every top-level `run_started` and `run_finished` in `data`.

    Raw bytes, deliberately. Asking "is this run live" or "which mode did it
    resume as" by validating every line through pydantic is what made `/api/runs`
    take 3.2s over forty 30k-line ledgers and freeze every SSE stream while it
    did. `bytes.find` over the same data is milliseconds, and there are only a
    handful of these lines per ledger to confirm.
    """

    def find(kind: bytes) -> list[int]:
        needle = b'","kind":"' + kind + b'"'
        found: list[int] = []
        pos = data.find(needle)
        while pos != -1:
            start = data.rfind(b"\n", 0, pos) + 1
            if _LINE_HEAD_RE.fullmatch(data, start, pos + 1):
                found.append(start)
            pos = data.find(needle, pos + 1)
        return found

    return find(b"run_started"), find(b"run_finished")


def line_at(data: bytes, start: int) -> dict[str, Any] | None:
    """The JSON object on the line beginning at `start`, or None if it will not parse."""
    end = data.find(b"\n", start)
    try:
        parsed = json.loads(data[start : end if end != -1 else len(data)])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def last_wall_clock(path: Path, upto: int | None = None) -> Any:
    """`wall_clock_s` from the last `run_started` in the first `upto` bytes."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read() if upto is None else handle.read(upto)
    except OSError:
        return None
    starts, _ = run_edges(data)
    if not starts:
        return None
    line = line_at(data, starts[-1]) or {}
    detail = line.get("detail")
    return detail.get("wall_clock_s") if isinstance(detail, dict) else None


def tail(
    store: RunStore,
    *,
    from_start: bool = True,
    poll: float = POLL_INTERVAL_S,
    stop_on_finish: bool = True,
    timeout_s: float | None = None,
    start_offset: int | None = None,
    stale_after: float | None = None,
) -> Iterator[LedgerEntry]:
    """Yield ledger entries as they are appended, for `qaas trace --follow`.

    The ledger is append-only, which is what makes this safe: a reader can hold
    a byte offset and never be wrong about it. Two rules follow from that and
    both matter.

    Only whole lines are parsed. A run can be mid-`write` when this reads, and
    half a JSON object is not an entry -- the offset advances to the last
    newline, so the remainder is picked up on the next poll rather than raising.

    The file may not exist yet. Following a run that is still starting is the
    normal case, not an error, so a missing ledger is waited for.

    With `stop_on_finish`, a ledger that has gone stale (`is_stale`) ends the
    follow as `run_finished` would. `stale_after` is that silence in seconds
    when the caller already knows it; otherwise it is read from the run's own
    `run_started`.
    """
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    offset = 0
    if start_offset is not None:
        # Taken by the caller, at a moment it chose. `from_start=False` seeks to
        # EOF *here*, which is inside whatever thread this generator runs on --
        # so an entry appended between the caller building its view and this
        # thread being scheduled lands behind the seek and is in neither half.
        # The dashboard hit exactly that: a ledger line written right after
        # `watcher.start()` was dropped from the live stream, and the test that
        # noticed read as a flaky timeout because the entry simply never came.
        offset = start_offset
    elif not from_start and store.ledger_path.exists():
        offset = store.ledger_path.stat().st_size
    # Bytes, not text. This read the file in text mode and took `tell()` as the
    # next offset, so a poll landing inside a multi-byte character decoded half
    # of it as U+FFFD, advanced past it, and decoded the other half as another
    # U+FFFD next time: the entry arrived with its prose mangled. And the
    # dashboard hands this a *byte* offset from its replay, which only means the
    # same thing to a binary read. Lines are split on b"\n" and decoded whole.
    pending = b""
    #: Runs started minus runs finished, over what this reader has actually
    #: seen. With `from_start=False` the earlier session's `run_started` is
    #: behind the seek, so this stays at 0 and the next `run_finished` still
    #: ends the follow -- which is right, because that is the run ending.
    open_runs = 0
    if stale_after is None and offset > 0:
        # The run's `run_started` is behind the seek, so its cap has to be read
        # from the part of the file this reader is not going to yield.
        stale_after = stale_after_s(last_wall_clock(store.ledger_path, offset))

    while True:
        chunk = b""
        if store.ledger_path.exists():
            with store.ledger_path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            offset += len(chunk)
            pending += chunk
            *lines, pending = pending.split(b"\n")  # the tail with no newline yet: not an entry
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    entry = LedgerEntry.model_validate_json(raw.decode("utf-8", errors="replace"))
                except Exception:
                    # A line this reader cannot parse is a line a future version
                    # wrote. Skipping it keeps the follow alive; killing the
                    # view over one unknown entry would not.
                    continue
                yield entry
                # Counted, not latched. A resumed run legitimately contains
                # `run_started … run_finished … run_started …`, and returning on
                # the *first* `run_finished` meant `qaas trace --follow` quit
                # while the resumed run was still writing -- and, with
                # `from_start=True` as the default, quit almost immediately
                # because the old line was already in the file.
                if entry.kind == LedgerKind.RUN_STARTED:
                    open_runs += 1
                    # A resume restarts the clock and may run under a different
                    # mode's cap; the newest `run_started` is the one in force.
                    stale_after = stale_after_s(entry.detail.get("wall_clock_s"))
                elif entry.kind == LedgerKind.RUN_FINISHED:
                    open_runs -= 1
                    if stop_on_finish and open_runs <= 0:
                        return
        # A run killed by the operating system never writes `run_finished`, so
        # the count above never reaches zero and this polled a dead file until
        # someone pressed ^C. Silence longer than the run's own cap allows is
        # the run ending too. Checked only on a poll that read nothing, so it is
        # one `stat` per idle poll and never delays an entry.
        if stop_on_finish and not chunk and store.ledger_path.exists():
            limit = stale_after if stale_after is not None else stale_after_s(None)
            try:
                silent_for = time.time() - store.ledger_path.stat().st_mtime
            except OSError:
                silent_for = 0.0
            if silent_for > limit:
                return
        if deadline is not None and time.monotonic() >= deadline:
            return
        time.sleep(poll)


def parse_kinds(names: Iterable[str]) -> list[LedgerKind]:
    """Validate `--kind` arguments against the enum.

    Raises ValueError naming the offender and the legal set, because "no output"
    is what a mistyped filter used to look like and it is indistinguishable from
    "this run has none of those".
    """
    kinds: list[LedgerKind] = []
    for name in names:
        try:
            kinds.append(LedgerKind(name))
        except ValueError:
            legal = ", ".join(sorted(k.value for k in LedgerKind))
            raise ValueError(f"unknown ledger kind {name!r}. Known kinds: {legal}") from None
    return kinds


#: What `--quiet` drops. A real run logs ~2400 `tool_call` lines against ~150 of
#: everything else, and every one of them is an agent reading a file. Dropping
#: them leaves what an agent *decided*: what it found, what it was refused, what
#: it filed, what it verdicted. Never dropped when asked for by `--kind`.
QUIET_KINDS = frozenset({LedgerKind.TOOL_CALL, LedgerKind.DRY_RUN})


def select(
    entries: Sequence[LedgerEntry],
    *,
    agent: str | None = None,
    kinds: Sequence[LedgerKind] | None = None,
    quiet: bool = False,
) -> list[LedgerEntry]:
    """Filter in memory. Agent match is case-insensitive; agent names are shouted."""
    wanted = set(kinds) if kinds else None
    name = agent.upper() if agent else None
    # "Never dropped when asked for by `--kind`" is what QUIET_KINDS documents,
    # and `select` applied the quiet filter unconditionally before the `wanted`
    # test -- so `qaas trace --quiet --kind tool_call` returned nothing at all,
    # the one combination where the flags mean something specific together.
    # Subtracting makes an explicit request win, as documented.
    hidden = (QUIET_KINDS - (wanted or frozenset())) if quiet else frozenset()
    return [
        e for e in entries
        if e.kind not in hidden
        and (wanted is None or e.kind in wanted)
        and (name is None or (e.agent or "").upper() == name)
    ]


def _short(value: Any) -> str:
    if isinstance(value, list):
        return f"[{len(value)}]" if len(value) > 4 else ", ".join(str(v) for v in value)
    if isinstance(value, float):
        return f"{value:.4g}"
    # Newlines are the reason a `verdict` used to be unprintable on one line.
    text = " ".join(str(value).split())
    # A dedupe fingerprint is 71 characters of hex that nobody reads in full; a
    # prefix is still enough to see two entries carry the same one. `qaas trace
    # --json` keeps the whole thing, which is where an exact match belongs.
    if text.startswith("sha256:"):
        return text[: len("sha256:") + 8] + "…"
    return text


def describe(entry: LedgerEntry, *, width: int = DETAIL_WIDTH) -> str:
    """One line of detail for one entry, truncated to stay in a column."""
    detail = entry.detail
    fields = DETAIL_FIELDS.get(entry.kind) or tuple(detail)
    bits = []
    for key in fields:
        value = detail.get(key)
        if value is None or value == "" or value == []:
            continue  # an absent field is noise; False and 0 are findings
        bits.append(_short(value) if len(fields) == 1 else f"{key}={_short(value)}")
    text = "  ".join(bits)
    return text if len(text) <= width else text[: width - 1] + "…"


@dataclass
class Row:
    """One printable line of the timeline.

    `count` > 1 means consecutive identical-kind entries were folded together.
    """

    at: datetime
    offset_s: float
    agent: str
    kind: str
    detail: str
    cost_usd: float | None = None
    count: int = 1


def _fold_tool_calls(run: list[LedgerEntry]) -> str:
    tools: dict[str, int] = {}
    for e in run:
        tools[str(e.detail.get("tool", "?"))] = tools.get(str(e.detail.get("tool", "?")), 0) + 1
    ranked = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{t}×{n}" for t, n in ranked[:6])
    return shown + (f", +{len(ranked) - 6} more" if len(ranked) > 6 else "")


def timeline(entries: Sequence[LedgerEntry], *, fold_tool_calls: bool = True) -> list[Row]:
    """Rows in run order, with cost accumulating.

    A real run logs ~2400 `tool_call` lines against ~150 of everything else, so
    printing one row each buries the dispatches, denials and verdicts that are
    the point of looking. Consecutive tool calls by the same agent fold into a
    single row that names the tools and how many -- folding only *consecutive*
    runs, so an interleaved denial still lands in the right place and the order
    stays honest. `--json` is exempt: an export must be faithful, not readable.
    """
    if not entries:
        return []
    origin = entries[0].at
    rows: list[Row] = []
    running = 0.0
    i = 0
    while i < len(entries):
        entry = entries[i]
        span = 1
        if fold_tool_calls and entry.kind == LedgerKind.TOOL_CALL:
            while (
                i + span < len(entries)
                and entries[i + span].kind == LedgerKind.TOOL_CALL
                and entries[i + span].agent == entry.agent
            ):
                span += 1
        cost = entry.detail.get("cost_usd") if entry.kind == LedgerKind.AGENT_FINISHED else None
        if isinstance(cost, (int, float)):
            running += float(cost)
        rows.append(
            Row(
                at=entry.at,
                offset_s=(entry.at - origin).total_seconds(),
                agent=entry.agent or "-",
                kind=str(entry.kind),
                detail=describe(entry) if span == 1 else _fold_tool_calls(entries[i : i + span]),
                cost_usd=running if cost is not None else None,
                count=span,
            )
        )
        i += span
    return rows


@dataclass
class RunSummary:
    """The header facts about a run, all of them read back from the ledger.

    Deliberately derived rather than stored: a run that was killed mid-flight
    never wrote `run_finished`, and it is exactly that run someone needs to look
    at. Everything here degrades to None instead of raising.
    """

    run_id: str
    mode: str | None = None
    started: datetime | None = None
    finished: datetime | None = None
    target_sha: str | None = None
    target_dirty: bool | None = None
    budget_usd: float | None = None
    agents: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    escalations: list[str] = field(default_factory=list)
    #: ticket key -> its latest verdict, or None if VERIFIER never reached it.
    tickets: dict[str, str | None] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    stopped_early: str | None = None
    completed: bool = False

    @property
    def duration_s(self) -> float | None:
        if self.started is None or self.finished is None:
            return None
        return (self.finished - self.started).total_seconds()


def summarise(store: RunStore, entries: Sequence[LedgerEntry] | None = None) -> RunSummary:
    """Fold a run's ledger into the header `qaas show` prints."""
    entries = list(entries) if entries is not None else read_ledger(store)
    summary = RunSummary(run_id=store.run_id)
    if entries:
        summary.started = entries[0].at
        summary.finished = entries[-1].at

    for entry in entries:
        summary.counts[str(entry.kind)] = summary.counts.get(str(entry.kind), 0) + 1
        detail = entry.detail
        if entry.kind == LedgerKind.RUN_STARTED:
            # The last session wins, matching how `mode`, `agents` and
            # `target_sha` are already handled just below. `started` was set once
            # from `entries[0]` and never updated, so `qaas show` reported a run
            # resumed the next morning as having taken fourteen hours.
            summary.started = entry.at
            summary.completed = False
            summary.stopped_early = None
            summary.mode = detail.get("mode")
            summary.budget_usd = detail.get("budget_usd")
            summary.agents = list(detail.get("agents") or [])
            summary.target_sha = detail.get("target_sha")
            summary.target_dirty = detail.get("target_dirty")
        elif entry.kind == LedgerKind.RUN_FINISHED:
            summary.completed = True
            summary.stopped_early = detail.get("stopped_early")
        elif entry.kind == LedgerKind.ESCALATION:
            reason = detail.get("reason")
            if reason:
                summary.escalations.append(str(reason))
        elif entry.kind == LedgerKind.TICKET and detail.get("key"):
            summary.tickets.setdefault(str(detail["key"]), None)
        elif entry.kind == LedgerKind.VERDICT and detail.get("ticket_key"):
            # Last verdict wins, matching how the router itself reads these
            # back (`_latest_verdict`); a reopened ticket is verdicted twice.
            summary.tickets[str(detail["ticket_key"])] = detail.get("verdict")
        elif entry.kind == LedgerKind.VERIFIED and detail.get("ticket_key"):
            summary.tickets[str(detail["ticket_key"])] = "VERIFIED"

    # Cost comes from the per-invocation result files, not from summing ledger
    # lines: `put_result` writes one file per invocation precisely so repeated
    # agents (REPRODUCER, FIXER) are not under-counted, and this must agree with
    # `qaas runs`.
    try:
        summary.cost_usd = store.total_cost_usd()
    except Exception:
        # A run killed mid-`put_result` leaves a truncated `results/*.json`, and
        # validating it raised straight out of `qaas show` -- the header of
        # exactly the run someone was trying to find out about. `put_result`
        # logs `agent_finished` with the same cost, so the ledger answers
        # instead; it is the survivor for the same reason on the dashboard.
        summary.cost_usd = sum(
            float(e.detail.get("cost_usd") or 0.0)
            for e in entries
            if e.kind == LedgerKind.AGENT_FINISHED
            and isinstance(e.detail.get("cost_usd"), (int, float))
        )
    return summary
