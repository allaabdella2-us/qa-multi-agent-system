"""M1/M6 verification: the scorer is strict, deterministic, and hard to flatter.

If these tests are wrong, every number the project reports is wrong, so they are
written to be adversarial about the ways a scorer can be too generous.
"""

from pathlib import Path

import pytest
import yaml

from qaas.envelope import DefectEnvelope, Domain, Severity
from qaas.scorecard import (
    MATCH_THRESHOLD,
    GoldenDefect,
    GoldenLedger,
    Match,
    Scorecard,
    score,
    similarity,
)

#: The calibration corpus this repository ships.
LEDGER = Path(__file__).resolve().parents[1] / "target-app" / "defects.yaml"

LEDGER_PATH = Path(__file__).resolve().parents[1] / "target-app" / "defects.yaml"


@pytest.fixture(scope="module")
def ledger() -> GoldenLedger:
    return GoldenLedger.load(LEDGER_PATH)


def env(**kw) -> DefectEnvelope:
    base = dict(
        run_id="r", discovered_by="API", domain=Domain.API, **{"class": "bug"},
        title="t", summary="s", severity=Severity.MAJOR, confidence=0.9,
    )
    base.update(kw)
    return DefectEnvelope(**base)


# -- the ledger itself ------------------------------------------------------


def test_ledger_loads_with_defects_and_planted_cases(ledger):
    # Counts are not asserted: the ledger grows when a run finds something real
    # that was never seeded, and that is a healthy event, not a broken test.
    assert len(ledger.defects) >= 14
    assert len(ledger.not_defects) >= 3


def test_the_corpus_can_measure_more_than_two_agents(ledger):
    """A scorecard can only speak about domains the ledger has defects in.

    It held `api` and `frontend` only, so six of the eight discovery agents had
    nothing seeded in their own surface -- every judgement about DBA, ARCHITECT,
    AUDITOR, SOCKET, LOAD and GUIDE was made on defects outside their
    specialty. "DBA: 0 finds" read as a broken agent and meant an empty corpus.
    """
    covered = {d.domain for d in ledger.defects}
    for domain in ("api", "frontend", "database", "security", "performance", "websocket"):
        assert domain in covered, f"nothing seeded for {domain}; its agent cannot be scored"


def test_discovered_defects_are_marked_as_such(ledger):
    """A found defect is weaker evidence than a planted one. Keep them labelled."""
    for d in ledger.defects:
        if d.discovered_not_seeded:
            assert d.keywords, f"{d.id} needs keywords like any other entry"


def test_every_defect_has_keywords_and_a_location(ledger):
    for d in ledger.defects:
        assert d.keywords, d.id
        assert d.endpoint or d.ui_route or d.paths, d.id


# -- matching ---------------------------------------------------------------


def test_a_good_report_matches_its_defect(ledger):
    e = env(
        title="Orders list ignores the limit parameter",
        summary="GET /v1/orders declares limit but never applies it; unbounded result set.",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py:41"]},
    )
    card = score([e], ledger, domains={"api"})
    assert [m.golden_id for m in card.matches] == ["API-01"]


def test_method_distinguishes_defects_on_the_same_path(ledger):
    """API-02 is GET /v1/orders/{id}; API-07 is DELETE on the same path."""
    e = env(
        title="Deleting a nonexistent order returns 200",
        summary="DELETE does not check the row existed; spec says 404 not found.",
        location={"endpoint": "DELETE /v1/orders/{order_id}", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger, domains={"api"})
    assert [m.golden_id for m in card.matches] == ["API-07"]


def test_path_prefix_and_line_numbers_do_not_defeat_matching(ledger):
    e = env(
        title="Order detail is not scoped to the caller org",
        summary="Any authenticated user can read another organization's order. Cross-tenant IDOR.",
        location={"endpoint": "GET /v1/orders/{id}", "paths": ["target-app/api/app/routes/orders.py:88"]},
    )
    assert [m.golden_id for m in score([e], ledger, domains={"api"}).matches] == ["API-02"]


def test_an_unexpected_domain_weakens_a_match_rather_than_voiding_it(ledger):
    """Right endpoint, right file, right words — different surface label.

    This used to be a veto, and the veto was measurably wrong. The ledger labels
    every seeded defect `api` or `frontend`, so ARCHITECT (`architecture`) and
    DBA (`database`) could not score a hit against this corpus by construction:
    one run charged them twelve false positives, of which nine were real defects
    matching at 0.57 to 1.00 — including a perfect 1.00, and an API-09 the same
    scorecard listed as *missed* while penalising the agent that found it.

    A different-but-defensible label is not a misunderstood defect. It is the
    same defect, found by an agent that owns a different surface.
    """
    e = env(
        domain=Domain.FRONTEND,
        title="Orders list ignores the limit parameter",
        summary="unbounded pagination, limit ignored",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger)
    assert card.matches, "a strong cross-domain match was thrown away"
    assert card.matches[0].golden_id == "API-01"


def test_a_weak_cross_domain_report_is_still_refused(ledger):
    """The gate is softened, not removed.

    Sharing a file with a seeded defect while describing something else must not
    become a match just because the penalty is no longer fatal — `orders.py`
    holds six seeded defects and the file alone identifies none of them.
    """
    e = env(
        domain=Domain.DATABASE,
        title="Connection pool size is not configurable",
        summary="The pool is hardcoded and cannot be tuned per environment.",
        location={"paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger)
    assert not card.matches
    assert card.false_positives == [e.id]


def test_naming_the_right_file_with_nothing_else_is_not_enough(ledger):
    """orders.py holds six seeded defects; the file alone cannot identify one."""
    e = env(
        title="Something seems off in the orders module",
        summary="The code here could be tidier.",
        location={"paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger, domains={"api"})
    assert not card.matches, "a vague finding must not be credited with a real defect"


def test_frontend_defect_matches_on_route_and_keywords(ledger):
    e = env(
        domain=Domain.FRONTEND,
        discovered_by="BROWSER",
        title="Place order button does nothing with a single item",
        summary="On /checkout/review the submit handler never fires for a one item cart.",
        location={"ui_route": "/checkout/review", "paths": ["web/src/routes/CheckoutReview.tsx"]},
    )
    assert [m.golden_id for m in score([e], ledger, domains={"frontend"}).matches] == ["UI-01"]


# -- the ways a scorer gets flattered ---------------------------------------


def test_two_reports_of_one_defect_are_one_find_and_one_duplicate(ledger):
    a = env(
        title="Orders list ignores limit",
        summary="limit parameter is not applied, unbounded result set",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    b = env(
        discovered_by="BROWSER",
        title="Unbounded pagination on order listing",
        summary="The limit query parameter is ignored so every row is returned.",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([a, b], ledger, domains={"api"})
    assert len(card.matches) == 1
    assert len(card.duplicates) == 1
    assert card.recall == pytest.approx(1 / card.total_golden)


def test_reporting_deliberately_correct_behaviour_is_a_false_positive(ledger):
    e = env(
        title="Legacy orders endpoint is broken and returns 410",
        summary="GET /v1/orders/legacy returns 410 Gone to every caller.",
        location={"endpoint": "GET /v1/orders/legacy", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger, domains={"api"})
    assert not card.matches
    assert card.false_positives == [e.id]
    assert card.regressions_on_planted == [(e.id, "NOT-01")]


def test_filing_everything_does_not_produce_a_good_score(ledger):
    """The spam strategy: 40 vague findings. Recall stays 0 and precision collapses."""
    spam = [
        env(title=f"Possible issue {i}", summary="Something might be wrong here.",
            location={"paths": ["api/app/routes/orders.py"]})
        for i in range(40)
    ]
    card = score(spam, ledger, domains={"api"})
    assert card.recall == 0.0
    assert card.precision == 0.0
    assert card.false_positive_rate == 1.0


# -- metrics ----------------------------------------------------------------


def test_recall_precision_and_misses_add_up(ledger):
    good = env(
        title="Refund endpoint has no role check",
        summary="Any authenticated viewer can refund; admin permission is never verified.",
        severity=Severity.BLOCKER,
        location={"endpoint": "POST /v1/orders/{order_id}/refund", "paths": ["api/app/routes/orders.py"]},
    )
    bad = env(title="Unrelated", summary="Nothing real.", location={"paths": ["api/app/db.py"]})
    card = score([good, bad], ledger, domains={"api"}, cost_usd=2.0)

    assert [m.golden_id for m in card.matches] == ["API-04"]
    assert card.precision == pytest.approx(0.5)
    assert card.recall == pytest.approx(1 / card.total_golden)
    assert card.cost_per_accepted == pytest.approx(2.0)
    assert "API-01" in card.missed and "API-04" not in card.missed
    assert card.summary()["found"] == 1


def test_severity_agreement_penalises_a_blocker_called_trivial(ledger):
    understated = env(
        title="Refund endpoint has no role check",
        summary="Any authenticated viewer can refund; admin permission is never verified.",
        severity=Severity.TRIVIAL,
        location={"endpoint": "POST /v1/orders/{order_id}/refund", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([understated], ledger, domains={"api"})
    assert len(card.matches) == 1
    assert card.matches[0].severity_delta == 4
    assert card.severity_agreement == 0.0


def test_an_empty_run_scores_zero_recall_not_perfect_precision(ledger):
    card = score([], ledger, domains={"api"})
    assert card.recall == 0.0
    assert card.precision == 0.0, "finding nothing must never look like finding everything"


def test_similarity_is_symmetric_across_equivalent_param_names(ledger):
    g = ledger.by_id("API-02")
    a = env(location={"endpoint": "GET /v1/orders/{order_id}"}, title="org scope missing",
            summary="cross-tenant read of another organization's order")
    b = env(location={"endpoint": "get /v1/orders/{id}"}, title="org scope missing",
            summary="cross-tenant read of another organization's order")
    assert similarity(a, g) == similarity(b, g) > 0.5


# -- lessons from real runs -------------------------------------------------
#
# Each of these encodes a scorer bug that a live run exposed. In every case the
# agent was right and the scorer was wrong, which is the failure mode to fear:
# a scorer that under-credits good work sends you tuning agents that are fine.


def test_a_security_classification_of_a_security_relevant_defect_counts(ledger):
    """API filed the cross-tenant read under `security`. That is not a miss."""
    e = env(
        domain=Domain.SECURITY,
        title="GET /v1/orders/{order_id} omits the org filter, exposing any org's orders",
        summary="Any authenticated user can read another organization's order by id.",
        severity=Severity.BLOCKER,
        location={"endpoint": "GET /v1/orders/{order_id}", "paths": ["api/app/routes/orders.py:98"]},
    )
    assert [m.golden_id for m in score([e], ledger).matches] == ["API-02"]


def test_a_ux_classification_of_a_frontend_defect_counts(ledger):
    """BROWSER filed the missing label under `ux`. Also not a miss."""
    e = env(
        domain=Domain.UX,
        discovered_by="BROWSER",
        title="WCAG 1.3.1: orders search input has no accessible name",
        summary="A bare input with a placeholder: no label, no aria-label. Screen readers announce nothing.",
        severity=Severity.MINOR,
        location={"ui_route": "/orders", "paths": ["web/src/components/SearchInput.tsx"]},
    )
    assert [m.golden_id for m in score([e], ledger).matches] == ["UI-05"]


def test_a_security_label_on_a_non_security_defect_is_penalised_not_voided(ledger):
    """`security` against a non-`security_relevant` entry gets no free pass...

    ...but it is the same argument as every other domain: an agent that found
    API-01 and filed it under `security` found API-01. The penalty is what keeps
    the label from being free — a vague security claim over the same file still
    fails, as the test above shows.
    """
    e = env(
        domain=Domain.SECURITY,
        title="Orders list ignores the limit parameter",
        summary="unbounded pagination, limit ignored",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger)
    assert card.matches and card.matches[0].golden_id == "API-01"


def test_the_expected_domain_still_scores_higher_than_a_surprising_one(ledger):
    """The penalty has to be visible in the number, or it is not a gate."""
    from qaas.scorecard import similarity

    api_01 = next(d for d in ledger.defects if d.id == "API-01")
    kw = dict(
        title="Orders list ignores the limit parameter",
        summary="unbounded pagination, limit ignored",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    expected = similarity(env(domain=Domain.API, **kw), api_01)
    surprising = similarity(env(domain=Domain.DATABASE, **kw), api_01)
    assert expected > surprising > MATCH_THRESHOLD


def test_two_defects_in_one_file_on_one_route_are_both_credited(ledger):
    """Both UI-02 and UI-04 live at /orders in OrdersList.tsx, so each report
    also resembles the other's entry. Greedy assignment once wrote off whichever
    lost the first pass as a duplicate; both must be matched."""
    rejection = env(
        domain=Domain.FRONTEND, discovered_by="BROWSER",
        title="Failed orders fetch throws an unhandled rejection",
        summary="The fetch has no catch and no error state; a 500 leaves the page blank.",
        location={"ui_route": "/orders", "paths": ["web/src/routes/OrdersList.tsx"]},
    )
    empty = env(
        domain=Domain.UX, discovered_by="BROWSER", severity=Severity.MINOR,
        title="Orders list has no empty state",
        summary="Zero matches renders bare table headers with no message at all.",
        location={"ui_route": "/orders", "paths": ["web/src/routes/OrdersList.tsx"]},
    )
    card = score([rejection, empty], ledger, domains={"frontend", "ux"})
    assert sorted(m.golden_id for m in card.matches) == ["UI-02", "UI-04"]
    assert not card.duplicates


def test_a_genuine_duplicate_is_still_a_duplicate(ledger):
    """The contested-pass fix must not stop real duplicates being counted."""
    a = env(
        title="Orders list ignores limit", summary="limit is not applied, unbounded result set",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    b = env(
        discovered_by="BROWSER", title="Unbounded pagination on order listing",
        summary="The limit query parameter is ignored so every row is returned.",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([a, b], ledger, domains={"api"})
    assert len(card.matches) == 1 and len(card.duplicates) == 1


def test_line_ranges_in_a_path_do_not_defeat_matching(ledger):
    """Agents cite `orders.py:104-112` as readily as `orders.py:104`."""
    e = env(
        title="Deleting a nonexistent order returns 200",
        summary="DELETE never checks the row existed; the spec declares 404 not found.",
        severity=Severity.MINOR,
        location={"endpoint": "DELETE /v1/orders/{order_id}", "paths": ["api/app/routes/orders.py:104-112"]},
    )
    assert [m.golden_id for m in score([e], ledger, domains={"api"}).matches] == ["API-07"]


def test_sharing_a_file_with_a_planted_case_is_not_reporting_it(ledger):
    """orders.py holds six defects and the deliberately-correct legacy handler.
    A path-only rule blamed agents for findings they never made."""
    e = env(
        title="Orders list ignores the limit parameter",
        summary="limit is declared and never applied; unbounded result set.",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    card = score([e], ledger, domains={"api"})
    assert not card.regressions_on_planted


# -- retiring a repaired defect ---------------------------------------------
#
# REVIEWER escalated a correct one-line fix on CORVID-7 because `defects.yaml`
# had no way to record that a seeded defect had been repaired: CLAUDE.md
# requires the ledger to change in the same commit as the defect, and the only
# options were to delete the entry (losing the severity/domain expectations a
# past score depends on) or leave it stale (a permanent phantom miss). These
# pin the third answer.


def _ledger_with(tmp_path, defects, not_defects=()):
    import yaml
    path = tmp_path / "defects.yaml"
    path.write_text(yaml.safe_dump(
        {"version": 1, "app": "t", "defects": list(defects), "not_defects": list(not_defects)}
    ))
    return GoldenLedger.load(path)


def _seed(defect_id="API-01", **over):
    d = {
        "id": defect_id, "domain": "api", "class": "bug", "severity": "major",
        "title": "Orders list accepts a limit parameter and ignores it",
        "detail": "The handler declares limit and never applies it.",
        "location": {"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
        "keywords": ["limit", "unbounded", "pagination", "ignored"],
        "phase": 1,
    }
    d.update(over)
    return d


def _report():
    return env(
        title="GET /v1/orders ignores the limit query parameter",
        summary="The limit parameter is validated and echoed but never applied; unbounded page.",
        location={"endpoint": "GET /v1/orders", "paths": ["target-app/api/app/routes/orders.py"]},
    )


def test_an_unfixed_defect_is_expected_and_counts_for_recall(tmp_path):
    led = _ledger_with(tmp_path, [_seed()])
    assert led.defects[0].retired is False
    card = score([], led)
    assert card.total_golden == 1
    assert card.missed == ["API-01"], "still expected, so not finding it is a miss"


def test_a_fixed_defect_leaves_the_recall_denominator(tmp_path):
    """The phantom miss this field exists to prevent: without it, a repaired
    defect is counted against recall on every run for the rest of the project."""
    led = _ledger_with(tmp_path, [_seed(fixed_in="fix/CORVID-7-orders-limit")])
    assert led.defects[0].retired is True
    card = score([], led)
    assert card.total_golden == 0
    assert card.missed == []
    assert card.recall == 0.0  # nothing expected, nothing found


def test_reporting_a_fixed_defect_is_neither_a_find_nor_a_false_positive(tmp_path):
    """The fix lives on a branch. An agent scanning a tree without it is right
    to report the defect, so precision must not be punished -- and recall must
    not be flattered either."""
    led = _ledger_with(tmp_path, [_seed(fixed_in="fix/CORVID-7-orders-limit")])
    card = score([_report()], led)
    assert card.matches == []
    assert card.false_positives == [], "a correct observation is not a false positive"
    assert [g for _, g in card.retired_hits] == ["API-01"]
    assert card.summary()["retired_hits"] == 1


def test_retiring_one_defect_does_not_disturb_the_others(tmp_path):
    led = _ledger_with(tmp_path, [
        _seed("API-01", fixed_in="fix/x"),
        _seed("API-02", location={"endpoint": "GET /v1/orders/{id}",
                                  "paths": ["api/app/routes/orders.py"]},
              title="Order detail leaks across organizations",
              detail="No org scoping on the detail route.",
              keywords=["tenant", "cross-tenant", "org", "leak", "authorization"]),
    ])
    card = score([_report()], led)
    assert card.total_golden == 1, "only the unfixed defect is expected"
    assert card.missed == ["API-02"]
    assert [g for _, g in card.retired_hits] == ["API-01"]
    assert card.false_positives == []


# -- the ledger is checked when it is read, not when it is too late ---------


@pytest.mark.parametrize(
    "field, bad, good",
    [("domain", "ui", "frontend"), ("severity", "high", "major")],
)
def test_a_ledger_value_that_is_not_an_enum_member_fails_at_load(tmp_path, field, bad, good):
    """Both failures used to be silent, late, or both.

    A bad `domain` scored 0.0 against every envelope forever — permanently
    missed, recall quietly lower, no error. A bad `severity` raised out of
    `severity_delta` after a paid run had already finished.
    """
    entry = {
        "id": "X-1", "domain": "api", "severity": "major",
        "title": "t", "detail": "d", "phase": 1,
    }
    entry[field] = bad
    path = tmp_path / "defects.yaml"
    path.write_text(yaml.safe_dump({"defects": [entry], "not_defects": []}))

    with pytest.raises(ValueError) as exc:
        GoldenLedger.load(path)
    assert "X-1" in str(exc.value) and bad in str(exc.value)
    assert good in str(exc.value), "the message must name what is allowed"


def test_the_shipped_ledger_still_loads():
    """The obligation this guards is a human's; this is what makes it visible."""
    ledger = GoldenLedger.load(LEDGER_PATH)
    assert ledger.defects and ledger.not_defects


# -- what the calibration numbers actually measure ---------------------------


def _golden(**kw):
    base = dict(
        id="API-01", domain="api", defect_class="bug", severity="major",
        title="Cross-tenant order read", detail="An org can read another org's orders.",
        endpoint="GET /v1/orders", paths=("api/app/routes/orders.py",),
        keywords=("tenant", "leak", "org"),
    )
    base.update(kw)
    return GoldenDefect(**base)


def _finding(title, summary, **kw):
    payload = dict(
        run_id="r", discovered_by="API", domain="api", **{"class": "bug"},
        title=title, summary=summary, severity="major", confidence=0.9,
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
        evidence=[{"type": "log", "uri": "artifact://a/b"}],
    )
    payload.update(kw)
    return DefectEnvelope.model_validate(payload)


def test_naming_the_right_place_is_not_finding_the_right_defect():
    """An anchor is *where*, never *what*.

    With `weights = (0.45, 0.30, 0.35)` the right endpoint plus the right file
    scored 0.75 against a 0.5 threshold on keyword overlap of exactly zero -- so
    "this endpoint is slow" was credited with having found the cross-tenant leak
    that lives in the same handler. Recall counted it, the real defect landed in
    `missed`, and the number the whole project is calibrated on moved the wrong
    way.
    """
    golden = _golden()
    wrong = _finding("Response time is slow", "This endpoint takes two seconds.")
    right = _finding("Cross-tenant order read", "An org can read another org's orders.")
    assert similarity(wrong, golden) < MATCH_THRESHOLD
    assert similarity(right, golden) >= MATCH_THRESHOLD


def test_an_entry_with_no_keywords_still_matches_on_its_anchor():
    """The rule applies only where there is something to overlap with."""
    golden = _golden(keywords=())
    assert similarity(_finding("Anything", "at all"), golden) >= MATCH_THRESHOLD


def test_one_shared_word_is_a_coincidence_and_not_a_match():
    """The gate was `keywords == 0.0`, and two false credits walked through it.

    The endpoint (0.45) and the file (0.30) already reach 0.75 on their own, so
    a single incidental word was the whole difference. On the calibration run
    this credited DB-01 -- the missing UNIQUE on `order_items` -- to a report
    arguing the *opposite*, that the constraints exist in PostgreSQL and only
    the ORM lacks them; the shared word was "total_cents". Separately UI-06, a
    contrast failure in `Button.tsx`, was credited to a missing-role finding in
    `OrdersList` on the strength of "wcag".
    """
    golden = _golden(keywords=("tenant", "leak", "org", "authorization"))
    coincidence = _finding(
        "Response time is slow",
        "The org filter makes this endpoint take two seconds.",
    )
    assert "org" in coincidence.summary  # the overlap is real, and it is one word
    assert similarity(coincidence, golden) < MATCH_THRESHOLD


def test_two_shared_words_are_a_topic():
    """The floor must not swallow real reports; the corpus's weakest sat at 2."""
    golden = _golden(keywords=("tenant", "leak", "org", "authorization"))
    genuine = _finding(
        "Orders leak across organisations",
        "One org reads another org's orders; the tenant check is missing.",
    )
    assert similarity(genuine, golden) >= MATCH_THRESHOLD


def test_a_single_keyword_entry_stays_matchable():
    """The floor is capped at the entry's own keyword count.

    Otherwise an entry declaring one keyword becomes unmatchable by arithmetic
    rather than by judgement -- it could never supply the second hit.
    """
    golden = _golden(keywords=("tenant",))
    assert similarity(_finding("Tenant leak", "Orders cross the tenant boundary."),
                      golden) >= MATCH_THRESHOLD


def test_a_websocket_finding_can_match_a_websocket_defect_recorded_under_api():
    """API-09 is `WEBSOCKET /v1/orders/stream` filed under domain `api`.

    SOCKET owns the realtime surface and reports `websocket`, so with an
    exact-equality domain gate that agent could only ever miss the one entry in
    its own domain and be charged a false positive for finding it.
    """
    golden = _golden(id="API-09", endpoint="WEBSOCKET /v1/orders/stream",
                     keywords=("websocket", "stream", "contract"))
    reported = _finding(
        "Orders stream is absent from the contract",
        "The websocket stream endpoint appears in no published contract.",
        domain="websocket",
        location={"endpoint": "WEBSOCKET /v1/orders/stream",
                  "paths": ["api/app/routes/stream.py"]},
    )
    assert similarity(reported, golden) >= MATCH_THRESHOLD


def test_duplicates_count_against_precision():
    """A run that finds three defects and files forty restatements of them used
    to score 100%. That is the ticket spam `qaas sweep --min-precision` exists
    to catch, so the one number CI reads could not see its own failure mode."""
    card = Scorecard(total_golden=1, total_envelopes=4)
    card.matches.append(
        Match(golden_id="API-01", envelope_id="a", score=0.9,
              reported_severity="major", expected_severity="major")
    )
    card.duplicates.extend(["b", "c", "d"])
    assert card.precision == pytest.approx(0.25)


def test_scoring_one_domain_does_not_charge_the_others_as_noise():
    """`--domain` narrowed only the golden side.

    Every finding outside the chosen domains stayed in the envelope list with
    nothing left that could match it, so scoring a nightly run with
    `--domain api` reported every frontend and database finding the run
    correctly made as a false positive.
    """
    ledger = GoldenLedger(defects=[_golden()], not_defects=[])
    envelopes = [
        _finding("Cross-tenant order read", "An org can read another org's orders."),
        _finding("Button has no label", "The submit control is unlabelled.",
                 domain="frontend", location={"ui_route": "/orders", "paths": ["web/src/x.tsx"]}),
    ]
    card = score(envelopes, ledger, domains={"api"})
    assert card.false_positives == [], "a frontend finding was charged to an api-only scoring"
    assert card.precision == pytest.approx(1.0)


def test_ui_08_is_a_defect_and_reporting_it_is_not_a_false_positive():
    """It sat under `not_defects` with no `why_correct` -- the one field that
    section's entries exist to carry -- while its own detail called it "a latent
    defect". So a correct report of the missing pager was scored as noise."""
    ledger = GoldenLedger.load(LEDGER)
    assert "UI-08" in {d.id for d in ledger.defects}
    assert "UI-08" not in {n.id for n in ledger.not_defects}
    assert all(n.why_correct for n in ledger.not_defects), (
        "every not_defects entry must say why the code is correct"
    )
