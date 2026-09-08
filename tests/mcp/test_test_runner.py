"""M2 verification: the test_runner server returns structure, not scraped text.

The suite under test is a throwaway package built in tmp_path — three tests with
known behaviour, one of them deliberately flaky — so the assertions are about
the parser and the flake maths, not about this repo's own suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from qaas.config import load_config
from qaas.mcp.context import ToolContext, handlers
from qaas.mcp.test_runner import MAX_FLAKE_RUNS, build_tools
from qaas.store import RunStore, SystemMapStore

REPO = Path(__file__).resolve().parents[2]

STABLE = '''
def test_ok():
    assert 1 + 1 == 2


def test_bad():
    expected = 2
    assert 1 + 1 == expected + 1, "arithmetic drifted"


def test_skipped():
    import pytest
    pytest.skip("not applicable here")
'''

# Fails on every odd invocation. A counter on disk survives the process boundary,
# which is what makes it flaky across runs rather than within one.
FLAKY = '''
from pathlib import Path

COUNTER = Path(__file__).with_name("counter.txt")


def test_flaky():
    n = int(COUNTER.read_text()) if COUNTER.exists() else 0
    COUNTER.write_text(str(n + 1))
    assert n % 2 == 0, f"flaked on run {n}"
'''

SOURCE = '''
def calculate(a, b):
    return a + b
'''

COVERING = '''
from pkg.calc import calculate


def test_calculate():
    assert calculate(1, 2) == 3
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal pytest project: passing, failing, skipped, flaky, plus a
    source file with a name-corresponding test for the affected_tests check."""
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "calc.py").write_text(SOURCE)
    (root / "tests" / "test_stable.py").write_text(STABLE)
    (root / "tests" / "test_flaky.py").write_text(FLAKY)
    (root / "tests" / "test_calc.py").write_text(COVERING)
    (root / "tests" / "test_unrelated.py").write_text("def test_nothing():\n    assert True\n")
    return root


@pytest.fixture
def ctx(project: Path, tmp_path: Path) -> ToolContext:
    config = load_config(REPO / "config")
    root = tmp_path / ".qaas"
    return ToolContext(
        store=RunStore("test-runner-run", root=root),
        maps=SystemMapStore(root),
        config=config,
        agent=config.agents["FORGE"],
        repo_root=project,
    )


@pytest.fixture
def tools(ctx: ToolContext) -> dict:
    return handlers(build_tools(ctx))


def outcome_of(result: dict, needle: str) -> str:
    tests = result["structuredContent"]["tests"]
    matches = [t for t in tests if needle in t["nodeid"]]
    assert matches, f"{needle} not in {[t['nodeid'] for t in tests]}"
    return matches[0]["outcome"]


# -- run_suite --------------------------------------------------------------


async def test_run_suite_returns_per_test_outcomes(tools):
    result = await tools["run_suite"]({"selector": "tests/test_stable.py"})
    assert not result.get("isError"), result

    payload = result["structuredContent"]
    assert outcome_of(result, "test_ok") == "passed"
    assert outcome_of(result, "test_bad") == "failed"
    assert outcome_of(result, "test_skipped") == "skipped"
    assert payload["totals"]["total"] == 3
    assert payload["totals"]["passed"] == 1
    assert payload["totals"]["failed"] == 1


async def test_run_suite_reports_the_failure_message(tools):
    result = await tools["run_suite"]({"selector": "tests/test_stable.py"})
    failing = [t for t in result["structuredContent"]["tests"] if t["outcome"] == "failed"]
    assert failing and failing[0]["message"], "a failure with no message is scraped text, not structure"
    assert "arithmetic drifted" in failing[0]["message"]


async def test_run_suite_accepts_a_k_expression(tools):
    """A selector that is not a path is passed to -k, not to the collector."""
    result = await tools["run_suite"]({"selector": "ok or nothing"})
    nodeids = [t["nodeid"] for t in result["structuredContent"]["tests"]]
    assert any("test_ok" in n for n in nodeids)
    assert not any("test_bad" in n for n in nodeids)


async def test_run_suite_refuses_a_cwd_outside_the_repo(tools, ctx):
    result = await tools["run_suite"]({"cwd": "../.."})
    assert result["isError"]
    assert "outside the repository" in result["content"][0]["text"]


async def test_run_suite_rejects_an_absurd_timeout(tools):
    result = await tools["run_suite"]({"timeout_s": 5000})
    assert result["isError"]
    assert "cap" in result["content"][0]["text"]


async def test_run_suite_errors_when_nothing_matches(tools):
    result = await tools["run_suite"]({"selector": "no_such_test_name_anywhere"})
    assert result["isError"]


# -- run_single -------------------------------------------------------------


async def test_run_single_returns_full_failure_output(tools):
    result = await tools["run_single"]({"test_id": "tests/test_stable.py::test_bad"})
    payload = result["structuredContent"]
    assert payload["outcome"] == "failed"
    assert "arithmetic drifted" in (payload["output"] or "")


async def test_run_single_on_a_passing_test(tools):
    result = await tools["run_single"]({"test_id": "tests/test_stable.py::test_ok"})
    assert result["structuredContent"]["outcome"] == "passed"


async def test_run_single_rejects_an_unknown_nodeid(tools):
    result = await tools["run_single"]({"test_id": "tests/test_stable.py::test_absent"})
    assert result["isError"]


# -- run_n_times (the flake detector) ---------------------------------------


async def test_run_n_times_reports_zero_flake_for_a_stable_test(tools):
    result = await tools["run_n_times"]({"test_id": "tests/test_stable.py::test_ok", "n": 3})
    payload = result["structuredContent"]
    assert payload["runs"] == 3
    assert payload["passed"] == 3
    assert payload["failed"] == 0
    assert payload["flake_rate"] == 0


async def test_run_n_times_detects_a_flaky_test(tools):
    result = await tools["run_n_times"]({"test_id": "tests/test_flaky.py::test_flaky", "n": 4})
    payload = result["structuredContent"]
    assert payload["runs"] == 4
    assert payload["flake_rate"] > 0, payload
    assert set(payload["outcomes"]) == {"passed", "failed"}


async def test_run_n_times_caps_the_run_count(tools):
    result = await tools["run_n_times"]({"test_id": "tests/test_stable.py::test_ok", "n": MAX_FLAKE_RUNS + 1})
    assert result["isError"]
    assert str(MAX_FLAKE_RUNS) in result["content"][0]["text"]


# -- affected_tests ---------------------------------------------------------


async def test_affected_tests_prefers_name_correspondence(tools):
    result = await tools["affected_tests"]({"paths": ["pkg/calc.py"]})
    affected = result["structuredContent"]["affected"]
    assert affected[0]["test_file"] == "tests/test_calc.py", affected
    assert result["structuredContent"]["heuristic"] is True


async def test_affected_tests_needs_paths(tools):
    assert (await tools["affected_tests"]({"paths": []}))["isError"]


# -- get_coverage -----------------------------------------------------------


async def test_get_coverage_is_honest_about_a_missing_tool(tools):
    """Either it measures, or it says it cannot. It never guesses."""
    result = await tools["get_coverage"]({})
    if importlib.util.find_spec("coverage") is None:
        assert result["isError"]
        assert "coverage is not installed" in result["content"][0]["text"]
    else:
        assert not result.get("isError"), result
        assert "overall_percent" in result["structuredContent"]


# -- timeouts and the JSON-report parser ------------------------------------


async def test_a_timeout_returns_partial_results_rather_than_hanging(tools, project):
    (project / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"
    )
    result = await tools["run_suite"]({"selector": "tests/test_slow.py", "timeout_s": 2})
    payload = result["structuredContent"]
    assert payload["timed_out"] is True
    assert payload["duration_s"] < 20
    assert "output_tail" in payload


def test_json_report_parser_reads_outcomes_and_longrepr(tmp_path):
    """The JSON path is preferred when the plugin is installed, and this
    environment does not have it — so exercise the parser against a report of
    the shape the plugin writes."""
    from qaas.mcp.test_runner import _parse_json_report

    report = tmp_path / "report.json"
    report.write_text(
        '{"tests": ['
        '{"nodeid": "tests/test_a.py::test_ok", "outcome": "passed",'
        ' "setup": {"duration": 0.001, "outcome": "passed"},'
        ' "call": {"duration": 0.01, "outcome": "passed"}},'
        '{"nodeid": "tests/test_a.py::test_bad", "outcome": "failed",'
        ' "call": {"duration": 0.02, "outcome": "failed", "longrepr": "assert 2 == 3"}}'
        "]}"
    )
    rows = _parse_json_report(report)
    assert [r["outcome"] for r in rows] == ["passed", "failed"]
    assert rows[0]["duration_s"] == pytest.approx(0.011)
    assert rows[1]["message"] == "assert 2 == 3"


def test_json_report_parser_declines_a_broken_report(tmp_path):
    """A truncated report must fall back to the terminal parser, not crash."""
    from qaas.mcp.test_runner import _parse_json_report

    broken = tmp_path / "report.json"
    broken.write_text("{not json")
    assert _parse_json_report(broken) is None
    assert _parse_json_report(tmp_path / "absent.json") is None
