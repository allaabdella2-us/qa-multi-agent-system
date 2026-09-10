"""M1/M6 verification: the scorer is strict, deterministic, and hard to flatter.

If these tests are wrong, every number the project reports is wrong, so they are
written to be adversarial about the ways a scorer can be too generous.
"""

from pathlib import Path

import pytest
import yaml

from qaas.envelope import DefectEnvelope, Domain, Severity
from qaas.scorecard import GoldenLedger, score, similarity

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
    assert {d.domain for d in ledger.defects} == {"api", "frontend"}


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


def test_wrong_domain_never_matches(ledger):
    e = env(
        domain=Domain.FRONTEND,
        title="Orders list ignores the limit parameter",
        summary="unbounded pagination, limit ignored",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
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


def test_a_security_domain_does_not_match_a_defect_that_is_not_security_relevant(ledger):
    """The gate is widened, not removed."""
    e = env(
        domain=Domain.SECURITY,
        title="Orders list ignores the limit parameter",
        summary="unbounded pagination, limit ignored",
        location={"endpoint": "GET /v1/orders", "paths": ["api/app/routes/orders.py"]},
    )
    assert not score([e], ledger).matches


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
