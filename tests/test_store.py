"""M0 verification: run state persists, and the ledger is a real audit trail."""

import os
import subprocess
import sys

import pytest

from qaas.envelope import DefectEnvelope, Domain, Severity
from qaas.store import AgentResult, RunStore, SystemMapStore, list_runs


@pytest.fixture
def store(tmp_path):
    return RunStore.new(root=tmp_path)


def make_env(run_id, **kw):
    base = dict(
        run_id=run_id,
        discovered_by="API",
        domain=Domain.API,
        **{"class": "bug"},
        title="Missing role check on the refund endpoint",
        summary="POST /v1/refunds accepts any authenticated user.",
        severity=Severity.CRITICAL,
        confidence=0.85,
    )
    base.update(kw)
    return DefectEnvelope(**base)


def test_envelope_persists_and_is_stamped_with_a_fingerprint(store):
    env = make_env(store.run_id)
    assert env.dedupe.fingerprint is None
    store.put_envelope(env)

    loaded = store.get_envelope(env.id)
    assert loaded is not None
    assert loaded.dedupe.fingerprint == env.fingerprint()
    assert [e.id for e in store.envelopes()] == [env.id]


def test_ledger_is_append_only_and_filterable(store):
    store.log("run_started", mode="pr-check")
    store.put_envelope(make_env(store.run_id))
    store.log("denial", agent="ARCHITECT", tool="Write", reason="read-only agent")

    kinds = [e.kind for e in store.ledger()]
    assert kinds == ["run_started", "envelope", "denial"]

    denials = list(store.ledger("denial"))
    assert len(denials) == 1
    assert denials[0].agent == "ARCHITECT"
    assert denials[0].detail["reason"] == "read-only agent"


def test_artifacts_round_trip_through_their_uri(store):
    uri = store.put_artifact("contract-test.txt", "FAILED tests/contract.py::orders")
    assert uri.startswith(f"artifact://{store.run_id}/")
    assert "FAILED" in store.resolve_artifact(uri).read_text()


def test_artifact_uri_cannot_escape_the_store(store):
    with pytest.raises(ValueError):
        store.resolve_artifact(f"artifact://{store.run_id}/../../../etc/passwd")


def test_artifact_names_with_slashes_are_flattened_not_nested(store):
    uri = store.put_artifact("nested/path/shot.png", b"\x89PNG")
    assert store.resolve_artifact(uri).read_bytes() == b"\x89PNG"


def test_costs_accumulate_across_agents(store):
    store.put_result(AgentResult(agent="API", cost_usd=0.42, num_turns=7))
    store.put_result(AgentResult(agent="BROWSER", cost_usd=1.08, num_turns=12))

    assert store.total_cost_usd() == pytest.approx(1.50)
    assert {r.agent for r in store.results()} == {"API", "BROWSER"}
    assert [e.kind for e in store.ledger("agent_finished")] == ["agent_finished"] * 2


def test_failed_agent_result_records_its_error(store):
    store.put_result(AgentResult(agent="BROWSER", subtype="failure", error="browser timeout"))
    entry = next(store.ledger("agent_finished"))
    assert entry.detail["error"] == "browser timeout"


def test_runs_are_discoverable(tmp_path):
    a = RunStore.new(root=tmp_path)
    b = RunStore.new(root=tmp_path)
    assert set(list_runs(tmp_path)) == {a.run_id, b.run_id}


# -- system map -------------------------------------------------------------


def test_system_map_versions_and_latest_pointer(tmp_path):
    maps = SystemMapStore(tmp_path)
    assert maps.get() is None

    v1 = maps.put({"services": ["orders-api"]})
    v2 = maps.put({"services": ["orders-api", "web"]})

    assert maps.latest_version() == v2
    assert maps.get()["services"] == ["orders-api", "web"]
    assert maps.get(v1)["services"] == ["orders-api"], "pinned version stays readable"
    assert maps.versions() == sorted([v1, v2])


def test_repeated_invocations_of_one_agent_all_count(store):
    """REPRODUCER runs once per finding. A per-agent filename would keep only the last,
    and the run's recorded cost would then be wrong by everything before it."""
    for i in range(3):
        store.put_result(AgentResult(agent="REPRODUCER", cost_usd=1.50, num_turns=5))

    results = store.results()
    assert len(results) == 3, "each invocation is its own record"
    assert store.total_cost_usd() == pytest.approx(4.50)
    assert [e.kind for e in store.ledger("agent_finished")] == ["agent_finished"] * 3


def test_different_agents_are_still_distinguishable(store):
    store.put_result(AgentResult(agent="REPRODUCER", cost_usd=1.0))
    store.put_result(AgentResult(agent="TRIAGE", cost_usd=0.5))
    assert {r.agent for r in store.results()} == {"REPRODUCER", "TRIAGE"}


# -- the ledger must survive the environment it runs in ---------------------


def test_a_denial_reason_round_trips_under_an_ascii_default_encoding(tmp_path):
    """Every guardrail denial contains `§` and `—`, and the writer named no encoding.

    `trace.py` reads the ledger as utf-8; `RunStore.log` wrote it in whatever the
    locale said. Where that resolves to ASCII, the first denial raised
    `UnicodeEncodeError` from inside `Guardrail.pre_tool_use` — the *primary*
    enforcement point — so the turn died instead of the agent being told why it
    was refused.

    It has to be a subprocess: the interpreter fixes its default encoding at
    startup, so setting the environment inside the test proves nothing. And it
    has to be `PYTHONUTF8=0` as well as `LC_ALL=C`, because PEP 540 coerces a
    bare C locale to UTF-8 mode and hides the bug. What is left is an honest
    stand-in for the environments where it does bite: an explicitly disabled
    UTF-8 mode, or a locale that is neither C nor UTF-8.
    """
    script = (
        "from qaas.store import RunStore\n"
        "store = RunStore.new(root=__import__('pathlib').Path(%r))\n"
        "reason = \"api/app/auth.py is outside the envelope (\\u00a78.2) \\u2014 escalate.\"\n"
        "store.log('denial', agent='FIXER', tool='Edit', reason=reason)\n"
        "back = list(store.ledger('denial'))\n"
        "assert back and back[0].detail['reason'] == reason, back\n"
        "print('ok')\n"
    ) % str(tmp_path)

    env = {**os.environ, "PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, env=env
    )

    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


@pytest.mark.parametrize(
    "name",
    ["../../escape.txt", "nested/path/shot.png", r"..\\windows\\escape.txt", "....//escape", "."],
)
def test_an_artifact_name_cannot_reach_outside_its_run(store, name):
    """The name is agent-supplied, and the old guard was a two-entry denylist.

    `name.replace("/", "_").replace("..", "_")` handled the two spellings someone
    thought of and nothing else — a backslash went through untouched. Flatten,
    then check the result, the way `resolve_artifact` already did.
    """
    uri = store.put_artifact(name, b"x")
    path = store.resolve_artifact(uri)
    assert path.parent == (store.dir / "artifacts").resolve()
    assert path.read_bytes() == b"x"


def test_reading_a_run_that_does_not_exist_does_not_create_it(tmp_path):
    """`qaas show <typo>` used to leave a permanent empty run behind.

    Constructing a store mkdir'd unconditionally, so every read-only command was
    also a writer, and the phantom then showed up in `qaas runs` forever.
    """
    RunStore("run-does-not-exist", root=tmp_path, create=False)
    assert not (tmp_path / "runs" / "run-does-not-exist").exists()
    assert list_runs(tmp_path) == []
