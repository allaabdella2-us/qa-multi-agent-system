"""M2 verification: the test_runner server returns structure, not scraped text.

The suite under test is a throwaway package built in tmp_path — three tests with
known behaviour, one of them deliberately flaky — so the assertions are about
the parser and the flake maths, not about this repo's own suite.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from support import CONFIG_SEARCH, PACKAGED_CONFIG

from qaas.config import load_config
from qaas.mcp.context import ToolContext, handlers
from qaas.mcp import test_runner as tr
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
    config = load_config(search=CONFIG_SEARCH)
    root = tmp_path / ".qaas"
    return ToolContext(
        store=RunStore("test-runner-run", root=root),
        maps=SystemMapStore(root),
        config=config,
        agent=config.agents["REPRODUCER"],
        target_root=project,
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


async def test_a_path_that_is_not_there_is_not_quietly_a_keyword(tools):
    """A missing file must be said, not reinterpreted.

    path-vs-keyword was decided on `head.exists()` alone, so a path-shaped
    selector that was not there fell through to `["-k", selector]` and pytest
    collected the *whole* suite filtered by a keyword nobody meant as one.
    `_matched_nothing` hides that only while the suite is clean; a suite with
    collection errors still yields rows, so the run comes back a success and
    the result for a real file and for a path that never existed are identical.
    VERIFIER hit exactly that -- "6 tests: 6 error" for both a repro test and a
    control path -- and correctly threw the run out as evidence.
    """
    result = await tools["run_suite"]({"selector": "tests/test_does_not_exist.py"})
    assert result["is_error"], "a missing path must be an error, not a -k expression"
    assert "looks like a path" in result["content"][0]["text"]


async def test_a_keyword_expression_is_still_a_keyword_expression(tools):
    """The refusal keys on path *shape*, so prose must pass through untouched."""
    result = await tools["run_suite"]({"selector": "ok or nothing"})
    assert not result.get("is_error"), result["content"][0]["text"]


async def test_run_suite_refuses_a_cwd_outside_the_repo(tools, ctx):
    result = await tools["run_suite"]({"cwd": "../.."})
    assert result["isError"]
    assert "outside the repository" in result["content"][0]["text"]


async def test_run_suite_refuses_a_selector_outside_the_repo(tools, tmp_path):
    """`cwd` was contained; the selector beside it decides what actually runs.

    The module writes its marker at *import* time, so the file existing at all
    proves collection executed code outside the target root — not merely that a
    test was selected.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = tmp_path / "collected-outside"
    (outside / "test_evil.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        f"def test_evil():\n    assert True\n"
    )

    result = await tools["run_suite"]({"selector": "../outside/test_evil.py"})

    assert result["isError"]
    assert "outside the repository" in result["content"][0]["text"]
    assert not marker.exists(), "the module was imported despite the refusal"


async def test_a_selector_may_not_be_a_pytest_option(tools, tmp_path):
    """Selectors reach argv with no `--`, so a leading dash is an option.

    `get_coverage` takes a selector through the same helper, but it returns on a
    missing `coverage` package before it looks at one, so the guard there is
    covered by `_selector_refusal` directly rather than through the tool.
    """
    result = await tools["run_suite"]({"selector": f"-p=evil --rootdir={tmp_path}"})
    assert result["isError"]
    assert "may not start with '-'" in result["content"][0]["text"]


def test_every_selector_taking_tool_asks_the_same_question():
    """The guard is one helper, and each call site must actually call it."""
    import inspect

    from qaas.mcp import test_runner

    source = inspect.getsource(test_runner.build_tools)
    for tool_name in ("run_suite", "run_single", "run_n_times", "get_coverage"):
        body = source.split(f"async def {tool_name}(", 1)[1].split("\n    @tool(", 1)[0]
        assert "_selector_refusal(" in body, f"{tool_name} does not contain its selector"


@pytest.mark.parametrize("tool_name", ["run_single", "run_n_times"])
async def test_a_test_id_may_not_be_a_pytest_option(tools, tool_name):
    args = {"test_id": "-o=addopts=-p evil"}
    if tool_name == "run_n_times":
        args["n"] = 1
    result = await tools[tool_name](args)
    assert result["isError"]
    assert "may not start with '-'" in result["content"][0]["text"]


async def test_a_test_id_outside_the_repo_is_refused(tools, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_evil.py").write_text("def test_evil():\n    assert True\n")
    result = await tools["run_single"]({"test_id": "../outside/test_evil.py::test_evil"})
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


async def test_affected_tests_is_derived_from_imports_not_filenames(tools):
    """The graph answers first, and says so.

    `test_calc.py` imports `pkg.calc`, so this is a derived answer at distance 1
    rather than a filename guess. `heuristic` is the flag a caller reads to know
    which it got -- "no tests are affected" and "I cannot see this language" are
    different answers and the tool must not conflate them.
    """
    result = await tools["affected_tests"]({"paths": ["pkg/calc.py"]})
    body = result["structuredContent"]
    assert body["affected"][0]["test_file"] == "tests/test_calc.py", body["affected"]
    assert body["affected"][0]["distance"] == 1
    assert body["heuristic"] is False
    assert body["method"] == "import-graph"
    # A test that imports nothing of the sort must not be dragged in.
    assert "tests/test_unrelated.py" not in [a["test_file"] for a in body["affected"]]


async def test_affected_tests_falls_back_when_the_graph_cannot_answer(tools, project):
    """A non-Python change, in a repository the graph can otherwise read.

    The fallback is the point: the target can be any language, and returning "no
    tests are affected" for a whole language would be worse than the heuristic
    it replaced. The answer says which method produced it either way.
    """
    (project / "pkg" / "calc.go").write_text("package pkg\n")
    result = await tools["affected_tests"]({"paths": ["pkg/calc.go"]})
    body = result["structuredContent"]
    assert body["heuristic"] is True
    assert body["method"] == "filename-heuristic"


async def test_a_test_two_hops_away_is_found_at_all(tools, project):
    """The case the filename heuristic scores zero, which is why this exists.

    `helpers.py` is imported by `calc.py`, which is imported by `test_calc.py`.
    Nothing about the name `test_calc` resembles `helpers`, so
    `regression-suite-selection`'s step 3 -- "a fix inside a shared helper breaks
    its consumers, not itself" -- was delegated to a ranking that could not see
    it.
    """
    (project / "pkg" / "helpers.py").write_text("def helper():\n    return 1\n")
    calc = project / "pkg" / "calc.py"
    calc.write_text("from pkg.helpers import helper\n" + calc.read_text())

    result = await tools["affected_tests"]({"paths": ["pkg/helpers.py"]})
    body = result["structuredContent"]
    reached = {a["test_file"]: a["distance"] for a in body["affected"]}
    assert reached.get("tests/test_calc.py") == 2, body["affected"]

    # And the heuristic really cannot: it is the control for the claim above.
    from qaas.mcp.test_runner import _collect_test_files, _score_tests

    guessed = _score_tests(project, ["pkg/helpers.py"], _collect_test_files(project))
    assert "tests/test_calc.py" not in [g["test_file"] for g in guessed]


TS_CONFIG = """
{
  // Next.js writes a tsconfig with comments in it.
  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./src/*"]}},
}
"""


@pytest.fixture
def ts_project(tmp_path: Path) -> Path:
    """A vitest project shaped like a real Next app, aliases and all.

    No node and no npm: `affected_tests` never starts a subprocess, it reads.
    """
    root = tmp_path / "ts"
    (root / "src" / "lib").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^1"}}))
    (root / "tsconfig.json").write_text(TS_CONFIG)
    (root / "src" / "lib" / "dates.ts").write_text("export const startOfDay = (d: Date) => d;\n")
    (root / "src" / "lib" / "orders.ts").write_text(
        'import { startOfDay } from "@/lib/dates";\nexport const total = () => startOfDay(new Date());\n'
    )
    (root / "src" / "lib" / "orders.test.ts").write_text(
        'import { it } from "vitest";\nimport { total } from "./orders";\nit("totals", () => total());\n'
    )
    (root / "src" / "lib" / "unrelated.test.ts").write_text(
        'import { it } from "vitest";\nit("nothing", () => {});\n'
    )
    return root


@pytest.fixture
def ts_tools(ts_project: Path, tmp_path: Path) -> dict:
    config = load_config(search=CONFIG_SEARCH)
    root = tmp_path / ".qaas-ts"
    return handlers(
        build_tools(
            ToolContext(
                store=RunStore("ts-run", root=root),
                maps=SystemMapStore(root),
                config=config,
                agent=config.agents["REPRODUCER"],
                target_root=ts_project,
            )
        )
    )


async def test_affected_tests_is_derived_for_typescript_too(ts_tools):
    """The gap this closes: the runner already ran vitest and jest, and the
    *graph* was still Python-only -- so a TS target's suite ran and its
    selection fell back to filenames, which is the half that makes this more
    than a guess. `orders.test.ts` reaches `dates.ts` through `@/lib/orders`.
    """
    result = await ts_tools["affected_tests"]({"paths": ["src/lib/dates.ts"]})
    body = result["structuredContent"]
    assert body["method"] == "import-graph", body
    assert body["heuristic"] is False
    reached = {a["test_file"]: a["distance"] for a in body["affected"]}
    assert reached == {"src/lib/orders.test.ts": 2}


async def test_js_test_files_are_collected_at_all(ts_tools, ts_project):
    """`_collect_test_files` globbed `*.py`, so on a vitest project this tool
    answered "No test files found" before it ranked anything."""
    from qaas.mcp.test_runner import _collect_test_files

    names = {p.name for p in _collect_test_files(ts_project)}
    assert names == {"orders.test.ts", "unrelated.test.ts"}
    assert not (await ts_tools["affected_tests"]({"paths": ["src/lib/dates.ts"]})).get("isError")


async def test_a_language_the_graph_cannot_read_is_named_not_averaged_away(tools, project):
    """"Nothing imports that" and "I cannot read this language" are different
    answers, and only this side of the call knows which one it gave."""
    (project / "pkg" / "calc.go").write_text("package pkg\n")
    result = await tools["affected_tests"]({"paths": ["pkg/calc.go"]})
    body = result["structuredContent"]
    assert ".go" in (body["unreadable"] or "")
    assert ".go" in result["content"][0]["text"]


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


# -- what a target's own test suite runs with -------------------------------


def test_the_targets_tests_never_see_this_users_credentials(monkeypatch):
    """The child environment is an allowlist, not a copy with two keys removed.

    It was `dict(os.environ, ...)` minus `PYTEST_ADDOPTS` and
    `PYTEST_CURRENT_TEST`, so the target repository's suite -- someone else's
    code, cloned from a pasted URL under `qaas run --repo` -- ran with this
    user's `ANTHROPIC_API_KEY`, `JIRA_API_TOKEN` and `GITHUB_TOKEN` in scope. A
    `conftest.py` that reads `os.environ` is the whole exploit, and running the
    target's tests is this server's purpose rather than an edge case.
    """
    from qaas.mcp.test_runner import _child_env

    for name, value in {
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "JIRA_API_TOKEN": "jira-secret",
        "GITHUB_TOKEN": "gh-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "SOME_COMPANY_INTERNAL_URL": "https://internal",
    }.items():
        monkeypatch.setenv(name, value)

    env = _child_env()
    assert "secret" not in " ".join(env.values()).lower()
    for leaked in ("ANTHROPIC_API_KEY", "JIRA_API_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        assert leaked not in env, leaked
    # An allowlist, so an unrecognised variable is dropped too -- the set of
    # secrets a machine holds is open-ended and the set a suite needs is not.
    assert "SOME_COMPANY_INTERNAL_URL" not in env


def test_what_a_target_actually_needs_still_reaches_it(monkeypatch):
    monkeypatch.setenv("QAAS_TARGET_DATABASE_URL", "postgres://localhost/test")
    env = _child_env_for_test({"DATABASE_URL": "explicitly passed"})
    assert env["QAAS_TARGET_DATABASE_URL"] == "postgres://localhost/test"
    assert env["DATABASE_URL"] == "explicitly passed"
    assert "PATH" in env and env["COLUMNS"] == "250"


def _child_env_for_test(extra):
    from qaas.mcp.test_runner import _child_env

    return _child_env(extra)


# -- which runner a project uses -------------------------------------------
#
# This server ran `python -m pytest` unconditionally, so on a TypeScript repo
# with three vitest files it answered "No tests matched the default
# collection". Everything that depends on running a test went with it:
# REPRODUCER cannot commit a failing test the system can execute, VERIFIER
# cannot verify a fix by running one, FIXER cannot check its own work.
# Discovery reads source and works anywhere; proving and verifying was Python
# only, which is the half that makes this more than a linter.


def _package_json(root, **fields):
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(fields), encoding="utf-8")
    return root


def test_a_project_with_no_package_json_is_pytest(tmp_path):
    """Every existing Python target must stay on exactly the path it was on."""
    assert tr._detect_runner(tmp_path) == "pytest"


def test_vitest_and_jest_are_detected_from_what_the_project_declares(tmp_path):
    vite = _package_json(tmp_path / "a", devDependencies={"vitest": "^2.0.0"})
    jest = _package_json(tmp_path / "b", devDependencies={"jest": "^29.0.0"})
    assert tr._detect_runner(vite) == "vitest"
    assert tr._detect_runner(jest) == "jest"


def test_a_runner_named_only_in_a_script_still_counts(tmp_path):
    """`npx vitest run` in scripts with no dependency entry is a real layout."""
    root = _package_json(tmp_path / "c", scripts={"test": "vitest run"})
    assert tr._detect_runner(root) == "vitest"


def test_an_unreadable_package_json_falls_back_rather_than_raising(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "package.json").write_text("{ not json", encoding="utf-8")
    assert tr._detect_runner(root) == "pytest"


def test_a_js_id_is_split_into_a_file_and_a_title(tmp_path):
    """vitest and jest have no nodeid: a file narrows, the name pattern matches
    the title. One token, so the title is a value and never a flag."""
    root = _package_json(tmp_path / "e", devDependencies={"vitest": "^2.0.0"})
    assert tr._single_selectors(root, "src/lib/dates.test.ts::utcDayKey works") == [
        "src/lib/dates.test.ts", "--testNamePattern=utcDayKey works",
    ]
    assert tr._single_selectors(root, "utcDayKey works") == ["--testNamePattern=utcDayKey works"]


def test_a_pytest_nodeid_is_still_passed_through_whole(tmp_path):
    assert tr._single_selectors(tmp_path, "tests/test_x.py::test_y") == [
        "tests/test_x.py::test_y"
    ]


def test_the_jest_json_shape_becomes_the_same_rows_pytest_produces(tmp_path):
    """vitest emits jest's shape, so one parser serves both."""
    report = tmp_path / "r.json"
    report.write_text(json.dumps({
        "testResults": [{
            "name": str(tmp_path / "src" / "money.test.ts"),
            "assertionResults": [
                {"fullName": "toMinor rounds half up", "status": "passed", "duration": 12},
                {"fullName": "toMinor rejects NaN", "status": "failed", "duration": 3,
                 "failureMessages": ["expected 1 to be 2"]},
                {"fullName": "toMinor handles bigint", "status": "todo", "duration": None},
            ],
        }]
    }), encoding="utf-8")

    rows = tr._parse_js_report(report, tmp_path)
    assert [r["outcome"] for r in rows] == ["passed", "failed", "skipped"]
    assert rows[0]["nodeid"] == "src/money.test.ts::toMinor rounds half up"
    assert rows[0]["duration_s"] == 0.012
    assert "expected 1 to be 2" in rows[1]["message"]
    # A missing duration must not crash the row it belongs to.
    assert rows[2]["duration_s"] == 0.0


def test_a_report_that_is_not_there_is_no_rows_rather_than_a_crash(tmp_path):
    assert tr._parse_js_report(tmp_path / "absent.json", tmp_path) is None


def test_the_js_runners_say_nothing_matched_in_prose(tmp_path):
    """There is no distinct exit code: both exit 1 and write a sentence."""
    said = tr._Completed(
        argv=["npx"], returncode=1, stdout="No test files found, exiting with code 1",
        stderr="", duration_s=0.1, timed_out=False, started=True,
    )
    assert tr._matched_nothing(said)


def test_a_typescript_selector_is_recognised_as_a_path(tmp_path):
    """The path-shape test was `.py` only, so a missing .ts became a -t filter."""
    for name in ("src/x.test.ts", "src/x.test.tsx", "x.spec.js", "a/b.mjs"):
        assert tr._looks_like_a_path(name), name
    assert not tr._looks_like_a_path("rounds half up")
