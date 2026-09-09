"""Phase D: the run ledger is legible.

The ledger recorded 28 kinds of entry and `qaas show` read exactly one of them
(`denial`), printing no cost, no mode, no duration and no verdicts. These tests
pin the readers -- `qaas trace`, the enriched `qaas show`, the closed `LedgerKind`
set -- and the two provenance gaps that made a run unreplayable: the task an
agent was actually given, and the commit it was given it against.

Everything here is offline: a synthetic run store, a scripted conductor, no
network and no API call.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from support import CONFIG_SEARCH

from qaas import cli
from qaas import conductor as conductor_mod
from qaas import trace as trace_mod
from qaas.conductor import Conductor, target_revision
from qaas.config import load_config
from qaas.envelope import DefectEnvelope, Domain, Severity
from qaas.mcp.context import ToolContext
from qaas.runner import RunOutcome, _record_task
from qaas.store import AgentResult, LedgerEntry, LedgerKind, RunStore

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def run(tmp_path) -> RunStore:
    """A synthetic run whose ledger carries one of every kind that matters.

    Shaped after a real `fix-cycle` ledger: discovery emits envelopes, FORGE
    reproduces, CLERK files, PROOF verdicts twice on one ticket, ARBITER
    escalates. Details are kept short on purpose -- rich wraps CLI output at 80
    columns and an assertion on a long string would be an assertion about
    terminal width.
    """
    store = RunStore("run-synthetic-0001", tmp_path)
    store.log(
        "run_started", mode="fix-cycle", agents=["CONDUIT", "FORGE", "CLERK", "PROOF"],
        budget_usd=10.0, wall_clock_s=3600,
        target_root="/tmp/app", target_sha="abc123def4567890", target_dirty=False,
    )
    store.log("agent_started", agent="CONDUIT", model="claude-opus-5",
              task_chars=42, task_preview="Exercise the API.", task_uri="artifact://x/task-CONDUIT-01.md")
    for tool in ("Read", "Read", "Grep"):
        store.log("tool_call", agent="CONDUIT", tool=tool, allowed=True)
    store.log("denial", agent="CONDUIT", tool="Bash", reason="not allowlisted")
    store.log("tool_call", agent="CONDUIT", tool="Read", allowed=True)
    store.put_envelope(_envelope(store.run_id, "CONDUIT"))
    store.put_result(AgentResult(agent="CONDUIT", cost_usd=1.25, num_turns=9))

    store.log("agent_started", agent="FORGE", model="claude-opus-5", task_chars=7)
    store.log("reproduction", agent="FORGE", envelope_id="e1", status="reproduced", fileable=True)
    store.log("vcs", agent="FORGE", action="commit", branch="qa/repro/e1", sha="deadbee")
    store.put_result(AgentResult(agent="FORGE", cost_usd=0.75, num_turns=4))

    store.log("ticket", agent="CLERK", action="created", key="CORVID-1", severity="major")
    store.log("ticket", agent="CLERK", action="created", key="CORVID-2", severity="minor")
    store.log("verdict", agent="PROOF", ticket_key="CORVID-1", verdict="NOT_FIXED", observed="still broken")
    store.log("reopened", agent="PROOF", ticket_key="CORVID-1", attempt=1)
    store.log("verdict", agent="PROOF", ticket_key="CORVID-1", verdict="VERIFIED", observed="passes")
    store.log("verified", agent="PROOF", ticket_key="CORVID-1", reopens=1)
    store.log("escalation", agent="ARBITER", reason="CORVID-2 needs a human")
    store.log("run_finished", run_id=store.run_id, mode="fix-cycle", agents_run=2,
              failed=[], cost_usd=2.0, escalations=["CORVID-2 needs a human"], stopped_early=None)
    return store


def _envelope(run_id: str, agent: str) -> DefectEnvelope:
    return DefectEnvelope(
        run_id=run_id, discovered_by=agent, domain=Domain.API, **{"class": "bug"},
        title="A finding", summary="Something is wrong.",
        severity=Severity.MAJOR, confidence=0.9,
    )


def _trace(runner, store: RunStore, *args) -> str:
    result = runner.invoke(cli.app, ["trace", store.run_id, "--root", str(store.root), *args])
    assert result.exit_code == 0, result.output
    return result.output


# -- the timeline -----------------------------------------------------------


def test_the_timeline_shows_every_major_kind(runner, run):
    output = _trace(runner, run)
    for kind in ("run_started", "agent_started", "tool_call", "denial", "envelope",
                 "reproduction", "vcs", "ticket", "verdict", "reopened", "verified",
                 "escalation", "agent_finished", "run_finished"):
        assert kind in output, f"{kind} is missing from the timeline"


def test_the_timeline_carries_the_facts_not_just_the_kinds(runner, run):
    output = _trace(runner, run)
    assert "not allowlisted" in output, "a denial without its reason is not an audit trail"
    assert "NOT_FIXED" in output and "VERIFIED" in output
    assert "CORVID-1" in output


def test_cost_accumulates_through_the_run(runner, run):
    output = _trace(runner, run)
    assert "$1.25" in output, "CONDUIT's cost"
    assert "$2.00" in output, "the running total after FORGE, not FORGE's $0.75"


def test_consecutive_tool_calls_fold_into_one_row(run):
    """2400 tool calls against 150 of everything else buries the run."""
    rows = trace_mod.timeline(trace_mod.read_ledger(run))
    folded = [r for r in rows if r.kind == "tool_call"]
    assert [r.count for r in folded] == [3, 1], "the denial between them must split the run"
    assert "Read×2" in folded[0].detail and "Grep×1" in folded[0].detail


def test_json_is_faithful_and_never_folds(run):
    """An export is parsed, not read: it keeps every entry."""
    entries = trace_mod.read_ledger(run)
    rows = trace_mod.timeline(entries, fold_tool_calls=False)
    assert len(rows) == len(entries)


# -- filters ----------------------------------------------------------------


def test_agent_filter_keeps_only_that_agent(runner, run):
    output = _trace(runner, run, "--agent", "PROOF")
    assert "verdict" in output and "reopened" in output
    assert "CONDUIT" not in output
    assert "run_started" not in output, "an unattributed entry is not PROOF's"


def test_agent_filter_is_case_insensitive(run):
    entries = trace_mod.read_ledger(run)
    assert trace_mod.select(entries, agent="proof") == trace_mod.select(entries, agent="PROOF")


def test_kind_filter_is_repeatable(runner, run):
    output = _trace(runner, run, "--kind", "verdict", "--kind", "escalation")
    assert "verdict" in output and "escalation" in output
    assert "tool_call" not in output and "envelope" not in output


def test_filters_compose(run):
    entries = trace_mod.read_ledger(run)
    picked = trace_mod.select(entries, agent="CLERK", kinds=[LedgerKind.TICKET])
    assert [e.detail["key"] for e in picked] == ["CORVID-1", "CORVID-2"]


def test_an_unknown_kind_filter_says_so_instead_of_printing_nothing(runner, run):
    result = runner.invoke(cli.app, ["trace", run.run_id, "--root", str(run.root), "--kind", "verdicts"])
    assert result.exit_code == 2
    assert "unknown ledger kind" in result.output
    assert "verdict" in result.output, "the legal set is part of the message"


def test_trace_on_a_run_that_does_not_exist_fails_loudly(runner, tmp_path):
    result = runner.invoke(cli.app, ["trace", "run-nope", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "no ledger" in result.output


# -- --json -----------------------------------------------------------------


def test_json_output_parses(runner, run):
    output = _trace(runner, run, "--json")
    entries = json.loads(output)
    assert len(entries) == len(trace_mod.read_ledger(run))
    assert {e["kind"] for e in entries} >= {"run_started", "verdict", "run_finished"}
    assert all("at" in e and "detail" in e for e in entries)


def test_json_output_honours_the_filters(runner, run):
    entries = json.loads(_trace(runner, run, "--json", "--kind", "verdict"))
    assert [e["detail"]["verdict"] for e in entries] == ["NOT_FIXED", "VERIFIED"]


def test_json_output_survives_a_detail_longer_than_the_console(runner, tmp_path):
    """rich soft-wraps at 80 columns; JSON wrapped mid-string does not parse."""
    store = RunStore("run-wide-0001", tmp_path)
    store.log("denial", agent="FORGE", tool="Bash", reason="x" * 4000)
    result = runner.invoke(cli.app, ["trace", store.run_id, "--root", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["detail"]["reason"] == "x" * 4000


# -- qaas show --------------------------------------------------------------


def test_show_reports_cost_mode_duration_and_escalations(runner, run):
    result = runner.invoke(cli.app, ["show", run.run_id, "--root", str(run.root)])
    assert result.exit_code == 0, result.output
    assert "fix-cycle" in result.output
    assert "$2.00" in result.output, "cost, which show never printed"
    assert "duration" in result.output
    assert "escalations" in result.output and "CORVID-2 needs a human" in result.output


def test_show_reports_each_ticket_and_its_verdict(runner, run):
    result = runner.invoke(cli.app, ["show", run.run_id, "--root", str(run.root)])
    assert "CORVID-1" in result.output and "VERIFIED" in result.output
    assert "no verdict" in result.output, "CORVID-2 was filed and never verified"


def test_show_reports_the_target_commit(runner, run):
    result = runner.invoke(cli.app, ["show", run.run_id, "--root", str(run.root)])
    assert "abc123def456" in result.output


def test_show_still_lists_findings_and_denials(runner, run):
    """The two things it did before must not have been traded away."""
    result = runner.invoke(cli.app, ["show", run.run_id, "--root", str(run.root)])
    assert "A finding" in result.output
    assert "not allowlisted" in result.output


def test_show_flags_a_run_that_never_finished(runner, tmp_path):
    store = RunStore("run-killed-0001", tmp_path)
    store.log("run_started", mode="nightly", agents=["CONDUIT"], budget_usd=1.0)
    result = runner.invoke(cli.app, ["show", store.run_id, "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "did not complete" in result.output


# -- the kind enum ----------------------------------------------------------


def test_an_unknown_kind_is_rejected_on_write(tmp_path):
    store = RunStore("run-typo-0001", tmp_path)
    with pytest.raises(ValidationError):
        store.log("denail", agent="FORGE", reason="typo")
    assert not list(store.ledger()), "nothing may reach the file"


def test_an_unknown_kind_is_rejected_on_read():
    with pytest.raises(ValidationError):
        LedgerEntry.model_validate_json('{"kind":"invented","agent":null,"detail":{}}')


def test_a_kind_still_behaves_as_its_string(tmp_path):
    """~60 call sites pass a literal and the conductor compares against strings."""
    store = RunStore("run-str-0001", tmp_path)
    entry = store.log("denial", agent="FORGE", reason="no")
    assert entry.kind == "denial"
    assert f"{entry.kind}" == "denial"
    assert '"kind":"denial"' in store.ledger_path.read_text()
    assert [e.kind for e in store.ledger("denial")] == ["denial"]


def test_every_kind_written_anywhere_in_the_source_is_a_member():
    """A `store.log("...")` naming a kind the enum lacks would raise at runtime."""
    import ast

    known = {k.value for k in LedgerKind}
    offenders = []
    for path in sorted((REPO / "src" / "qaas").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value not in known
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.args[0].value!r}")
    assert not offenders, f"ledger kinds with no LedgerKind member: {offenders}"


def test_the_wire_format_did_not_change(run):
    """StrEnum keeps the wire format, so ledgers written before this still read."""
    raw = run.ledger_path.read_text().splitlines()
    assert [json.loads(line)["kind"] for line in raw] == [str(e.kind) for e in run.ledger()]


# -- provenance: the task an agent was given --------------------------------


def _context(tmp_path, cfg, name="FORGE") -> ToolContext:
    from qaas.store import SystemMapStore

    store = RunStore("run-task-0001", tmp_path)
    return ToolContext(store=store, maps=SystemMapStore(tmp_path), config=cfg,
                       agent=cfg.agents[name], target_root=REPO)


def test_the_task_an_agent_received_is_recoverable(tmp_path):
    """`task_chars=len(task)` recorded the length and threw away the prompt."""
    cfg = load_config(search=CONFIG_SEARCH)
    ctx = _context(tmp_path, cfg)
    task = "Reproduce finding e1.\n" + "detail " * 500
    detail = _record_task(ctx, "FORGE", task)

    assert detail["task_preview"].startswith("Reproduce finding e1.")
    assert ctx.store.resolve_artifact(detail["task_uri"]).read_text() == task


def test_each_invocation_of_a_repeated_agent_keeps_its_own_task(tmp_path):
    """FORGE runs once per finding; a per-agent filename would keep only the last."""
    cfg = load_config(search=CONFIG_SEARCH)
    ctx = _context(tmp_path, cfg)
    first = _record_task(ctx, "FORGE", "finding one")
    second = _record_task(ctx, "FORGE", "finding two")

    assert first["task_uri"] != second["task_uri"]
    assert ctx.store.resolve_artifact(first["task_uri"]).read_text() == "finding one"
    assert ctx.store.resolve_artifact(second["task_uri"]).read_text() == "finding two"


# -- provenance: the commit the run examined --------------------------------


def test_target_revision_reads_a_real_checkout(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "first")

    info = target_revision(repo)
    assert info["target_sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert info["target_dirty"] is False

    (repo / "a.txt").write_text("two")
    assert target_revision(repo)["target_dirty"] is True, "a dirty tree is not pinned by its sha"


def test_target_revision_survives_a_directory_that_is_not_a_repo(tmp_path):
    """`environment.mode: none` targets and plain directories are supported."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    info = target_revision(plain)
    assert info == {"target_root": str(plain), "target_sha": None, "target_dirty": None}
    assert target_revision(None)["target_sha"] is None


def test_run_started_pins_the_run_to_a_commit(tmp_path, monkeypatch):
    """Without this, nothing said which code the findings were found in."""
    async def fake_run_agent(spec, ctx, task, *, options=None, max_budget_usd=None, on_event=None):
        return RunOutcome(result=AgentResult(agent=spec.name, cost_usd=0.0))

    monkeypatch.setattr(conductor_mod, "run_agent", fake_run_agent)
    cfg = load_config(search=CONFIG_SEARCH)
    report = asyncio.run(Conductor(cfg, target_root=REPO, root=tmp_path).run("pr-check"))

    started = next(iter(RunStore(report.run_id, tmp_path).ledger("run_started")))
    # `cfg.target_app` is gone: the target root is the profile's, resolved once.
    assert Path(started.detail["target_root"]) == Path(REPO)
    assert started.detail["target_sha"] == _git(REPO, "rev-parse", "HEAD").strip()
    assert started.detail["target_dirty"] in (True, False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True).stdout
