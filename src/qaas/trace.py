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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

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
    LedgerKind.RUN_FINISHED: ("agents_run", "cost_usd", "failed", "stopped_early"),
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


def select(
    entries: Sequence[LedgerEntry],
    *,
    agent: str | None = None,
    kinds: Sequence[LedgerKind] | None = None,
) -> list[LedgerEntry]:
    """Filter in memory. Agent match is case-insensitive; agent names are shouted."""
    wanted = set(kinds) if kinds else None
    name = agent.upper() if agent else None
    return [
        e for e in entries
        if (wanted is None or e.kind in wanted)
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
    #: ticket key -> its latest verdict, or None if PROOF never reached it.
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
            # Last verdict wins, matching how the conductor itself reads these
            # back (`_latest_verdict`); a reopened ticket is verdicted twice.
            summary.tickets[str(detail["ticket_key"])] = detail.get("verdict")
        elif entry.kind == LedgerKind.VERIFIED and detail.get("ticket_key"):
            summary.tickets[str(detail["ticket_key"])] = "VERIFIED"

    # Cost comes from the per-invocation result files, not from summing ledger
    # lines: `put_result` writes one file per invocation precisely so repeated
    # agents (FORGE, MENDER) are not under-counted, and this must agree with
    # `qaas runs`.
    summary.cost_usd = store.total_cost_usd()
    return summary
