"""M0 verification: the contract holds, and the gates cannot be talked past."""

import json

import pytest
from pydantic import ValidationError

from qaas.envelope import (
    DefectEnvelope,
    Domain,
    Evidence,
    Reproduction,
    ReproStatus,
    Severity,
)


def make(**overrides) -> DefectEnvelope:
    base = dict(
        run_id="run-1",
        discovered_by="CONDUIT",
        domain=Domain.API,
        **{"class": "bug"},
        title="Orders list endpoint returns unbounded result set",
        summary="GET /v1/orders ignores the limit parameter and returns every row.",
        severity=Severity.MAJOR,
        confidence=0.9,
    )
    base.update(overrides)
    return DefectEnvelope(**base)


# -- round trip -------------------------------------------------------------


def test_round_trips_through_json():
    env = make(
        location={"service": "orders-api", "paths": ["src/routes/orders.py:88"]},
        evidence=[{"type": "test_output", "uri": "artifact://run-1/contract.txt"}],
    )
    again = DefectEnvelope.from_json(env.to_json())
    assert again == env


def test_class_serializes_under_its_reserved_word_alias():
    payload = json.loads(make().to_json())
    assert payload["class"] == "bug"
    assert "defect_class" not in payload


# -- rejection, not repair --------------------------------------------------


@pytest.mark.parametrize(
    "bad,because",
    [
        ({"title": "x" * 91}, "title over 90 chars"),
        ({"title": "two\nlines"}, "title must be one line"),
        ({"confidence": 1.4}, "confidence out of range"),
        ({"severity": "catastrophic"}, "severity off the rubric"),
        ({"domain": "blockchain"}, "unknown domain"),
        ({"discovered_by": "conduit"}, "agent name not SCREAMING_CASE"),
        ({"evidence": [{"type": "log", "uri": "wat/nope"}]}, "evidence uri scheme"),
    ],
)
def test_malformed_envelopes_are_rejected(bad, because):
    with pytest.raises(ValidationError):
        make(**bad)


def test_unknown_fields_are_rejected_not_ignored():
    with pytest.raises(ValidationError):
        make(vibes="pretty bad")


# -- fingerprint stability --------------------------------------------------


def test_same_defect_different_prose_fingerprints_alike():
    a = make(location={"service": "orders-api", "paths": ["src/routes/orders.py:88"]})
    b = make(
        discovered_by="SURFACE",
        run_id="run-2",
        title="Unbounded query on the orders listing",
        summary="Completely different words describing the same defect.",
        confidence=0.7,
        severity=Severity.CRITICAL,
        location={"service": "orders-api", "paths": ["src/routes/orders.py:104"]},
    )
    assert a.fingerprint() == b.fingerprint(), "line moves and prose must not change identity"


def test_different_endpoints_fingerprint_apart():
    a = make(location={"service": "orders-api", "endpoint": "GET /v1/orders"})
    b = make(location={"service": "orders-api", "endpoint": "GET /v1/invoices"})
    assert a.fingerprint() != b.fingerprint()


def test_with_fingerprint_populates_dedupe_without_mutating():
    env = make()
    stamped = env.with_fingerprint()
    assert env.dedupe.fingerprint is None
    assert stamped.dedupe.fingerprint == env.fingerprint()


# -- the gates --------------------------------------------------------------


def test_no_evidence_is_not_fileable():
    ok, reason = make().is_fileable()
    assert not ok and "evidence" in reason


def test_failing_test_counts_as_evidence():
    env = make(reproduction=Reproduction(status=ReproStatus.REPRODUCED, failing_test="t.py::x"))
    assert env.is_fileable()[0]


def test_low_confidence_is_not_fileable():
    env = make(confidence=0.4, evidence=[Evidence(type="log", uri="artifact://a")])
    ok, reason = env.is_fileable()
    assert not ok and "confidence" in reason


def test_not_reproducible_is_not_fileable():
    env = make(
        evidence=[Evidence(type="log", uri="artifact://a")],
        reproduction=Reproduction(status=ReproStatus.NOT_REPRODUCIBLE),
    )
    ok, reason = env.is_fileable()
    assert not ok and "reproducible" in reason


def test_severity_ranks_blocker_above_minor():
    assert Severity.BLOCKER.rank < Severity.MINOR.rank
