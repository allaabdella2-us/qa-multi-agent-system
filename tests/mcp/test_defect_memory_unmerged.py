"""A fix verified on a branch nobody has merged is not a regression when it recurs.

VERIFIED means a fix works *on its branch*; §8.4 leaves the merge to a human.
Until then the main branch still has the defect, and the next run against it
finds it again. Memory recorded the VERIFIED as "resolved", so that sighting
was announced as "REGRESSION of QAAS-55" and filed as a new ticket linked to one
whose fix had never left `fix/QAAS-55-orders-limit` -- in the first run of the
whole roster after QAAS-55 was verified.

Merged is read the ways a merge button offers -- merge commit, rebase, squash --
and a deleted branch is "cannot tell", which stays a regression: a false
"not merged" would suppress a real one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import make_envelope, text_of

from qaas.adapters.vcs import LocalGit
from qaas.mcp import defect_memory
from qaas.mcp.context import handlers
from qaas.mcp.defect_memory import build_tools

REPORT = dict(
    title="Order list ignores the page size parameter",
    summary="GET /v1/orders returns every row regardless of the limit query parameter.",
    location={"endpoint": "GET /v1/orders", "paths": ["api/app/orders.py"]},
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """main with the defect; `fix/X` with a failing test and then the fix."""
    root = tmp_path / "app"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "orders.py").write_text("LIMIT = None\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "fix/X")
    (root / "test_orders.py").write_text("def test_limit(): ...\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "failing test")
    (root / "orders.py").write_text("LIMIT = 50\n")
    _git(root, "commit", "-qam", "the fix")
    _git(root, "checkout", "-q", "main")
    return root


def _fix(repo: Path) -> str:
    return _git(repo, "rev-parse", "fix/X")


# -- LocalGit.landed --------------------------------------------------------------


def test_an_unmerged_fix_has_not_landed(repo):
    assert LocalGit(repo).landed(_fix(repo), "fix/X") is False


@pytest.mark.parametrize("how", ["merge", "fast-forward", "rebase", "squash"])
def test_every_way_of_merging_counts_as_landed(repo, how):
    fix = _fix(repo)
    if how == "merge":
        (repo / "other.py").write_text("x = 1\n")  # so main has moved on
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "unrelated")
        _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "fix/X")
    elif how == "fast-forward":
        _git(repo, "merge", "-q", "--ff-only", "fix/X")
    elif how == "rebase":
        (repo / "other.py").write_text("x = 1\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "unrelated")
        _git(repo, "cherry-pick", fix + "~1", fix)
    else:
        _git(repo, "merge", "-q", "--squash", "fix/X")
        _git(repo, "commit", "-qm", "squashed")
    assert LocalGit(repo).landed(fix, "fix/X") is True


def test_a_deleted_branch_cannot_be_told_from_a_merged_one(repo):
    """Merging a PR usually deletes its branch; treat that as unknown, never 'unmerged'."""
    fix = _fix(repo)
    _git(repo, "branch", "-D", "fix/X")
    assert LocalGit(repo).landed(fix, "fix/X") is None


def test_a_commit_the_repository_does_not_have_is_unknown(repo):
    assert LocalGit(repo).landed("0" * 40, "fix/X") is None


# -- the memory acts on it ----------------------------------------------------------


async def _seen_again(make_ctx, repo, tmp_path, *, commit, branch="fix/X"):
    first = make_ctx("TRIAGE", root=tmp_path / ".qaas", target_root=repo)
    envelope = make_envelope(first, **REPORT)
    await handlers(build_tools(first))["record"]({"envelope_id": envelope.id, "ticket_key": "QAAS-55"})
    defect_memory.resolve(
        first.store.root, envelope.fingerprint(), "QAAS-55", first.store.run_id,
        target=first.config.target or "", branch=branch, commit=commit,
    )
    later = make_ctx("TRIAGE", root=tmp_path / ".qaas", target_root=repo)
    again = make_envelope(later, discovered_by="API", **REPORT)
    tools = handlers(build_tools(later))
    return later, tools, await tools["record"]({"envelope_id": again.id}), envelope.fingerprint()


async def test_a_recurrence_on_unmerged_main_is_the_same_open_defect(make_ctx, repo, tmp_path):
    later, tools, result, fp = await _seen_again(make_ctx, repo, tmp_path, commit=_fix(repo))
    body = result["structuredContent"]
    assert body["regression"] is False and body["fix_unmerged"] is True
    assert body["ticket_key"] == "QAAS-55" and body["fix_branch"] == "fix/X"
    assert "NOT a regression" in text_of(result) and "fix/X" in text_of(result)
    assert not list(later.store.ledger("regression"))
    # Still resolved: the verified fix has not stopped being one.
    occurrences = await tools["get_occurrences"]({"fingerprint": fp})
    assert occurrences["structuredContent"]["resolved"] is True


async def test_once_the_fix_is_merged_a_recurrence_is_a_regression(make_ctx, repo, tmp_path):
    fix = _fix(repo)
    _git(repo, "merge", "-q", "--ff-only", "fix/X")
    later, _, result, _ = await _seen_again(make_ctx, repo, tmp_path, commit=fix)
    assert result["structuredContent"]["regression"] is True
    assert list(later.store.ledger("regression"))


async def test_a_resolution_with_no_commit_is_a_regression_as_before(make_ctx, repo, tmp_path):
    """`mark_resolved`, and every row resolved before this column existed."""
    _, _, result, _ = await _seen_again(make_ctx, repo, tmp_path, commit=None, branch=None)
    assert result["structuredContent"]["regression"] is True


async def test_search_says_where_the_fix_is_waiting(make_ctx, repo, tmp_path):
    later, tools, _, _ = await _seen_again(make_ctx, repo, tmp_path, commit=_fix(repo))
    result = await tools["search_similar"]({
        "title": REPORT["title"], "summary": REPORT["summary"], "domain": "api",
        "endpoint": "GET /v1/orders", "paths": ["api/app/orders.py"],
    })
    assert "FIX VERIFIED on fix/X" in text_of(result)
