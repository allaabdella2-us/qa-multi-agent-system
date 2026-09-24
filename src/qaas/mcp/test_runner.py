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
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from qaas import importgraph
from qaas.mcp.context import ToolContext, err, ok

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 900

# §10 says flake rate is measured, not guessed, but a 200-run loop is a budget
# incident. Twenty runs already resolves a 5%-flaky test most of the time.
MAX_FLAKE_RUNS = 20

# And a wall clock over the whole investigation: 20 runs of the 900s per-run
# cap is five hours in one tool call, which nothing else in the system bounds.
MAX_FLAKE_TOTAL_S = 1_800

# How much raw output to hand back when parsing found nothing useful. Enough to
# diagnose a collection error, not enough to flood the agent's context.
OUTPUT_TAIL = 6_000

# pytest exit codes we treat specially (see pytest.ExitCode).
_EXIT_NO_TESTS = 5
_EXIT_USAGE_ERROR = 4

#: pytest's "the run never reached the test": 2 is an interrupted session (a
#: collection error), 3 an internal error, 4 a usage error -- which is also
#: what a `conftest.py` that does not import exits with. `_outcome_of` fell
#: back to "failed" for all three, so a passing test in a repository whose
#: conftest raised ImportError was reported as a failing one, and REPRODUCER
#: would have cited that as a reproduction.
_EXIT_DID_NOT_RUN = frozenset({2, 3, 4})

_EXIT_MEANING = {
    1: "tests failed",
    2: "interrupted, usually by a collection error",
    3: "pytest internal error",
    4: "usage error: a conftest.py that does not import, or a path or option pytest refused",
}

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
# A bare name that is not a nodeid ("test_does_not_exist") is reported as
# "file or directory not found" instead, which this did not match -- so it fell
# through to the exit-code fallback and came back "failed", a test failure for
# a test that does not exist.
_NOT_FOUND_RE = re.compile(r"^ERROR: (?:file or directory )?not found: ", re.MULTILINE)
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
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


#: `start_new_session` and `killpg` are POSIX. On Windows the child is killed
#: alone, which is what every platform got before.
_POSIX = os.name == "posix"

#: How long to wait for the pipes to close once the group has been killed.
_DRAIN_S = 5


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started.

    `subprocess.run(timeout=)` killed the child and nothing else, so a test that
    spawned a server, `npx` and the node it runs, pytest-xdist's workers, and
    each of `run_n_times`' up-to-twenty runs all kept running after the tool
    had reported a timeout -- still holding ports and the database the next
    run needed. The child is started in its own session, so its process group
    is exactly what it started.
    """
    if _POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(proc: subprocess.Popen, expired: subprocess.TimeoutExpired) -> tuple[str, str]:
    """Everything the killed group wrote, without waiting on a pipe forever.

    A second `communicate` returns the whole of both streams, including what
    the first one read before it timed out. It can only hang if something
    outside the group -- a process that called setsid itself -- still holds a
    pipe, so it is bounded too, and the partial output on the first exception
    is the answer then.
    """
    try:
        out, errout = proc.communicate(timeout=_DRAIN_S)
    except subprocess.TimeoutExpired:
        out, errout = expired.stdout, expired.stderr
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        try:
            proc.wait(timeout=_DRAIN_S)
        except subprocess.TimeoutExpired:
            pass
    return _decode(out), _decode(errout)


#: Environment variables a child process legitimately needs. Everything else is
#: dropped — see `_child_env`.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
        # Python's own behaviour, and the virtualenv the target runs in.
        "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
        "VIRTUAL_ENV", "CONDA_PREFIX", "PYENV_ROOT",
        # CI runners key a lot of behaviour off these.
        "CI", "GITHUB_ACTIONS", "SYSTEMROOT", "WINDIR", "APPDATA", "LOCALAPPDATA",
    }
)

#: Prefixes a target's own test suite is expected to read — its database URL,
#: its feature flags, whatever `env_control` set for it.
_ENV_ALLOWED_PREFIXES = ("QAAS_TARGET_", "PYTEST_DISABLE_")


def _child_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a target's own test suite runs in. An allowlist.

    It was `dict(os.environ, ...)` with two pytest keys removed, so the target
    repository's test suite — someone else's code, cloned from a URL seconds ago
    under `qaas run --repo` — executed with this user's `ANTHROPIC_API_KEY`,
    `JIRA_API_TOKEN`, `GITHUB_TOKEN` and every other credential `envfile.py`
    exports in scope. A `conftest.py` reading `os.environ` is all it takes, and
    running the target's tests is the *point* of this server, not an edge case.

    An allowlist rather than a denylist because the set of secrets a machine
    holds is open-ended and the set of variables a test suite needs is not.
    Anything a target genuinely requires is named in the profile and arrives
    through `env_extra` or the `QAAS_TARGET_` prefix.
    """
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}
    env.update(
        {k: v for k, v in os.environ.items() if k.startswith(_ENV_ALLOWED_PREFIXES)}
    )
    env.update(env_extra or {})
    # A wide terminal keeps pytest from wrapping nodeids across lines, which is
    # the one thing that would break the progress-line parser.
    env["COLUMNS"] = "250"
    # The parent process is usually pytest itself (this server is exercised from
    # a test suite); its addopts must not leak into the child's run.
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env


def _run(
    argv: list[str],
    cwd: Path,
    timeout_s: int,
    env_extra: dict[str, str] | None = None,
    *,
    on_start: Any = None,
) -> _Completed:
    """Run argv under a hard timeout, returning partial output if it expires.

    `subprocess.run` kills the child and hands the partial streams back on the
    exception, which is the difference between "the suite hung, here is how far
    it got" and an agent staring at nothing. It is `Popen` now so the kill can
    reach the whole process group (`_kill_group`); `_drain` keeps the partial
    streams.

    Decoded as UTF-8 with replacement. `text=True` decoded strictly, so one
    Latin-1 byte anywhere in a test's output raised UnicodeDecodeError out of
    `subprocess.run` and the tool raised instead of returning -- every row
    lost for one stray byte in a log line.
    """
    env = _child_env(env_extra)

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", start_new_session=_POSIX,
        )
    except OSError as exc:
        return _Completed(argv, -1, "", f"could not start {argv[0]}: {exc}", 0.0, False, started=False)
    if on_start is not None:
        on_start(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        stdout, stderr = _drain(proc, exc)
        return _Completed(argv, -1, stdout, stderr, time.monotonic() - started, True)
    return _Completed(
        argv, proc.returncode, stdout or "", stderr or "", time.monotonic() - started, False
    )


async def _run_async(argv: list[str], cwd: Path, timeout_s: int, env_extra: dict[str, str] | None = None) -> _Completed:
    """Off the event loop: a 300s suite must not block the other tools.

    The child has its own session now (see `_kill_group`), so a Ctrl-C at the
    terminal no longer reaches it, and a cancelled tool call -- the router's
    wall clock preempting an agent, or the operator stopping the run -- would
    leave it running while the worker thread waited out its whole timeout.
    Cancellation kills the group instead.
    """
    live: list[subprocess.Popen] = []
    try:
        return await asyncio.to_thread(
            _run, argv, cwd, timeout_s, env_extra, on_start=live.append
        )
    except asyncio.CancelledError:
        for proc in live:
            _kill_group(proc)
        raise


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


def _selector_refusal(ctx: ToolContext, cwd: Path, selector: str) -> str | None:
    """Why this selector may not reach pytest, if it may not.

    `cwd` was resolved and contained two lines above every call site; the
    selector beside it was not, and it is the argument that decides what runs.
    Two holes, both reachable from one tool call:

      * Selectors are appended to pytest's argv with no `--` separator, so one
        beginning with `-` is parsed as an *option*. `-p`, `-c`, `--rootdir=`
        and `-o addopts=...` each load code of the caller's choosing.
      * A path-shaped selector was passed to the collector unchecked, so
        `run_suite({"selector": "../outside"})` collected and **executed**
        modules outside the target root. Under `qaas run --repo <url>` the
        sibling of that root is `.qaas/targets/`, holding every other clone.

    Resolution happens before the containment test, so `..` and a symlink out of
    the checkout are caught by the same check — the reasoning `_resolve_cwd`
    already records.
    """
    if selector.startswith("-"):
        return (
            f"selector '{selector}' may not start with '-': pytest reads it as an "
            "option rather than a test to run. Name a path, a nodeid, or a -k "
            "expression without a leading dash."
        )
    # pytest expands `@file` into arguments read from that file -- its own
    # argsfile feature, and the same door as a leading dash one step removed:
    # `run_single("@args.txt")` loaded `-p no:terminal` out of the file and ran
    # a test outside the root, past every check below, because none of them
    # can see what the file says.
    if selector.startswith("@"):
        return (
            f"selector '{selector}' may not start with '@': pytest reads it as a "
            "file of extra arguments rather than a test to run. Name a path, a "
            "nodeid, or a -k expression."
        )
    root = ctx.target_root.resolve()
    resolved = _selector_head(cwd, selector)
    if resolved.exists() and not resolved.is_relative_to(root):
        return (
            f"selector '{selector}' resolves to {resolved}, outside the repository "
            f"({root}). Tests are run from inside the checkout, never beside it."
        )
    # A path-shaped selector that does not exist used to fall through to
    # `["-k", selector]`, because the caller below decides path-vs-keyword on
    # `head.exists()` alone. pytest then collected the *whole* suite and
    # filtered it by a keyword nobody meant as one. That is silent when the
    # suite is clean -- `_matched_nothing` catches it -- and actively
    # misleading when it is not: a suite with collection errors yields rows, so
    # the run is reported as a success, and the identical result comes back for
    # a real file and for a path that was never there. VERIFIER found this by
    # running a control path against a target whose suite had six collection
    # errors and getting the same "6 tests: 6 error" both times, which is the
    # one observation that makes a test run worthless as evidence.
    #
    # A `-k` expression is prose ("cancel and not slow"); a path has a
    # separator, a .py, or a nodeid's `::`. When it looks like a path and is
    # not one, say so rather than guessing a different meaning for it.
    if not resolved.exists() and _looks_like_a_path(selector):
        return (
            f"selector '{selector}' looks like a path but nothing exists at "
            f"{resolved}. If you meant a keyword filter, drop the path "
            "separator and the extension; if you meant a file, check the path "
            f"against what is in {cwd}."
        )
    return None


#: Test-file extensions across the runners this server knows about. A selector
#: ending in one of these was written as a file, whatever language the project
#: turns out to be in.
_TEST_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _looks_like_a_path(selector: str) -> bool:
    """Was this selector written as a file, rather than as a keyword filter?"""
    head = selector.split("::", 1)[0]
    return (
        "::" in selector
        or "/" in head
        or "\\" in head
        or head.endswith(_TEST_SUFFIXES)
    )


def _single_selectors(cwd: Path, test_id: str) -> list[str]:
    """One test's identity, in the argv shape its runner expects.

    A pytest nodeid is `path::test` and pytest takes it whole. vitest and jest
    have no nodeid: a file narrows the run and `-t` matches the full title, so
    the same string has to be taken apart to mean the same thing.
    """
    runner = _detect_runner(cwd)
    if runner not in _JS_RUNNERS:
        return [test_id]
    # One token, `--testNamePattern=<title>`, never `-t <title>`. The title is
    # the part after `::`, which `_selector_refusal` never sees the start of,
    # so `file.test.ts::--rootDir=..` became `-t --rootDir=..` and the runner
    # read the title as a flag of its own. Joined, it is the value of the
    # pattern whatever it starts with.
    file, _, title = test_id.partition("::")
    if title:
        return [file, f"--testNamePattern={title}"]
    return [f"--testNamePattern={test_id}"]


def _selector_head(cwd: Path, selector: str) -> Path:
    """The filesystem part of a selector ('tests/x.py::test_y' -> 'tests/x.py')."""
    head = Path(selector.split("::", 1)[0])
    return (head if head.is_absolute() else cwd / head).resolve()


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
# Which runner this project uses
# ---------------------------------------------------------------------------
#
# This server ran `python -m pytest` unconditionally. Pointed at a TypeScript
# repository with three vitest files it answered "No tests matched the default
# collection", and every tool that depends on running a test went with it:
# REPRODUCER cannot commit a failing test the system can execute, VERIFIER
# cannot verify a fix by running one, FIXER cannot check its own work.
# Discovery reads source and works anywhere; proving and verifying was Python
# only, which is the half that makes this more than a linter.
#
# vitest and jest both emit jest's JSON shape, so one parser serves both and
# adding a third runner is a detection entry plus an argv builder.

_JS_RUNNERS = ("vitest", "jest")

#: How each runner spells "filter by test name". pytest takes an expression,
#: the JS runners take a substring of the full title.
_KEYWORD_FLAG = {"pytest": "-k", "vitest": "-t", "jest": "-t"}

#: jest/vitest statuses -> the vocabulary the rest of this module speaks.
_JS_OUTCOME = {
    "passed": "passed", "failed": "failed", "pending": "skipped",
    "skipped": "skipped", "todo": "skipped", "disabled": "skipped",
}

_JS_NO_TESTS_RE = re.compile(
    r"no test (files |suites )?found|no tests found", re.IGNORECASE
)


def _detect_runner(cwd: Path) -> str:
    """Which test runner this project uses. pytest unless it says otherwise.

    Read from `package.json` rather than by globbing for `*.test.ts`, because a
    repository holding both languages should still be decided by what it
    declares. A project with no package.json is pytest, which keeps every
    existing Python target on exactly the path it was on.
    """
    manifest = cwd / "package.json"
    if not manifest.is_file():
        return "pytest"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "pytest"
    declared: set[str] = set()
    for section in ("devDependencies", "dependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            declared |= set(block)
    scripts = data.get("scripts")
    script_text = " ".join(
        v for v in (scripts or {}).values() if isinstance(v, str)
    ) if isinstance(scripts, dict) else ""
    for runner in _JS_RUNNERS:
        if runner in declared or runner in script_text:
            return runner
    return "pytest"


def _js_argv(runner: str, selectors: list[str], report_path: Path) -> list[str]:
    """`npx --no-install` so the project's own pinned runner is what runs.

    Without it npx will happily fetch a different major version from the
    network mid-run and report against that instead.
    """
    if runner == "vitest":
        argv = ["npx", "--no-install", "vitest", "run", "--reporter=json",
                f"--outputFile={report_path}"]
    else:
        argv = ["npx", "--no-install", "jest", "--ci", "--json",
                f"--outputFile={report_path}"]
    return argv + selectors


def _parse_js_report(path: Path, cwd: Path) -> list[dict[str, Any]] | None:
    """Per-test rows from jest's JSON shape, which vitest also emits."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = report.get("testResults")
    if not isinstance(files, list):
        return None

    rows: list[dict[str, Any]] = []
    for result in files:
        if not isinstance(result, dict):
            continue
        name = str(result.get("name") or "?")
        try:
            name = str(Path(name).relative_to(cwd))
        except ValueError:
            pass
        for case in result.get("assertionResults") or []:
            if not isinstance(case, dict):
                continue
            title = case.get("fullName") or case.get("title") or "?"
            failures = [m for m in (case.get("failureMessages") or []) if m]
            status = str(case.get("status", "unknown"))
            rows.append(
                {
                    # `file::title` so a JS nodeid reads like a pytest one and
                    # `_outcome_of`'s endswith match keeps working unchanged.
                    "nodeid": f"{name}::{title}",
                    "outcome": _JS_OUTCOME.get(status, status),
                    "duration_s": round((case.get("duration") or 0) / 1000, 4),
                    "message": _shorten("\n".join(failures)) if failures else None,
                }
            )
    return rows


async def _js_tests(
    runner: str, cwd: Path, selectors: list[str], timeout_s: int
) -> tuple[_Completed, list[dict[str, Any]], str]:
    with tempfile.TemporaryDirectory(prefix="qaas-js-") as tmp:
        report_path = Path(tmp) / "report.json"
        proc = await _run_async(_js_argv(runner, selectors, report_path), cwd, timeout_s)
        rows = _parse_js_report(report_path, cwd) or []
    return proc, rows, f"{runner}-json"


async def _run_tests(
    cwd: Path, selectors: list[str], timeout_s: int
) -> tuple[_Completed, list[dict[str, Any]], str]:
    """Run this project's suite, whichever runner it uses."""
    runner = _detect_runner(cwd)
    if runner in _JS_RUNNERS:
        return await _js_tests(runner, cwd, selectors, timeout_s)
    return await _pytest(cwd, selectors, timeout_s)


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
        report = json.loads(path.read_text(encoding="utf-8"))
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
    """Whether the selector picked no test at all, however the runner said so."""
    if proc.returncode == _EXIT_NO_TESTS:
        return True
    # vitest and jest both exit 1 and say so in prose; there is no distinct code.
    if proc.returncode != 0 and _JS_NO_TESTS_RE.search(proc.stdout + proc.stderr):
        return True
    return proc.returncode == _EXIT_USAGE_ERROR and bool(
        _NOT_FOUND_RE.search(proc.stdout + proc.stderr)
    )


def _never_ran(proc: _Completed, rows: list[dict[str, Any]], found: bool) -> bool:
    """Whether the run produced no result for what it was asked about.

    `found` is whether a result row answers the question -- the row for the
    one test `run_single` wanted, or any row at all for a suite. A timeout is
    not this: it has its own outcome and its partial results are real.

    Before this, both shapes came back as ordinary results. A `conftest.py`
    raising ImportError exits 4 with no rows, which `run_suite` reported as a
    successful "0 tests: none run" and `run_single` -- on a test that passes --
    as "failed", from the exit-code fallback. Neither is an outcome of the
    test; the suite never reached it.
    """
    if proc.timed_out or not proc.started:
        return False
    if not found and proc.returncode in _EXIT_DID_NOT_RUN:
        return True
    return not rows and proc.returncode != 0


def _never_ran_reason(proc: _Completed, what: str, note: str = "") -> str:
    """The `err()` text for a run that never reached `what`, with its output."""
    meaning = _EXIT_MEANING.get(proc.returncode, "no result was reported")
    return (
        f"{what} did not run: the runner exited {proc.returncode} ({meaning}) and "
        "reported no result for it. This is not a test outcome -- do not record it "
        "as a failure or a pass. Fix what the output below names, then run again."
        + (f" {note}" if note else "")
        + "\n\n"
        + (_tail(proc) or "(no output)")
    )


def _outcome_of(rows: list[dict[str, Any]], test_id: str, proc: _Completed) -> str:
    """One test's outcome, falling back to the exit code if parsing missed it.

    The fallback is for a row the terminal parser missed in a run that really
    happened. "failed" used to be what everything else fell through to, so a
    run that never reached the test -- a conftest that does not import, a
    usage error -- read as a test failure. That is "not_run" now, and every
    caller turns it into an `err()` before an agent sees it.
    """
    for row in rows:
        if row["nodeid"] == test_id or row["nodeid"].endswith(test_id):
            return row["outcome"]
    if proc.timed_out:
        return "timeout"
    if _matched_nothing(proc):
        return "not_collected"
    if _never_ran(proc, rows, found=False):
        return "not_run"
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
            refusal = _selector_refusal(ctx, cwd, selector)
            if refusal:
                return err(refusal)
            head = _selector_head(cwd, selector)
            selectors = (
                [selector] if head.exists()
                else [_KEYWORD_FLAG[_detect_runner(cwd)], selector]
            )

        proc, rows, parser = await _run_tests(cwd, selectors, timeout_s)
        if not proc.started:
            return err(proc.stderr)
        if _matched_nothing(proc) and not rows:
            return err(
                f"No tests matched {selector or 'the default collection'} in {cwd}. "
                "Check the selector against the files that exist."
            )
        if _never_ran(proc, rows, found=bool(rows)):
            return err(_never_ran_reason(proc, f"The suite in {cwd}"))

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
        refusal = _selector_refusal(ctx, cwd, test_id)
        if refusal:
            return err(refusal)

        proc, rows, parser = await _run_tests(cwd, _single_selectors(cwd, test_id), timeout_s)
        if not proc.started:
            return err(proc.stderr)
        if _matched_nothing(proc):
            return err(
                f"'{test_id}' matched no test in {cwd}. Nodeids look like "
                "'path/to/test_file.py::test_name'."
            )

        row = next((r for r in rows if r["nodeid"] == test_id or r["nodeid"].endswith(test_id)), None)
        # `_outcome_of` falls back to the exit code when no row matches, which is
        # right for pytest's terminal parser -- it can miss a row for a test that
        # really ran -- and wrong for the JS runners. `-t` matching nothing marks
        # every test skipped and exits 0, so a mistyped title came back "passed"
        # with no test behind it. The JSON reporter always writes a row per test,
        # so here a missing row means the id named nothing.
        if row is None and _detect_runner(cwd) in _JS_RUNNERS:
            titles = [r["nodeid"] for r in rows][:5]
            return err(
                f"'{test_id}' matched no test in {cwd}. This project runs "
                f"{_detect_runner(cwd)}, where an id is 'path/to/file.test.ts::"
                "full test title'."
                + (f" Titles in range: {titles}" if titles else "")
            )
        if _never_ran(proc, rows, found=row is not None):
            return err(_never_ran_reason(proc, f"'{test_id}'"))
        outcome = _outcome_of(rows, test_id, proc)
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
        refusal = _selector_refusal(ctx, cwd, test_id)
        if refusal:
            return err(refusal)
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

        # `n` runs of `timeout_s` each is 20 x 900s = five hours inside a single
        # tool call, which no budget or turn limit sees. The per-run timeout
        # bounds a hang; only an aggregate bounds the loop.
        deadline = asyncio.get_running_loop().time() + min(MAX_FLAKE_TOTAL_S, n * timeout_s)

        outcomes: list[str] = []
        messages: list[str] = []
        for attempt in range(n):
            remaining = deadline - asyncio.get_running_loop().time()
            if attempt and remaining <= 0:
                messages.append(
                    f"Stopped after {attempt} of {n} runs: the {MAX_FLAKE_TOTAL_S}s budget for "
                    "one flake investigation was reached. Judge the flake on these."
                )
                break
            proc, rows, _ = await _run_tests(
                cwd, _single_selectors(cwd, test_id), max(1, int(min(timeout_s, remaining)))
            )
            if not proc.started:
                return err(proc.stderr)
            if attempt == 0 and _matched_nothing(proc):
                return err(f"'{test_id}' matched no test in {cwd}.")
            row = next((r for r in rows if r["nodeid"] == test_id or r["nodeid"].endswith(test_id)), None)
            # The JS runners' `-t` matching nothing skips every test and exits
            # 0, which `run_single` already refuses to read as "passed" and
            # this loop still did -- twenty times, as "stable (passed)".
            if attempt == 0 and row is None and _detect_runner(cwd) in _JS_RUNNERS:
                return err(f"'{test_id}' matched no test in {cwd}.")
            # A run that never reached the test is not a sample of it. Counted,
            # a broken conftest came back "stable (failed)" over twenty runs;
            # mixed in, it would move the flake rate REPRODUCER's verdict
            # turns on. Either way the number is about the harness, not the
            # test, so the investigation stops and says why.
            if _never_ran(proc, rows, found=row is not None):
                return err(_never_ran_reason(
                    proc, f"'{test_id}' (run {attempt + 1} of {n})",
                    note=f"Runs before it: {outcomes}." if outcomes else "",
                ))
            outcome = _outcome_of(rows, test_id, proc)
            outcomes.append(outcome)
            if row and row.get("message"):
                messages.append(f"run {attempt + 1}: {row['message']}")

        counts = Counter(outcomes)
        majority, majority_count = counts.most_common(1)[0]
        # `total`, not `n`. The loop above breaks early when the aggregate flake
        # budget expires, and every statistic divided by the number of runs
        # *requested* rather than the number that actually happened. Stopping at
        # 3 of 20 with all three passing reported a 85% flake rate on a test
        # that never once disagreed with itself -- and flake rate is what
        # REPRODUCER's verdict turns on, so the number being wrong in the
        # alarming direction is the bad half of the trade.
        total = len(outcomes)
        flake_rate = round((total - majority_count) / total, 4) if total else 0.0
        passed = counts.get("passed", 0)

        verdict = (
            f"stable ({majority})" if flake_rate == 0
            else f"FLAKY: {flake_rate:.0%} of runs disagreed with the majority ({majority})"
        )
        shortfall = f" (of {n} requested)" if total != n else ""
        return ok(
            f"{test_id} over {total} runs{shortfall} — {passed} passed, "
            f"{total - passed} not passed. {verdict}.",
            runs=total,
            requested=n,
            passed=passed,
            failed=total - passed,
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

        paths = [str(p) for p in raw_paths]
        candidates = await asyncio.to_thread(_collect_test_files, cwd)
        if not candidates:
            return err(f"No test files found under {cwd}.")

        # Derived first, guessed second. The graph answers the question the
        # heuristic structurally cannot: a test that reaches the changed module
        # through a caller. That is step 3 of `regression-suite-selection` --
        # "a fix inside a shared helper breaks its consumers, not itself" -- and
        # `_score_tests` scores it zero, because it compares filenames and the
        # consumer's filename has nothing to do with the changed one.
        derived, unreadable, truncated = await asyncio.to_thread(_graph_tests, cwd, paths)
        # What the graph could not read travels with both answers. "Nothing
        # imports the changed file" and "I cannot read this language" are
        # different facts and the second one is only knowable here.
        # So does where it stopped reading: past `importgraph.MAX_MODULES` the
        # graph is partial, and that flag was never read, so a partial ranking
        # -- or a fallback forced by a changed file the graph never reached --
        # was presented as the complete answer.
        tail = "".join(
            f"\n{note}" for note in (unreadable, _truncation_note(truncated)) if note
        )
        if derived:
            return ok(
                "Covering tests by import graph, nearest first: "
                + ", ".join(item["test_file"] for item in derived[:10])
                + ".\nDistance is import hops from the changed file: 0 is the file "
                "itself, 1 imports it directly, 2 reaches it through one more "
                "module. This is derived from the source, not guessed from "
                "filenames — but it is an *import* graph, so a test that exercises "
                "the code through a fixture, a plugin or an HTTP call does not "
                "appear here. Run the full suite before concluding nothing broke."
                + tail,
                affected=derived[:25],
                heuristic=False,
                method="import-graph",
                unreadable=unreadable,
                truncated=truncated,
            )

        scored = await asyncio.to_thread(_score_tests, cwd, paths, candidates)
        if not scored:
            return ok(
                "No test file looks related to those paths. That is itself worth reporting: "
                "the change may be untested." + tail,
                affected=[],
                heuristic=True,
                method="filename-heuristic",
                unreadable=unreadable,
                truncated=truncated,
            )
        return ok(
            "Likely covering tests, best first: "
            + ", ".join(item["test_file"] for item in scored[:10])
            + ".\nThis is the filename heuristic, not the import graph: either the "
            "changed paths are not Python, TypeScript or JavaScript, or nothing in "
            "this repository imports them. Treat the ranking as a place to start, "
            "and run the full suite before concluding nothing broke." + tail,
            affected=scored[:25],
            heuristic=True,
            method="filename-heuristic",
            searched=len(candidates),
            unreadable=unreadable,
            truncated=truncated,
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
            refusal = _selector_refusal(ctx, cwd, selector)
            if refusal:
                return err(refusal)
            head = _selector_head(cwd, selector)
            selectors = (
                [selector] if head.exists()
                else [_KEYWORD_FLAG[_detect_runner(cwd)], selector]
            )

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
                data = json.loads(json_file.read_text(encoding="utf-8"))
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

def _collect_test_files(root: Path) -> list[Path]:
    """Every file the project's runner would collect, in any language it uses.

    Two things were wrong with the `rglob("*.py")` this replaces. It was Python
    only, so on a vitest project `affected_tests` answered "No test files found"
    before it ranked anything -- and `rglob` *filters* after descending, so it
    read its way through `node_modules` to do it. The graph's walker prunes.
    """
    return [p for p in importgraph.source_files(root) if importgraph.is_test_file(p)]


def _truncation_note(truncated: bool) -> str | None:
    """One sentence saying the graph stopped at its cap, or None."""
    if not truncated:
        return None
    return (
        f"The import graph stopped at {importgraph.MAX_MODULES} source files, so it "
        "is incomplete: a test outside what it read is missing from this answer, "
        "not unaffected. Run the full suite before concluding nothing broke."
    )


def _graph_tests(root: Path, changed: list[str]) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Covering tests from the import graph, plus what the graph could not read.

    Empty is the honest answer in three cases and the caller must fall back in
    all of them: the target is in a language this cannot parse, the changed
    paths are, or nothing in the repository imports them. Distinguishing "no
    tests are affected" from "I cannot see this language" matters enough that
    the tool reports which method produced the ranking *and* which languages
    were walked past -- the second half is why this returns a note rather than
    only rows.

    Never raises -- the fallback exists precisely so a ranking failure is never
    a verification failure.

    The note also says when the graph was *truncated*. `importgraph` stops at
    `MAX_MODULES` and records it -- "reported rather than silently applied",
    its own comment says -- and nothing here read the flag, so on a repository
    past the cap a partial ranking was presented as the complete one.
    """
    try:
        graph = importgraph.build(root)
        note = graph.unreadable_note
        truncated = bool(getattr(graph, "truncated", False))
        if not graph:
            return [], note, truncated
        rows = graph.affected_tests(changed)
    except Exception:  # noqa: BLE001 — a ranking is never worth failing a phase for
        return [], None, False
    return [
        {
            "test_file": r["test_file"],
            "distance": r["distance"],
            "why": (
                "the changed file itself" if r["distance"] == 0
                else "imports it directly" if r["distance"] == 1
                else f"reaches it through {r['distance'] - 1} more module(s)"
            ),
        }
        for r in rows
    ], note, truncated


def _conventional_names(stem: str) -> set[str]:
    """What a test of `<stem>` is called, per the conventions people follow.

    pytest's is `test_x.py`/`x_test.py`; vitest's and jest's is `x.test.ts` and
    `x.spec.tsx`. Naming the second set matters because the heuristic is the
    *fallback*, and the fallback is what a JS target gets whenever the graph
    cannot reach the change.
    """
    return {f"test_{stem}.py", f"{stem}_test.py"} | {
        f"{stem}.{kind}{ext}"
        for kind in ("test", "spec")
        for ext in importgraph.JS_SUFFIXES
    }


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
    # Read each candidate once rather than once per changed path: the inner loop
    # used to call `read_text` inside `for raw in changed`, so ten changed files
    # against a thousand tests was ten thousand file reads for a ranking.
    texts: dict[Path, str] = {}
    for candidate in candidates:
        try:
            texts[candidate] = candidate.read_text(errors="ignore")
        except OSError:
            texts[candidate] = ""

    for raw in changed:
        source = Path(raw)
        stem = source.stem
        module_dir = source.parent.as_posix()
        for candidate in candidates:
            score = 0
            why: list[str] = []
            name = candidate.name
            if name in _conventional_names(stem):
                score += 100
                why.append(f"name matches {source.name}")
            elif stem and stem in name:
                score += 25
                why.append(f"filename mentions '{stem}'")

            if stem:
                if re.search(
                    rf"\b(import|from)\b[^\n]*\b{re.escape(stem)}\b", texts[candidate]
                ):
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
