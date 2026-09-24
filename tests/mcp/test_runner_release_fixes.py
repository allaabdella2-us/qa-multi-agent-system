"""test_runner regressions found before 0.0.2: outcomes, process groups, argv.

Each test here is a bug that was reproduced against the shipped server. The
suites under test are throwaway packages in tmp_path, so the assertions are
about what the tool tells an agent, not about this repository's own suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from conftest import is_error, structured, text_of

from support import CONFIG_SEARCH

from qaas.config import load_config
from qaas.mcp import test_runner as tr
from qaas.mcp.context import ToolContext, handlers
from qaas.store import RunStore, SystemMapStore

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _tools_for(root: Path, store_root: Path) -> dict:
    config = load_config(search=CONFIG_SEARCH)
    return handlers(
        tr.build_tools(
            ToolContext(
                store=RunStore("runner-fixes", root=store_root),
                maps=SystemMapStore(store_root),
                config=config,
                agent=config.agents["REPRODUCER"],
                target_root=root,
            )
        )
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n")
    return root


@pytest.fixture
def tools(project: Path, tmp_path: Path) -> dict:
    return _tools_for(project, tmp_path / ".qaas")


@pytest.fixture
def broken_conftest(project: Path) -> Path:
    """A repository whose conftest.py does not import. pytest exits 4 with no rows."""
    (project / "conftest.py").write_text("import no_such_module_for_qaas_tests\n")
    return project


def _gone(pid: int, within_s: float = 10.0) -> bool:
    """Whether `pid` has exited. A killed orphan is a zombie until init reaps it,
    and `kill(pid, 0)` succeeds on a zombie, so this polls."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def _wait_for(path: Path, within_s: float = 30.0) -> str:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


# -- 1. an outcome the run never produced is not an outcome ------------------


async def test_a_name_that_is_not_a_test_is_an_error_not_a_failure(tools):
    """pytest says "file or directory not found" and exits 4. The not-found
    pattern only knew "not found:", so this fell through to the exit-code
    fallback and a test that does not exist was reported "failed"."""
    result = await tools["run_single"]({"test_id": "test_does_not_exist"})
    assert is_error(result), text_of(result)
    assert "matched no test" in text_of(result)


async def test_a_broken_conftest_is_not_an_empty_successful_suite(tools, broken_conftest):
    """Exit 4, no rows: this came back as a *success* reading "0 tests: none run"."""
    result = await tools["run_suite"]({})
    assert is_error(result), text_of(result)
    text = text_of(result)
    assert "did not run" in text and "exited 4" in text
    assert "no_such_module_for_qaas_tests" in text, "the output tail must say why"


async def test_a_passing_test_behind_a_broken_conftest_is_not_failed(tools, broken_conftest):
    """The exit-code fallback read exit 4 as "failed", so REPRODUCER would have
    cited a passing test as a reproduction."""
    result = await tools["run_single"]({"test_id": "tests/test_a.py::test_ok"})
    assert is_error(result), f"reported as an outcome: {structured(result)}"
    assert "no_such_module_for_qaas_tests" in text_of(result)


async def test_run_n_times_does_not_count_runs_that_never_happened(tools, broken_conftest):
    """Twenty exit-4 runs read as "stable (failed)" -- a flake verdict about the
    harness rather than the test."""
    result = await tools["run_n_times"]({"test_id": "tests/test_a.py::test_ok", "n": 3})
    assert is_error(result), text_of(result)
    assert "run 1 of 3" in text_of(result)


def test_the_exit_code_fallback_never_calls_a_usage_error_a_failure():
    for code in (2, 3, 4):
        proc = tr._Completed(["pytest"], code, "", "boom", 0.1, False)
        assert tr._outcome_of([], "tests/test_a.py::test_ok", proc) == "not_run", code
    # Rows that exist but do not name the test -- a parametrised id, say -- in a
    # run that did happen keep the exit-code fallback it always had.
    ran = tr._Completed(["pytest"], 1, "", "", 0.1, False)
    rows = [{"nodeid": "tests/test_a.py::test_p[1]", "outcome": "failed"}]
    assert tr._outcome_of(rows, "tests/test_a.py::test_p", ran) == "failed"
    # And a timeout is still its own answer, not an error.
    slow = tr._Completed(["pytest"], -1, "", "", 9.0, True)
    assert tr._outcome_of([], "tests/test_a.py::test_ok", slow) == "timeout"


async def test_a_js_title_that_matches_nothing_is_not_twenty_passes(tmp_path, monkeypatch):
    """`-t` matching nothing skips every test and exits 0. `run_single` refused
    to call that "passed"; `run_n_times` still did, every run."""
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}))
    (root / "a.test.ts").write_text("")

    async def fake_run_tests(cwd, selectors, timeout_s):
        proc = tr._Completed(["npx"], 0, "", "", 0.1, False)
        return proc, [{"nodeid": "a.test.ts::something else", "outcome": "skipped"}], "vitest-json"

    monkeypatch.setattr(tr, "_run_tests", fake_run_tests)
    tools = _tools_for(root, tmp_path / ".qaas")
    result = await tools["run_n_times"]({"test_id": "a.test.ts::typo in the title", "n": 3})
    assert is_error(result), text_of(result)


# -- 2. a timeout kills what the test started, not only pytest ---------------


@POSIX_ONLY
async def test_a_timeout_kills_the_processes_a_test_started(tools, project, tmp_path):
    """`subprocess.run(timeout=)` killed pytest and nothing else. A test that
    spawned a server kept it running after the tool reported the timeout."""
    pidfile = tmp_path / "grandchild.pid"
    (project / "tests" / "test_spawns.py").write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n\n"
        "def test_spawns():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"    Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "    time.sleep(120)\n"
    )
    result = await tools["run_suite"]({"selector": "tests/test_spawns.py", "timeout_s": 3})
    assert structured(result)["timed_out"] is True
    grandchild = int(_wait_for(pidfile))
    assert _gone(grandchild), f"pid {grandchild} outlived the timeout that killed its test"


@POSIX_ONLY
async def test_a_cancelled_run_kills_its_process_group(tmp_path):
    """The child has its own session now, so a Ctrl-C at the terminal no longer
    reaches it. A cancelled tool call has to do that job instead, or the worker
    thread waits out the whole timeout with the suite still running."""
    pidfile = tmp_path / "child.pid"
    script = (
        "import os, time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(120)"
    )
    task = asyncio.create_task(tr._run_async([sys.executable, "-c", script], tmp_path, 120))
    pid = int(await asyncio.to_thread(_wait_for, pidfile))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(_gone, pid)


# -- 3. one stray byte does not cost the whole result ------------------------


def test_output_that_is_not_utf8_is_replaced_rather_than_raised(tmp_path):
    """`text=True` decoded strictly: one Latin-1 byte raised UnicodeDecodeError
    out of the run and every row went with it."""
    proc = tr._run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9 ok\\n')"],
        tmp_path, 30,
    )
    assert proc.returncode == 0
    assert "caf\ufffd ok" in proc.stdout


# -- 5 and 6. argv the agent supplies is never a flag -----------------------


@pytest.mark.parametrize("tool_name", ["run_single", "run_n_times", "run_suite"])
async def test_a_pytest_argsfile_is_refused(tools, project, tmp_path, tool_name):
    """pytest expands `@file` into arguments. `run_single("@args.txt")` loaded
    `-p no:terminal` from the file and ran a test outside the root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = tmp_path / "ran-outside"
    (outside / "test_evil.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "def test_evil():\n    assert True\n"
    )
    (project / "args.txt").write_text(f"-p\nno:terminal\n{outside / 'test_evil.py'}\n")

    args = {"selector": "@args.txt"} if tool_name == "run_suite" else {"test_id": "@args.txt"}
    if tool_name == "run_n_times":
        args["n"] = 1
    result = await tools[tool_name](args)
    assert is_error(result)
    assert "may not start with '@'" in text_of(result)
    assert not marker.exists()


def test_a_js_title_is_a_value_never_a_flag(tmp_path):
    """`file.test.ts::--rootDir=..` became `-t --rootDir=..`: the title after
    `::` is never seen by the leading-dash check, and the runner read it as
    its own option."""
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"devDependencies": {"jest": "^29"}}))

    argv = tr._single_selectors(root, "src/a.test.ts::--rootDir=..")
    assert argv == ["src/a.test.ts", "--testNamePattern=--rootDir=.."]
    assert "--rootDir=.." not in argv and "-t" not in argv


# -- 8. a truncated graph says so -------------------------------------------


async def test_a_truncated_import_graph_is_not_presented_as_complete(tools, project, monkeypatch):
    """`importgraph` stops at MAX_MODULES and sets `truncated`; nothing read it,
    so on a repository past the cap a partial ranking read as the whole one."""
    (project / "pkg").mkdir()
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "calc.py").write_text("def f():\n    return 1\n")
    (project / "tests" / "test_calc.py").write_text("from pkg.calc import f\n\ndef test_f():\n    assert f()\n")

    real_build = tr.importgraph.build
    monkeypatch.setattr(tr.importgraph, "build", lambda root: real_build(root, max_modules=2))

    result = await tools["affected_tests"]({"paths": ["pkg/calc.py"]})
    assert not is_error(result), text_of(result)
    assert structured(result)["truncated"] is True
    assert f"stopped at {tr.importgraph.MAX_MODULES}" in text_of(result)


async def test_a_complete_import_graph_carries_no_truncation_note(tools, project):
    result = await tools["affected_tests"]({"paths": ["tests/test_a.py"]})
    assert structured(result)["truncated"] is False
    assert "stopped at" not in text_of(result)
