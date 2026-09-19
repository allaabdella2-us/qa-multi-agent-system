"""Scoring a run against the golden ledger.

This is the module that keeps the project honest. Everything else in the system
can look like it works — agents run, envelopes appear, tickets get filed — while
the findings are noise. The scorecard is what says otherwise, in numbers.

Matching is deliberately deterministic. Using a model to judge whether a finding
matches a seeded defect would make the score depend on the same class of system
being measured, and a generous judge would flatter the result exactly when the
result least deserves it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from qaas.envelope import DefectEnvelope, Domain, normalize_path, Severity

MATCH_THRESHOLD = 0.5


@dataclass(frozen=True)
class GoldenDefect:
    """One seeded defect, as recorded in target-app/defects.yaml."""

    id: str
    domain: str
    defect_class: str
    severity: str
    title: str
    detail: str
    endpoint: str | None = None
    ui_route: str | None = None
    paths: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    phase: int = 1
    security_relevant: bool = False
    # True for defects the system found rather than ones placed for it to find.
    # They still count, but they are weaker evidence: the ledger is a floor on
    # what exists in the app, never a complete oracle.
    discovered_not_seeded: bool = False
    #: The ref a fix for this defect landed on, once one has. Retires the entry
    #: from the recall denominator without deleting it -- REVIEWER escalated a
    #: correct fix because the schema had no way to say this, and CLAUDE.md
    #: requires the ledger to change in the same commit as the defect. Deleting
    #: the entry instead would lose the severity and domain expectations that
    #: make a past score reproducible.
    fixed_in: str | None = None

    @property
    def retired(self) -> bool:
        """Fixed, so no longer expected to be present. Not scored for recall."""
        return self.fixed_in is not None


@dataclass(frozen=True)
class PlantedNonDefect:
    """Correct behaviour that looks wrong. Reporting one is a false positive."""

    id: str
    title: str
    why_correct: str
    endpoint: str | None = None
    ui_route: str | None = None
    paths: tuple[str, ...] = ()


@dataclass
class GoldenLedger:
    defects: list[GoldenDefect]
    not_defects: list[PlantedNonDefect]

    @classmethod
    def load(cls, path: Path | str) -> "GoldenLedger":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            defects=[_golden(d) for d in raw.get("defects", [])],
            not_defects=[_planted(d) for d in raw.get("not_defects", [])],
        )

    def for_phase(self, phase: int) -> list[GoldenDefect]:
        return [d for d in self.defects if d.phase <= phase]

    def by_id(self, defect_id: str) -> GoldenDefect | None:
        return next((d for d in self.defects if d.id == defect_id), None)


def _enum_or_die(kind: type[Domain] | type[Severity], value: Any, defect_id: str, field: str) -> str:
    """A ledger value that must name an enum member, checked when it is read.

    `domain` and `severity` arrived as free strings and were compared against
    validated enums downstream, so the two failure modes were both silent and
    both late. A `domain: ui` (not a `Domain`) scores 0.0 similarity against
    every envelope forever: the defect is permanently `missed`, recall drops and
    nothing says why. A `severity: high` (not a `Severity`) raises ValueError out
    of `Match.severity_delta` — *after* a paid run has finished.

    "A stale ledger silently corrupts every score" is the reason the ledger is a
    human's responsibility at merge. This makes the failure loud and immediate
    instead, which is the only part of that a program can help with.
    """
    try:
        return kind(value).value
    except ValueError:
        allowed = ", ".join(sorted(m.value for m in kind))
        raise ValueError(
            f"{defect_id}: {field} '{value}' is not a {kind.__name__}. Use one of: {allowed}."
        ) from None


def _golden(d: dict[str, Any]) -> GoldenDefect:
    loc = d.get("location", {}) or {}
    return GoldenDefect(
        id=d["id"],
        domain=_enum_or_die(Domain, d["domain"], d["id"], "domain"),
        defect_class=d.get("class", "bug"),
        severity=_enum_or_die(Severity, d["severity"], d["id"], "severity"),
        title=d["title"],
        detail=d.get("detail", ""),
        endpoint=loc.get("endpoint"),
        ui_route=loc.get("ui_route"),
        paths=tuple(loc.get("paths", ())),
        keywords=tuple(d.get("keywords", ())),
        phase=int(d.get("phase", 1)),
        security_relevant=bool(d.get("security_relevant", False)),
        discovered_not_seeded=bool(d.get("discovered_not_seeded", False)),
        fixed_in=(str(d["fixed_in"]) if d.get("fixed_in") else None),
    )


def _planted(d: dict[str, Any]) -> PlantedNonDefect:
    loc = d.get("location", {}) or {}
    return PlantedNonDefect(
        id=d["id"],
        title=d["title"],
        why_correct=d.get("why_correct", ""),
        endpoint=loc.get("endpoint"),
        ui_route=loc.get("ui_route"),
        paths=tuple(loc.get("paths", ())),
    )


# -- similarity -------------------------------------------------------------


def _norm_endpoint(endpoint: str | None) -> str | None:
    """`GET /v1/orders/{order_id}` and `get /v1/orders/{id}` are the same endpoint."""
    if not endpoint:
        return None
    e = re.sub(r"\{[^}]*\}", "{}", endpoint.strip().lower())
    return re.sub(r"\s+", " ", e)


def _norm_path(path: str) -> str:
    """Compare by tail, so `target-app/api/app/routes/orders.py:88` matches `api/app/routes/orders.py`.

    Delegates to the envelope's own normalisation rather than restating it. The
    two were separate implementations and drifted: the scorer handled `:104-112`
    line ranges and the repo-root prefix, the fingerprint handled neither, so a
    defect the scorer counted as one thing hashed as three.
    """
    return normalize_path(path)


def _path_overlap(a: Iterable[str], b: Iterable[str]) -> float:
    """Fraction of the golden defect's files the report also names."""
    golden = {_norm_path(p) for p in b}
    if not golden:
        return 0.0
    reported = {_norm_path(p) for p in a}
    hits = sum(
        1
        for g in golden
        if any(r == g or r.endswith("/" + g) or g.endswith("/" + r) for r in reported)
    )
    return hits / len(golden)


def _keyword_hits(text: str, keywords: Iterable[str]) -> int:
    """How many of an entry's keywords the report actually uses."""
    blob = text.lower()
    return sum(1 for t in keywords if t.lower() in blob)


def _keyword_overlap(text: str, keywords: Iterable[str]) -> float:
    terms = list(keywords)
    if not terms:
        return 0.0
    return _keyword_hits(text, terms) / len(terms)


# Domains that describe the same surface. An agent choosing either one has
# classified the defect defensibly, so scoring must accept both.
#
# Both entries were learned from real runs, and both times the scorer was wrong
# rather than the agent: API filed a cross-tenant read under `security`, and
# BROWSER filed a missing label and a contrast failure under `ux`. Marking those
# as misses would have hidden a perfect discovery run behind a 50% score.
#
# `websocket`/`api` is the same shape, found the same way. API-09 is a defect on
# `WEBSOCKET /v1/orders/stream` recorded under domain `api` -- a defensible
# reading, since the endpoint is served by the API and the defect is that it is
# missing from the API's published contract. But SOCKET owns the realtime
# surface and files under `websocket`, so with an exact-equality gate that agent
# could only ever *miss* the one entry in its own domain and be charged a false
# positive for finding it.
_EQUIVALENT_DOMAINS: dict[str, set[str]] = {
    "ux": {"frontend"},
    "frontend": {"ux"},
    "websocket": {"api"},
    "api": {"websocket"},
}


def _domains_compatible(reported: str, golden: GoldenDefect) -> bool:
    """Whether a reported domain is an acceptable classification of this defect.

    Domain stays a gate — naming the right file under a genuinely wrong domain
    means the defect was misunderstood — but the gate accepts any defensible
    reading, not only the one the ledger happened to write down.
    """
    if reported == golden.domain:
        return True
    if reported == "security" and golden.security_relevant:
        return True
    return golden.domain in _EQUIVALENT_DOMAINS.get(reported, set())


#: A domain the ledger did not expect weakens a match; it does not void one.
#:
#: `defect_memory.CROSS_DOMAIN_PENALTY` has always worked this way, and the
#: scorer did not. It vetoed to 0.0 instead -- and the ledger labels every
#: seeded defect `api` or `frontend`, so ARCHITECT (`architecture`) and DBA
#: (`database`) could not score a hit against this corpus by construction. One
#: run charged them twelve false positives; nine were real defects matching at
#: 0.57 to 1.00, including a perfect 1.00 on API-04 and an API-09 that the same
#: scorecard listed as *missed* while an agent was being penalised for finding
#: it. The measured verdict was "these two agents are broken", which would have
#: sent someone to rewrite two working prompts.
#:
#: 0.75 rather than a half: the gate still has to mean something. At this value
#: a strong location-and-keyword agreement survives a different-but-defensible
#: label, while a vague report that merely shares a file does not.
CROSS_DOMAIN_PENALTY = 0.75

# How many of a golden entry's keywords a report must use before it can be
# credited with having found that entry. See the gate in `similarity`.
MIN_KEYWORD_HITS = 2


def similarity(env: DefectEnvelope, golden: GoldenDefect) -> float:
    """0..1 confidence that `env` reports `golden`."""
    domain_factor = 1.0 if _domains_compatible(env.domain.value, golden) else CROSS_DOMAIN_PENALTY

    text = f"{env.title} {env.summary} {env.suggested_fix_area}"

    endpoint_match = (
        _norm_endpoint(env.location.endpoint) is not None
        and _norm_endpoint(env.location.endpoint) == _norm_endpoint(golden.endpoint)
    )
    route_match = (
        env.location.ui_route is not None
        and golden.ui_route is not None
        and env.location.ui_route.rstrip("/") == golden.ui_route.rstrip("/")
    )

    # A cross-cutting defect has no single endpoint or route to anchor on, and
    # the files that best demonstrate it are a judgement call — a report of
    # inconsistent error shapes may cite whichever two handlers differ. For
    # those, the prose has to carry the identification.
    anchored = bool(golden.endpoint or golden.ui_route)
    weights = (0.45, 0.30, 0.35) if anchored else (0.0, 0.35, 0.65)
    w_location, w_paths, w_keywords = weights

    keywords = _keyword_overlap(text, golden.keywords)
    score = w_location if (endpoint_match or route_match) else 0.0
    score += w_paths * _path_overlap(env.location.paths, golden.paths)
    score += w_keywords * keywords
    score = min(score, 1.0)

    # An anchor is *where*, never *what*. With `weights = (0.45, 0.30, 0.35)`,
    # naming the right endpoint and the right file scored 0.75 against a
    # threshold of 0.5 with keyword overlap of exactly zero -- so a finding that
    # said "this endpoint is slow" was credited with having found the
    # cross-tenant leak that lives in the same handler. Recall counted it, the
    # real defect went in `missed`, and the number the whole project is
    # calibrated on moved in the wrong direction.
    #
    # `golden.keywords` is what an entry uses to say what the defect *is*, so
    # requiring some overlap is requiring the report to be about the same thing.
    # Applied only where there is something to overlap with: an entry that
    # declares no keywords has nothing to say here and the anchor is all there
    # is. An unanchored defect already weights keywords at 0.65 and needs no
    # help.
    # One word is a coincidence, not a topic. The gate below started as
    # `keywords == 0.0` and two false credits walked straight through it, both
    # on a single incidental hit:
    #
    #   DB-01 wants the missing UNIQUE on order_items. A report titled "Order
    #   constraints exist only in PostgreSQL, so violations escape as HTTP 500"
    #   -- the *opposite* claim about the same table -- shared the endpoint
    #   (0.45) and the files (0.30) and needed nothing more than the word
    #   "total_cents" to reach 0.81.
    #
    #   UI-06 wants a contrast failure in Button.tsx. A report about missing
    #   role/name on order rows in OrdersList matched on "wcag" alone.
    #
    # Across the calibration run every one of the fourteen true matches used at
    # least two keywords and the lowest sat at 2-of-7, so the floor separates
    # them cleanly rather than being tuned to the boundary. Capped at the
    # entry's own keyword count: an entry declaring a single keyword would
    # otherwise become unmatchable by arithmetic rather than by judgement.
    if anchored and golden.keywords:
        needed = min(MIN_KEYWORD_HITS, len(golden.keywords))
        if _keyword_hits(text, golden.keywords) < needed:
            return min(score, MATCH_THRESHOLD - 0.01)
    return score * domain_factor


def resembles_planted(env: DefectEnvelope, planted: PlantedNonDefect) -> float:
    """Whether a finding is a report of deliberately-correct behaviour.

    This needs a real anchor — the same endpoint or the same route. Sharing a
    file is not enough: `orders.py` holds six seeded defects as well as the
    deliberately-correct legacy handler, so a path-only rule blamed agents for
    reporting the legacy endpoint when they had reported something else entirely.
    """
    if planted.endpoint and _norm_endpoint(env.location.endpoint) == _norm_endpoint(planted.endpoint):
        return 1.0
    if planted.ui_route and env.location.ui_route == planted.ui_route:
        return 1.0
    return 0.0


# -- results ----------------------------------------------------------------


@dataclass
class Match:
    golden_id: str
    envelope_id: str
    score: float
    reported_severity: str
    expected_severity: str

    @property
    def severity_delta(self) -> int:
        """Ranks apart. 0 is agreement, positive means the report was too calm."""
        return Severity(self.reported_severity).rank - Severity(self.expected_severity).rank


@dataclass
class Scorecard:
    matches: list[Match] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    regressions_on_planted: list[tuple[str, str]] = field(default_factory=list)
    #: Findings that matched a defect already marked `fixed_in`. Neither a find
    #: nor a false positive: the report is correct wherever the fix has not
    #: landed, so scoring it either way would be a lie about the run.
    retired_hits: list[tuple[str, str]] = field(default_factory=list)
    total_golden: int = 0
    total_envelopes: int = 0
    cost_usd: float = 0.0

    @property
    def recall(self) -> float:
        return len(self.matches) / self.total_golden if self.total_golden else 0.0

    @property
    def precision(self) -> float:
        """Accepted findings over findings judged — duplicates included.

        Duplicates were kept out of the denominator as well as the numerator, so
        a run that found three real defects and filed forty restatements of them
        scored 100% precision. That is the exact ticket-spam this system is built
        to avoid, and `qaas sweep --min-precision` is the gate meant to catch it,
        so the one number CI reads could not see the failure mode it exists for.

        A duplicate is not a false positive -- it is true -- but it is a finding
        that should not have been emitted, and precision is the measure of what
        an operator would have had to read.
        """
        judged = len(self.matches) + len(self.false_positives) + len(self.duplicates)
        return len(self.matches) / judged if judged else 0.0

    @property
    def false_positive_rate(self) -> float:
        return len(self.false_positives) / self.total_envelopes if self.total_envelopes else 0.0

    @property
    def duplicate_rate(self) -> float:
        return len(self.duplicates) / self.total_envelopes if self.total_envelopes else 0.0

    @property
    def severity_agreement(self) -> float:
        """Share of matches scored within one rank of the expected severity."""
        if not self.matches:
            return 0.0
        return sum(1 for m in self.matches if abs(m.severity_delta) <= 1) / len(self.matches)

    @property
    def cost_per_accepted(self) -> float | None:
        return self.cost_usd / len(self.matches) if self.matches else None

    def by_agent(self, envelopes: list[DefectEnvelope]) -> dict[str, dict[str, Any]]:
        """The same numbers, split by which agent reported each finding.

        This was always one join away and never made: `Match.envelope_id` plus
        `DefectEnvelope.discovered_by` is the whole of it. So the single most
        actionable calibration fact the system can produce — that LOAD is at 40%
        precision on performance while AUDITOR is at 95% on security — was
        invisible, and CLAUDE.md's instruction to "run `qaas score` after
        changing any prompt, threshold or model" could only ever be answered with
        a run-wide average that moves too little to read.

        A pure function over data it is handed, like the rest of this module.
        `envelopes` comes in rather than being looked up, so the scorecard still
        depends on nothing but the ledger and the findings.
        """
        who = {e.id: e.discovered_by for e in envelopes}
        rows: dict[str, dict[str, Any]] = {}

        def row(agent: str) -> dict[str, Any]:
            return rows.setdefault(
                agent,
                {"matched": 0, "false_positive": 0, "duplicate": 0,
                 "planted_misreported": 0, "severity_deltas": []},
            )

        for match in self.matches:
            entry = row(who.get(match.envelope_id, "unknown"))
            entry["matched"] += 1
            entry["severity_deltas"].append(match.severity_delta)
        for envelope_id in self.false_positives:
            row(who.get(envelope_id, "unknown"))["false_positive"] += 1
        for envelope_id in self.duplicates:
            row(who.get(envelope_id, "unknown"))["duplicate"] += 1
        for envelope_id, _planted in self.regressions_on_planted:
            row(who.get(envelope_id, "unknown"))["planted_misreported"] += 1

        for entry in rows.values():
            # The same denominator `Scorecard.precision` uses, duplicates
            # included: an agent that restates its own finding forty times has
            # not earned 100%.
            judged = entry["matched"] + entry["false_positive"] + entry["duplicate"]
            entry["precision"] = round(entry["matched"] / judged, 3) if judged else None
            deltas = entry.pop("severity_deltas")
            entry["severity_agreement"] = (
                round(sum(1 for d in deltas if abs(d) <= 1) / len(deltas), 3) if deltas else None
            )
        return dict(sorted(rows.items()))

    def summary(self) -> dict[str, Any]:
        return {
            "found": len(self.matches),
            "of": self.total_golden,
            "recall": round(self.recall, 3),
            "precision": round(self.precision, 3),
            "false_positives": len(self.false_positives),
            "false_positive_rate": round(self.false_positive_rate, 3),
            "duplicates": len(self.duplicates),
            "duplicate_rate": round(self.duplicate_rate, 3),
            "severity_agreement": round(self.severity_agreement, 3),
            "planted_misreported": len(self.regressions_on_planted),
            "retired_hits": len(self.retired_hits),
            "cost_usd": round(self.cost_usd, 4),
            "cost_per_accepted": (
                round(self.cost_per_accepted, 4) if self.cost_per_accepted is not None else None
            ),
            "missed": sorted(self.missed),
        }


def score(
    envelopes: list[DefectEnvelope],
    ledger: GoldenLedger,
    *,
    phase: int = 1,
    domains: set[str] | None = None,
    cost_usd: float = 0.0,
    threshold: float = MATCH_THRESHOLD,
) -> Scorecard:
    """Match findings to seeded defects, best pair first.

    Assignment is one-to-one and greedy on the strongest pair remaining. A second
    envelope for an already-matched defect is a duplicate, not a second find —
    counting it as a find would reward exactly the ticket-spam this system is
    built to avoid.
    """
    in_scope = [d for d in ledger.for_phase(phase) if domains is None or d.domain in domains]
    # `--domain` narrowed only the golden side, so every finding outside the
    # chosen domains stayed in `total_envelopes` and fell through to the
    # false-positive loop with nothing left that could match it. Scoring a
    # nightly run with `--domain api` therefore reported every frontend,
    # database and security finding the run correctly made as noise, and
    # precision collapsed for a reason that had nothing to do with the agents.
    #
    # Filtered by *compatibility* rather than equality, using the same rule the
    # matcher uses: a finding filed under `security` or `ux` is a defensible
    # reading of an `api` or `frontend` entry and must survive the same scoping
    # that lets it match.
    if domains is not None:
        wanted = {
            d for d in domains
        } | {equivalent for d in domains for equivalent in _EQUIVALENT_DOMAINS.get(d, set())}
        envelopes = [
            e for e in envelopes
            if e.domain.value in wanted
            or (e.domain.value == "security" and any(g.security_relevant for g in in_scope))
        ]
    # A defect marked `fixed_in` is still matched -- so a report of it is not
    # written off as a false positive -- but it leaves the recall denominator.
    # Counting a repaired defect as a miss on every future run is exactly the
    # silent corruption CLAUDE.md warns about.
    golden = [d for d in in_scope if not d.retired]
    retired = {d.id for d in in_scope if d.retired}
    matchable = in_scope
    card = Scorecard(total_golden=len(golden), total_envelopes=len(envelopes), cost_usd=cost_usd)

    pairs = sorted(
        (
            (similarity(env, g), env, g)
            for env in envelopes
            for g in matchable
            if similarity(env, g) >= threshold
        ),
        key=lambda t: -t[0],
    )

    claimed_golden: set[str] = set()
    claimed_env: set[str] = set()
    contested: list[tuple[float, DefectEnvelope, GoldenDefect]] = []

    # First pass: settle the unambiguous pairs, strongest first.
    for sim, env, g in pairs:
        if g.id in claimed_golden or env.id in claimed_env:
            contested.append((sim, env, g))
            continue
        claimed_golden.add(g.id)
        claimed_env.add(env.id)
        if g.id in retired:
            card.retired_hits.append((env.id, g.id))
        else:
            card.matches.append(
                Match(
                    golden_id=g.id,
                    envelope_id=env.id,
                    score=round(sim, 3),
                    reported_severity=env.severity.value,
                    expected_severity=g.severity,
                )
            )

    # Second pass: an envelope whose best match was taken gets its next-best
    # before anything else. Branding it a duplicate here was a real bug — two
    # defects in one file on one route (a missing empty state and an unhandled
    # rejection, both on /orders in OrdersList.tsx) each match the other's
    # golden entry, so whichever lost the first pass was written off entirely.
    # Only an envelope with no unclaimed match left is genuinely a duplicate.
    for sim, env, g in contested:
        if env.id in claimed_env:
            continue
        if g.id not in claimed_golden:
            claimed_golden.add(g.id)
            claimed_env.add(env.id)
            if g.id in retired:
                card.retired_hits.append((env.id, g.id))
            else:
                card.matches.append(
                    Match(
                        golden_id=g.id,
                        envelope_id=env.id,
                        score=round(sim, 3),
                        reported_severity=env.severity.value,
                        expected_severity=g.severity,
                    )
                )

    for sim, env, g in contested:
        if env.id not in claimed_env:
            card.duplicates.append(env.id)
            claimed_env.add(env.id)

    card.missed = [g.id for g in golden if g.id not in claimed_golden]

    for env in envelopes:
        if env.id in claimed_env:
            continue
        card.false_positives.append(env.id)
        for planted in ledger.not_defects:
            if resembles_planted(env, planted) >= MATCH_THRESHOLD:
                card.regressions_on_planted.append((env.id, planted.id))
                break

    card.matches.sort(key=lambda m: m.golden_id)
    return card
