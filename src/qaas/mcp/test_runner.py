"""The `test_runner` MCP server — structured outcomes, never scraped CLI text.

An agent that reads pytest's terminal output makes two kinds of mistake: it
misreads a summary line, and it argues with itself about what "2 failed, 1
passed" implies for the one test it cares about. Both disappear if the tool
returns a list of `{nodeid, outcome, duration_s, message}` and the agent reads a
field. §5.2 asks for this server for exactly that reason.

Parsing strategy, in order of preference:

  * `pytest-json-report` if it is installed. Exact durations, exact longrepr.
  * The terminal output otherwise, run with `-v -rfE --durations=0` so the
    facts we need are on lines with a stable shape. Robust beats clever here:
    outcomes come from the per-test progress lines, failure messages from the
    short summary, durations from the durations table, and anything unparseable
    degrades to a total from the exit code rather than to a wrong answer.

Every subprocess is argv-only and time-boxed. A caller never supplies a command
string, and a run that exceeds its timeout returns what it managed to collect,
flagged, instead of hanging the agent's turn.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas.mcp.context import ToolContext, err, ok

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900

# §10 says flake rate is measured, not guessed, but a 200-run loop is a budget
# incident. Twenty runs already resolves a 5%-flaky test most of the time.
MAX_FLAKE_RUNS = 20

# How much raw output to hand back when parsing found nothing useful. Enough to
# diagnose a collection error, not enough to flood the agent's context.
OUTPUT_TAIL = 6_000

# pytest exit codes we treat specially (see pytest.ExitCode).
_EXIT_NO_TESTS = 5
_EXIT_USAGE_ERROR = 4

_OUTCOMES = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "xfailed",
    "XPASS": "xpassed",
}

# `tests/test_a.py::test_x PASSED   [ 50%]` — the -v progress line.
_PROGRESS_RE = re.compile(
    r"^(?P<nodeid>\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
# `FAILED tests/test_a.py::test_x - AssertionError: ...` — the -rfE summary line.
_SUMMARY_RE = re.compile(
    r"^(?P<outcome>FAILED|ERROR)\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<message>.*))?$"
)
# `0.01s call     tests/test_a.py::test_x` — the --durations table.
_DURATION_RE = re.compile(r"^(?P<seconds>\d+\.\d+)s\s+(?:call|setup|teardown)\s+(?P<nodeid>\S+)$")

# pytest reports an unknown nodeid as a usage error, not as "no tests ran", so
# the two have to be told apart by the message rather than by the exit code.
_NOT_FOUND_RE = re.compile(r"^ERROR: not found: ", re.MULTILINE)
_UNKNOWN_OPTION_RE = re.compile(r"unrecognized (arguments|option)", re.IGNORECASE)

_FAILURE_HEADER_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+$", re.MULTILINE)
_SUMMARY_HEADER_RE = re.compile(r"^=+ (short test summary info|warnings summary) =+", re.MULTILINE)


# ---------------------------------------------------------------------------
# subprocess plumbing
# ---------------------------------------------------------------------------


@dataclass
class _Completed:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    started: bool = True


def _decode(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode(errors="replace")


def _run(argv: list[str], cwd: Path, timeout_s: int, env_extra: dict[str, str] | None = None) -> _Completed:
    """Run argv under a hard timeout, returning partial output if it expires.

    `subprocess.run` kills the child and hands the partial streams back on the
    exception, which is the difference between "the suite hung, here is how far
    it got" and an agent staring at nothing.
    """
    env = dict(os.environ, **(env_extra or {}))
    # A wide terminal keeps pytest from wrapping nodeids across lines, which is
    # the one thing that would break the progress-line parser.
    env["COLUMNS"] = "250"
    # The parent process is usually pytest itself (this server is exercised from
    # a test suite); its addopts must not leak into the child's run.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return _Completed(
            argv, -1, _decode(exc.stdout), _decode(exc.stderr), time.monotonic() - started, True
        )
    except OSError as exc:
        return _Completed(argv, -1, "", f"could not start {argv[0]}: {exc}", 0.0, False, started=False)
    return _Completed(
        argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started, False
    )


async def _run_async(argv: list[str], cwd: Path, timeout_s: int, env_extra: dict[str, str] | None = None) -> _Completed:
    """Off the event loop: a 300s suite must not block the other tools."""
    return await asyncio.to_thread(_run, argv, cwd, timeout_s, env_extra)


def _resolve_cwd(ctx: ToolContext, raw: str | None) -> tuple[Path | None, str | None]:
    """Working directory for a run: the repo root, or a directory inside it.

    Resolved before the containment check so `..` and symlinks cannot walk out
    of the checkout the run is supposed to be confined to.
    """
    root = ctx.target_root.resolve()
    if not raw:
        return root, None
    candidate = Path(raw)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and not resolved.is_relative_to(root):
        return None, f"cwd '{raw}' resolves to {resolved}, outside the repository ({root})."
    if not resolved.is_dir():
        return None, f"cwd '{raw}' is not a directory."
    return resolved, None


def _timeout(args: dict[str, Any]) -> tuple[int, str | None]:
    raw = args.get("timeout_s")
    if raw is None:
        return DEFAULT_TIMEOUT_S, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S, f"timeout_s must be a number, got {raw!r}."
    if value < 1:
        return DEFAULT_TIMEOUT_S, "timeout_s must be at least 1 second."
    if value > MAX_TIMEOUT_S:
        return DEFAULT_TIMEOUT_S, (
            f"timeout_s {value} exceeds the {MAX_TIMEOUT_S}s cap. A test that needs "
            "longer than fifteen minutes is a finding in itself, not a longer wait."
        )
    return value, None


# ---------------------------------------------------------------------------
# pytest invocation and parsing
# ---------------------------------------------------------------------------


def _json_report_available() -> bool:
    """Whether the child interpreter (which is this one) has the plugin."""
    return importlib.util.find_spec("pytest_jsonreport") is not None


def _base_argv(selectors: list[str], json_report_path: Path | None) -> list[str]:
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=short"]
    if json_report_path is not None:
        argv += ["--json-report", f"--json-report-file={json_report_path}", "-q"]
    else:
        argv += ["-v", "-rfE", "--durations=0", "--durations-min=0"]
    return argv + selectors


def _parse_json_report(path: Path) -> list[dict[str, Any]] | None:
    """Per-test rows from pytest-json-report, or None if it wrote nothing usable."""
    try:
        report = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    raw_tests = report.get("tests")
    if not isinstance(raw_tests, list):
        return None

    rows: list[dict[str, Any]] = []
    for entry in raw_tests:
        phases = [entry.get(p) for p in ("setup", "call", "teardown")]
        duration = sum(p.get("duration", 0.0) for p in phases if isinstance(p, dict))
        message = None
        for phase in phases:
            if isinstance(phase, dict) and phase.get("outcome") in {"failed", "error"}:
                message = _shorten(_stringify_longrepr(phase.get("longrepr")))
                break
        rows.append(
            {
                "nodeid": entry.get("nodeid", "?"),
                "outcome": entry.get("outcome", "unknown"),
                "duration_s": round(duration, 4),
                "message": message,
            }
        )
    return rows


def _stringify_longrepr(longrepr: Any) -> str:
    """pytest-json-report emits a string, or a dict when tracebacks are structured."""
    if isinstance(longrepr, str):
        return longrepr
    if isinstance(longrepr, dict):
        crash = longrepr.get("crash")
        if isinstance(crash, dict) and crash.get("message"):
            return str(crash["message"])
        return json.dumps(longrepr)[:2_000]
    return "" if longrepr is None else str(longrepr)


def _shorten(text: str, limit: int = 1_200) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


def _parse_terminal(stdout: str) -> list[dict[str, Any]]:
    """Per-test rows from pytest's terminal output.

    Three independent line shapes, merged: outcomes from the progress lines
    (the only source that names every test, skips included), messages from the
    short summary, durations from the durations table. Any one of them missing
    degrades a field, not the whole result.
    """
    outcomes: dict[str, str] = {}
    order: list[str] = []
    messages: dict[str, str] = {}
    durations: dict[str, float] = {}

    for line in stdout.splitlines():
        line = line.rstrip()
        summary = _SUMMARY_RE.match(line)
        if summary:
            nodeid = summary.group("nodeid").rstrip(":")
            message = (summary.group("message") or "").strip()
            if message:
                messages[nodeid] = message
            outcomes.setdefault(nodeid, _OUTCOMES[summary.group("outcome")])
            if nodeid not in order:
                order.append(nodeid)
            continue

        progress = _PROGRESS_RE.match(line)
        if progress:
            nodeid = progress.group("nodeid")
            if nodeid not in outcomes:
                order.append(nodeid)
            outcomes[nodeid] = _OUTCOMES[progress.group("outcome")]
            continue

        duration = _DURATION_RE.match(line.strip())
        if duration:
            nodeid = duration.group("nodeid")
            durations[nodeid] = durations.get(nodeid, 0.0) + float(duration.group("seconds"))

    return [
        {
            "nodeid": nodeid,
            "outcome": outcomes.get(nodeid, "unknown"),
            "duration_s": round(durations[nodeid], 4) if nodeid in durations else None,
            "message": _shorten(messages.get(nodeid, "")),
        }
        for nodeid in order
    ]


def _failure_detail(stdout: str) -> str | None:
    """The FAILURES/ERRORS section verbatim — what `run_single` is actually for."""
    header = _FAILURE_HEADER_RE.search(stdout)
    if not header:
        return None
    tail = stdout[header.start():]
    end = _SUMMARY_HEADER_RE.search(tail, 1)
    section = tail[: end.start()] if end else tail
    return _shorten(section, 8_000)


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(row["outcome"] for row in rows)
    return {"total": len(rows), **{outcome: counts[outcome] for outcome in sorted(counts)}}


async def _pytest(cwd: Path, selectors: list[str], timeout_s: int) -> tuple[_Completed, list[dict[str, Any]], str]:
    """Run pytest and return (process, per-test rows, parser used)."""
    use_json = _json_report_available()
    with tempfile.TemporaryDirectory(prefix="qaas-pytest-") as tmp:
        report_path = Path(tmp) / "report.json" if use_json else None
        proc = await _run_async(_base_argv(selectors, report_path), cwd, timeout_s)

        # An unknown option (an older pytest without --durations-min, say) is a
        # usage error, not a test failure. Retry once with the minimal flag set
        # rather than reporting a suite that never ran.
        if proc.returncode == _EXIT_USAGE_ERROR and not use_json and _UNKNOWN_OPTION_RE.search(
            proc.stdout + proc.stderr
        ):
            argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=short", "-v", "-rfE"]
            proc = await _run_async(argv + selectors, cwd, timeout_s)

        rows: list[dict[str, Any]] | None = None
        if report_path is not None:
            rows = _parse_json_report(report_path)
        parser = "json-report" if rows is not None else "terminal"
        if rows is None:
            rows = _parse_terminal(proc.stdout)
    return proc, rows, parser


def _matched_nothing(proc: _Completed) -> bool:
    """Whether the selector picked no test at all, however pytest said so."""
    if proc.returncode == _EXIT_NO_TESTS:
        return True
    return proc.returncode == _EXIT_USAGE_ERROR and bool(
        _NOT_FOUND_RE.search(proc.stdout + proc.stderr)
    )


def _outcome_of(rows: list[dict[str, Any]], test_id: str, proc: _Completed) -> str:
    """One test's outcome, falling back to the exit code if parsing missed it."""
    for row in rows:
        if row["nodeid"] == test_id or row["nodeid"].endswith(test_id):
            return row["outcome"]
    if proc.timed_out:
        return "timeout"
    if _matched_nothing(proc):
        return "not_collected"
    if proc.returncode == 0:
        return "passed"
    return "failed"


def _tail(proc: _Completed) -> str:
    combined = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return combined[-OUTPUT_TAIL:]


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


def build_tools(ctx: ToolContext) -> list:
    """The test-runner tools, bound to one agent's run context.

    Split from `build` so tests can call the handlers directly without standing
    up an MCP transport.
    """

    _CWD_SCHEMA = {
        "cwd": {"type": "string", "description": "Directory to run in. Defaults to the repo root; must stay inside it."},
        "timeout_s": {"type": "number", "description": f"Seconds before the run is killed. Default {DEFAULT_TIMEOUT_S}, cap {MAX_TIMEOUT_S}."},
    }

    @tool(
        "run_suite",
        "Run the test suite, optionally narrowed by a selector, and get structured "
        "per-test outcomes back. Read the fields; do not parse the summary text.",
        {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "A path ('tests/api'), a nodeid, or a -k expression ('order and not slow').",
                },
                **_CWD_SCHEMA,
            },
        },
    )
    async def run_suite(args: dict[str, Any]) -> dict[str, Any]:
        cwd, cwd_error = _resolve_cwd(ctx, args.get("cwd"))
        if cwd_error:
            return err(cwd_error)
        timeout_s, timeout_error = _timeout(args)
        if timeout_error:
            return err(timeout_error)

        selector = (args.get("selector") or "").strip()
        selectors: list[str] = []
        if selector:
            # A selector that names something on disk is a path; anything else is
            # a -k expression. Guessing wrong wastes a run, so the check is a
            # filesystem question, not a syntax one.
            head = selector.split("::", 1)[0]
            selectors = [selector] if (cwd / head).exists() else ["-k", selector]

        proc, rows, parser = await _pytest(cwd, selectors, timeout_s)
        if not proc.started:
            return err(proc.stderr)
        if _matched_nothing(proc) and not rows:
            return err(
                f"No tests matched {selector or 'the default collection'} in {cwd}. "
                "Check the selector against the files that exist."
            )

        totals = _totals(rows)
        structured = {
            "tests": rows,
            "totals": totals,
            "exit_code": proc.returncode,
            "timed_out": proc.timed_out,
            "duration_s": round(proc.duration_s, 3),
            "parser": parser,
            "cwd": str(cwd),
            "selector": selector or None,
        }
        if proc.timed_out or not rows:
            structured["output_tail"] = _tail(proc)

        headline = ", ".join(f"{count} {name}" for name, count in totals.items() if name != "total")
        note = f" TIMED OUT after {timeout_s}s; these are partial results." if proc.timed_out else ""
        return ok(
            f"{totals['total']} tests: {headline or 'none run'} in {proc.duration_s:.1f}s.{note}",
            **structured,
        )

    @tool(
        "run_single",
        "Run one test by nodeid and get its full failure output. Use this to confirm "
        "a repro, not to browse the suite.",
        {
            "type": "object",
            "required": ["test_id"],
            "properties": {
                "test_id": {"type": "string", "description": "e.g. 'tests/api/test_orders.py::test_returns_500'"},
                **_CWD_SCHEMA,
            },
        },
    )
    async def run_single(args: dict[str, Any]) -> dict[str, Any]:
        cwd, cwd_error = _resolve_cwd(ctx, args.get("cwd"))
        if cwd_error:
            return err(cwd_error)
        timeout_s, timeout_error = _timeout(args)
        if timeout_error:
            return err(timeout_error)

        test_id = str(args["test_id"]).strip()
        if not test_id:
            return err("test_id is required.")

        proc, rows, parser = await _pytest(cwd, [test_id], timeout_s)
        if not proc.started:
            return err(proc.stderr)
        if _matched_nothing(proc):
            return err(
                f"'{test_id}' matched no test in {cwd}. Nodeids look like "
                "'path/to/test_file.py::test_name'."
            )

        outcome = _outcome_of(rows, test_id, proc)
        row = next((r for r in rows if r["nodeid"] == test_id or r["nodeid"].endswith(test_id)), None)
        detail = _failure_detail(proc.stdout)
        structured = {
            "nodeid": test_id,
            "outcome": outcome,
            "duration_s": (row or {}).get("duration_s"),
            "message": (row or {}).get("message"),
            "output": detail or (_tail(proc) if outcome != "passed" else None),
            "exit_code": proc.returncode,
            "timed_out": proc.timed_out,
            "parser": parser,
        }
        return ok(f"{test_id}: {outcome} in {proc.duration_s:.1f}s.", **structured)

    @tool(
        "run_n_times",
        "Run one test repeatedly and measure its flake rate — the share of runs whose "
        "outcome differs from the majority. A non-zero rate means the defect is flaky, "
        "which is a different finding from a defect that always reproduces (§10).",
        {
            "type": "object",
            "required": ["test_id", "n"],
            "properties": {
                "test_id": {"type": "string"},
                "n": {"type": "number", "description": f"Number of runs, 1-{MAX_FLAKE_RUNS}."},
                **_CWD_SCHEMA,
            },
        },
    )
    async def run_n_times(args: dict[str, Any]) -> dict[str, Any]:
        cwd, cwd_error = _resolve_cwd(ctx, args.get("cwd"))
        if cwd_error:
            return err(cwd_error)
        timeout_s, timeout_error = _timeout(args)
        if timeout_error:
            return err(timeout_error)

        test_id = str(args["test_id"]).strip()
        if not test_id:
            return err("test_id is required.")
        try:
            n = int(args["n"])
        except (TypeError, ValueError):
            return err(f"n must be a whole number, got {args['n']!r}.")
        if n < 1:
            return err("n must be at least 1.")
        if n > MAX_FLAKE_RUNS:
            return err(
                f"n={n} exceeds the {MAX_FLAKE_RUNS}-run cap. Twenty runs resolve a "
                "5% flake most of the time; more is a budget problem, not better evidence."
            )

        outcomes: list[str] = []
        messages: list[str] = []
        for attempt in range(n):
            proc, rows, _ = await _pytest(cwd, [test_id], timeout_s)
            if not proc.started:
                return err(proc.stderr)
            if attempt == 0 and _matched_nothing(proc):
                return err(f"'{test_id}' matched no test in {cwd}.")
            outcome = _outcome_of(rows, test_id, proc)
            outcomes.append(outcome)
            row = next((r for r in rows if r["nodeid"] == test_id or r["nodeid"].endswith(test_id)), None)
            if row and row.get("message"):
                messages.append(f"run {attempt + 1}: {row['message']}")

        counts = Counter(outcomes)
        majority, majority_count = counts.most_common(1)[0]
        flake_rate = round((n - majority_count) / n, 4)
        passed = counts.get("passed", 0)

        verdict = (
            f"stable ({majority})" if flake_rate == 0
            else f"FLAKY: {flake_rate:.0%} of runs disagreed with the majority ({majority})"
        )
        return ok(
            f"{test_id} over {n} runs — {passed} passed, {n - passed} not passed. {verdict}.",
            runs=n,
            passed=passed,
            failed=n - passed,
            flake_rate=flake_rate,
            majority_outcome=majority,
            outcomes=outcomes,
            counts=dict(counts),
            messages=messages[:5],
        )

    @tool(
        "affected_tests",
        "Heuristic: given changed source paths, the test files most likely to cover them. "
        "It is a heuristic, not a coverage-derived answer — treat the ranking as a place to "
        "start, and run the full suite before concluding nothing broke.",
        {
            "type": "object",
            "required": ["paths"],
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Repo-relative changed paths."},
                "cwd": _CWD_SCHEMA["cwd"],
            },
        },
    )
    async def affected_tests(args: dict[str, Any]) -> dict[str, Any]:
        cwd, cwd_error = _resolve_cwd(ctx, args.get("cwd"))
        if cwd_error:
            return err(cwd_error)
        raw_paths = args.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return err("paths must be a non-empty array of repo-relative paths.")

        candidates = await asyncio.to_thread(_collect_test_files, cwd)
        if not candidates:
            return err(f"No test files found under {cwd}.")

        scored = await asyncio.to_thread(_score_tests, cwd, [str(p) for p in raw_paths], candidates)
        if not scored:
            return ok(
                "No test file looks related to those paths. That is itself worth reporting: "
                "the change may be untested.",
                affected=[],
                heuristic=True,
            )
        return ok(
            "Likely covering tests, best first: "
            + ", ".join(item["test_file"] for item in scored[:10]),
            affected=scored[:25],
            heuristic=True,
            searched=len(candidates),
        )

    @tool(
        "get_coverage",
        "Per-file line coverage, measured by running the suite under coverage.py. "
        "Returns an error if coverage is not installed rather than an estimate.",
        {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Limit measurement to these source paths."},
                "selector": {"type": "string", "description": "Optional -k expression or path to narrow the suite."},
                **_CWD_SCHEMA,
            },
        },
    )
    async def get_coverage(args: dict[str, Any]) -> dict[str, Any]:
        if importlib.util.find_spec("coverage") is None:
            return err(
                "coverage is not installed in this environment, so there is no coverage "
                "number to report. Install `coverage` (or add it to the dev extras) and "
                "call again; do not estimate coverage from reading the code."
            )

        cwd, cwd_error = _resolve_cwd(ctx, args.get("cwd"))
        if cwd_error:
            return err(cwd_error)
        timeout_s, timeout_error = _timeout(args)
        if timeout_error:
            return err(timeout_error)

        paths = [str(p) for p in (args.get("paths") or [])]
        selector = (args.get("selector") or "").strip()
        selectors: list[str] = []
        if selector:
            head = selector.split("::", 1)[0]
            selectors = [selector] if (cwd / head).exists() else ["-k", selector]

        with tempfile.TemporaryDirectory(prefix="qaas-coverage-") as tmp:
            data_file = Path(tmp) / ".coverage"
            json_file = Path(tmp) / "coverage.json"
            env_extra = {"COVERAGE_FILE": str(data_file)}
            run_argv = [sys.executable, "-m", "coverage", "run"]
            if paths:
                run_argv.append("--source=" + ",".join(paths))
            run_argv += ["-m", "pytest", "-q", "-p", "no:cacheprovider", *selectors]

            run = await _run_async(run_argv, cwd, timeout_s, env_extra)
            if not run.started:
                return err(run.stderr)
            if run.timed_out:
                return err(f"The coverage run exceeded {timeout_s}s and was killed. Narrow it with `selector`.")

            report = await _run_async(
                [sys.executable, "-m", "coverage", "json", "-o", str(json_file)],
                cwd,
                min(timeout_s, 120),
                env_extra,
            )
            if not json_file.exists():
                return err(
                    "coverage produced no report: "
                    + (_tail(report) or _tail(run) or "no output")
                )
            try:
                data = json.loads(json_file.read_text())
            except ValueError as exc:
                return err(f"coverage report was not valid JSON: {exc}")

        files = {
            name: round(info.get("summary", {}).get("percent_covered", 0.0), 2)
            for name, info in (data.get("files") or {}).items()
        }
        if paths:
            wanted = tuple(paths)
            files = {n: pct for n, pct in files.items() if n.startswith(wanted)} or files
        overall = round((data.get("totals") or {}).get("percent_covered", 0.0), 2)
        lowest = sorted(files.items(), key=lambda kv: kv[1])[:5]

        return ok(
            f"Overall line coverage {overall}% across {len(files)} files. "
            + ("Lowest: " + ", ".join(f"{n} {p}%" for n, p in lowest) if lowest else ""),
            overall_percent=overall,
            files=files,
            tests_exit_code=run.returncode,
        )

    return [run_suite, run_single, run_n_times, affected_tests, get_coverage]


# ---------------------------------------------------------------------------
# affected_tests heuristic
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache", ".qaas"}


def _collect_test_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            found.append(path)
    return found


def _score_tests(root: Path, changed: list[str], candidates: list[Path]) -> list[dict[str, Any]]:
    """Rank test files by three independent, cheap signals.

    Name correspondence is the strongest (`foo.py` -> `test_foo.py` is a
    convention people actually follow), an import of the changed module is next,
    and sharing a directory is the weak tie-breaker that catches package-level
    test layouts. Deliberately no AST or coverage database: this runs before the
    agent knows which suite to run, so it must be fast and never wrong-by-crash.
    """
    scores: dict[Path, int] = {}
    reasons: dict[Path, list[str]] = {}

    for raw in changed:
        source = Path(raw)
        stem = source.stem
        module_dir = source.parent.as_posix()
        for candidate in candidates:
            score = 0
            why: list[str] = []
            name = candidate.name
            if name in {f"test_{stem}.py", f"{stem}_test.py"}:
                score += 100
                why.append(f"name matches {source.name}")
            elif stem and stem in name:
                score += 25
                why.append(f"filename mentions '{stem}'")

            if stem:
                try:
                    text = candidate.read_text(errors="ignore")
                except OSError:
                    text = ""
                if re.search(rf"\b(import|from)\b[^\n]*\b{re.escape(stem)}\b", text):
                    score += 40
                    why.append(f"imports '{stem}'")

            if module_dir and module_dir not in {".", ""} and module_dir in candidate.as_posix():
                score += 10
                why.append(f"shares directory {module_dir}")

            if score:
                scores[candidate] = scores.get(candidate, 0) + score
                reasons.setdefault(candidate, []).extend(w for w in why if w not in reasons.get(candidate, []))

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [
        {
            "test_file": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
            "score": score,
            "why": reasons.get(path, []),
        }
        for path, score in ranked
    ]


def build(ctx: ToolContext):
    """Construct the test_runner MCP server bound to one agent's run context."""
    return create_sdk_mcp_server(name="test_runner", version="1.0.0", tools=build_tools(ctx))
