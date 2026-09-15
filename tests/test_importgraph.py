"""The import graph: derived answers, and the honest limits of them.

This module exists because `affected_tests` ranked by filename, so a test that
reached the changed code through a caller scored zero. The tests below are
mostly about the cases that ranking could not see — and about the cases this one
cannot see either, which matter just as much: a graph that quietly returns
nothing for a Go repository is worse than the guess it replaced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qaas.importgraph import ImportGraph, build, is_test_file


def write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small package with a three-layer import chain.

        helpers  <-  orders  <-  test_orders
                 <-  api/routes

    Nothing about the name `test_orders` resembles `helpers`, which is the whole
    point: the filename heuristic cannot connect them and this must.
    """
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/helpers.py", "def money(x):\n    return x\n")
    write(tmp_path, "pkg/orders.py", "from pkg.helpers import money\n\n\ndef total():\n    return money(1)\n")
    write(tmp_path, "pkg/api/__init__.py")
    write(tmp_path, "pkg/api/routes.py", "from pkg.orders import total\n")
    write(tmp_path, "tests/test_orders.py", "from pkg.orders import total\n\n\ndef test_total():\n    assert total()\n")
    write(tmp_path, "tests/test_unrelated.py", "def test_nothing():\n    assert True\n")
    return tmp_path


# -- the transitive answer --------------------------------------------------


def test_a_direct_importer_is_one_hop(repo):
    graph = build(repo)
    assert graph.affected_tests(["pkg/orders.py"]) == [
        {"test_file": "tests/test_orders.py", "distance": 1}
    ]


def test_a_test_two_hops_away_is_found(repo):
    """The case that motivates the whole module."""
    rows = graph_rows(build(repo).affected_tests(["pkg/helpers.py"]))
    assert rows == {"tests/test_orders.py": 2}


def test_an_unrelated_test_is_never_dragged_in(repo):
    """A graph that returns everything is as useless as one that returns nothing."""
    for changed in ("pkg/helpers.py", "pkg/orders.py", "pkg/api/routes.py"):
        assert "tests/test_unrelated.py" not in graph_rows(build(repo).affected_tests([changed]))


def test_depth_is_bounded_so_a_whole_codebase_is_not_the_answer(repo):
    """At six hops everything in a codebase reaches everything else."""
    assert not build(repo).affected_tests(["pkg/helpers.py"], max_depth=1)
    assert build(repo).affected_tests(["pkg/helpers.py"], max_depth=2)


def graph_rows(rows) -> dict[str, int]:
    return {r["test_file"]: r["distance"] for r in rows}


# -- how imports are actually spelled ---------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "import pkg.helpers",
        "from pkg.helpers import money",
        "from pkg import helpers",
        "from .helpers import money",
        "from . import helpers",
    ],
)
def test_every_spelling_of_an_import_resolves(tmp_path, statement):
    """Including the relative ones. `from . import x` in `pkg/orders.py` means
    `pkg.x`, and resolving it needs the file's own package, not just the text."""
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/helpers.py", "money = 1\n")
    write(tmp_path, "pkg/orders.py", statement + "\n")
    graph = build(tmp_path)
    assert "pkg/helpers.py" in graph.imports["pkg/orders.py"], statement


def test_a_deeper_relative_import_climbs_the_right_number_of_packages(tmp_path):
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/helpers.py", "money = 1\n")
    write(tmp_path, "pkg/api/__init__.py")
    write(tmp_path, "pkg/api/routes.py", "from ..helpers import money\n")
    assert "pkg/helpers.py" in build(tmp_path).imports["pkg/api/routes.py"]


def test_a_src_layout_resolves_without_being_told(tmp_path):
    """`src/qaas/store.py` answers to `qaas.store`, which is how it is imported.

    Registering a file under every dotted suffix of its path is what makes flat
    layouts, src-layouts and monorepos work without a configuration knob.
    """
    write(tmp_path, "src/proj/__init__.py")
    write(tmp_path, "src/proj/store.py", "X = 1\n")
    write(tmp_path, "tests/test_store.py", "from proj.store import X\n")
    assert graph_rows(build(tmp_path).affected_tests(["src/proj/store.py"])) == {
        "tests/test_store.py": 1
    }


def test_a_third_party_import_produces_no_edge(tmp_path):
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/orders.py", "import json\nimport pydantic\nfrom pathlib import Path\n")
    assert build(tmp_path).imports["pkg/orders.py"] == set()


# -- the limits, stated ------------------------------------------------------


def test_a_repository_with_no_python_yields_an_empty_graph(tmp_path):
    """Empty and falsey, which is the signal `affected_tests` falls back on.

    Returning "no tests are affected" for a Go or TypeScript target would be a
    confident wrong answer where the heuristic gave a useful vague one.
    """
    write(tmp_path, "main.go", "package main\n")
    write(tmp_path, "web/app.tsx", "export const App = () => null;\n")
    graph = build(tmp_path)
    assert not graph
    assert graph.affected_tests(["main.go"]) == []


def test_a_file_that_does_not_parse_is_skipped_and_counted(tmp_path):
    """Never raises. A repository with one Python 2 file left in it still gets a
    graph of everything else, and can say what it could not read."""
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/broken.py", "def f(:\n")
    write(tmp_path, "pkg/fine.py", "X = 1\n")
    write(tmp_path, "tests/test_fine.py", "from pkg.fine import X\n")
    graph = build(tmp_path)
    assert graph.unparsed == ["pkg/broken.py"]
    assert graph_rows(graph.affected_tests(["pkg/fine.py"])) == {"tests/test_fine.py": 1}


def test_vendored_and_hidden_trees_are_not_walked(tmp_path):
    """Found by running this against qaas itself: `.claude/worktrees/` held two
    stale checkouts of the whole repository, so every changed file reported three
    copies of every test and the real one ranked no higher."""
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/fine.py", "X = 1\n")
    write(tmp_path, "tests/test_fine.py", "from pkg.fine import X\n")
    write(tmp_path, "node_modules/thing/setup.py", "from pkg.fine import X\n")
    write(tmp_path, ".claude/worktrees/copy/tests/test_fine.py", "from pkg.fine import X\n")
    write(tmp_path, ".venv/lib/site.py", "from pkg.fine import X\n")
    assert graph_rows(build(tmp_path).affected_tests(["pkg/fine.py"])) == {
        "tests/test_fine.py": 1
    }


def test_a_symlinked_directory_does_not_loop(tmp_path):
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/fine.py", "X = 1\n")
    try:
        (tmp_path / "pkg" / "self").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem does not do directory symlinks")
    assert build(tmp_path)  # terminates, which is the assertion


def test_the_module_cap_is_reported_rather_than_silently_applied(tmp_path):
    """A bound on latency, not on correctness -- so it has to be visible.

    "I looked at all of it" and "I looked at the first N" are different answers
    and a caller ranking a diff deserves to know which it got.
    """
    write(tmp_path, "pkg/__init__.py")
    for i in range(6):
        write(tmp_path, f"pkg/m{i}.py", "X = 1\n")
    graph = build(tmp_path, max_modules=3)
    assert graph.truncated
    assert len(graph.imports) == 3


# -- how a caller spells a path ---------------------------------------------


@pytest.mark.parametrize("spelling", ["pkg/orders.py", "./pkg/orders.py", "pkg/orders.py:42", "pkg/orders.py:42-58"])
def test_a_cited_path_is_understood_however_an_agent_wrote_it(repo, spelling):
    """`api/app/db.py:112-118` is how an agent naturally cites a region, and it
    is the same file. `envelope.normalize_path` learned this the same way."""
    assert graph_rows(build(repo).affected_tests([spelling])) == {"tests/test_orders.py": 1}


def test_a_path_the_repository_does_not_have_reaches_nothing(repo):
    assert build(repo).affected_tests(["pkg/nonexistent.py"]) == []


def test_the_changed_file_is_its_own_dependent_at_distance_zero(repo):
    """Which is what makes a direct test of the changed module rank above a test
    of its caller -- and what makes a changed *test file* rank first."""
    graph = build(repo)
    assert graph.dependents_of(["pkg/orders.py"])["pkg/orders.py"] == 0
    assert graph_rows(graph.affected_tests(["tests/test_orders.py"])) == {
        "tests/test_orders.py": 0
    }


@pytest.mark.parametrize(
    "name, expected",
    [("test_x.py", True), ("x_test.py", True), ("x.py", False), ("testing.py", False)],
)
def test_test_discovery_matches_pytest_s_own_rule(name, expected):
    assert is_test_file(Path(name)) is expected


def test_an_empty_graph_is_falsey_but_a_populated_one_is_not(tmp_path):
    assert not ImportGraph(root=tmp_path)
    write(tmp_path, "a.py", "X = 1\n")
    assert build(tmp_path)
