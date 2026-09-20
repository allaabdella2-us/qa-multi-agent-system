"""The import graph: derived answers, and the honest limits of them.

This module exists because `affected_tests` ranked by filename, so a test that
reached the changed code through a caller scored zero. The tests below are
mostly about the cases that ranking could not see — and about the cases this one
cannot see either, which matter just as much: a graph that quietly returns
nothing for a Go repository is worse than the guess it replaced.
"""

from __future__ import annotations

import json
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


def test_a_language_this_cannot_read_yields_an_empty_graph(tmp_path):
    """Empty and falsey, which is the signal `affected_tests` falls back on.

    Returning "no tests are affected" for a Go target would be a confident wrong
    answer where the heuristic gave a useful vague one. TypeScript used to be in
    this test beside Go and is not any more -- see the test below it.
    """
    write(tmp_path, "main.go", "package main\n")
    write(tmp_path, "lib/thing.rb", "module Thing\nend\n")
    graph = build(tmp_path)
    assert not graph
    assert graph.affected_tests(["main.go"]) == []


def test_what_it_could_not_read_is_named_rather_than_averaged_away(tmp_path):
    """"Nothing imports that" and "I cannot read this language" are different
    answers, and the caller has to be able to pass the difference on."""
    write(tmp_path, "main.go", "package main\n")
    write(tmp_path, "cmd/serve.go", "package main\n")
    graph = build(tmp_path)
    assert graph.unreadable == {".go": 2}
    assert ".go" in graph.unreadable_note
    assert build(tmp_path / "cmd").unreadable_note is not None
    write(tmp_path, "pkg/only.py", "X = 1\n")
    assert build(tmp_path).unreadable_note  # a mixed repo still says so


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


# -- TypeScript and JavaScript ----------------------------------------------
#
# The graph was Python-only while `test_runner` had already learned to run
# vitest and jest, so a TS target's suite ran and its *selection* fell back to
# filenames -- which is the half that makes this more than a guess. Verified
# against a real Next.js console: tests ran, the graph was empty.


TSCONFIG = """
{
  // Next.js writes this file with comments in it, which json.loads refuses.
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@/*": ["./src/*"],
    },
  },
}
"""


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    """A Next-shaped app with a three-layer chain, spelled the way one really is.

        src/lib/dates.ts  <-  src/lib/orders.ts  <-  src/app/page.tsx
                                                 <-  src/lib/orders.test.ts

    `orders.ts` reaches `dates.ts` through the `@/` alias, which is the thing
    that has to work: without `tsconfig.json` most imports in a modern TS
    repository resolve to nothing and the graph is all leaves.
    """
    write(tmp_path, "tsconfig.json", TSCONFIG)
    write(tmp_path, "package.json", '{"name": "app"}\n')
    write(tmp_path, "src/lib/dates.ts", "export const startOfDay = (d: Date) => d;\n")
    write(
        tmp_path,
        "src/lib/orders.ts",
        'import { startOfDay } from "@/lib/dates";\n\nexport const total = () => startOfDay(new Date());\n',
    )
    write(tmp_path, "src/app/page.tsx", 'import { total } from "@/lib/orders";\n\nexport default () => total();\n')
    write(
        tmp_path,
        "src/lib/orders.test.ts",
        'import { describe, it } from "vitest";\nimport { total } from "./orders";\n\ndescribe("total", () => it("works", () => total()));\n',
    )
    write(tmp_path, "src/lib/unrelated.test.ts", 'import { it } from "vitest";\n\nit("nothing", () => {});\n')
    return tmp_path


def test_a_typescript_test_two_hops_away_is_found(ts_repo):
    """The motivating case, in the other language.

    Nothing about the name `orders.test.ts` resembles `dates.ts`, and the hop
    between them goes through a tsconfig path alias.
    """
    rows = graph_rows(build(ts_repo).affected_tests(["src/lib/dates.ts"]))
    assert rows == {"src/lib/orders.test.ts": 2}
    assert "src/lib/unrelated.test.ts" not in rows


def test_a_tsconfig_alias_is_what_makes_the_rest_resolve(ts_repo):
    graph = build(ts_repo)
    assert graph.imports["src/lib/orders.ts"] == {"src/lib/dates.ts"}
    assert graph.imports["src/app/page.tsx"] == {"src/lib/orders.ts"}


def test_a_tsconfig_full_of_comments_and_trailing_commas_is_still_read(ts_repo):
    """A tsconfig is JSON-with-comments, which `json.loads` refuses outright --
    and refusing to read it means refusing to resolve `@/`, which is most of the
    first-party imports in the repository."""
    with pytest.raises(ValueError):
        json.loads(TSCONFIG)
    assert build(ts_repo).imports["src/lib/orders.ts"]


@pytest.mark.parametrize(
    "statement",
    [
        'import { money } from "./helpers";',
        'import money from "./helpers";',
        'import * as helpers from "./helpers";',
        'import type { Money } from "./helpers";',
        'import {\n  money,\n  other,\n} from "./helpers";',
        'export { money } from "./helpers";',
        'export * from "./helpers";',
        'const m = await import("./helpers");',
        'const m = require("./helpers");',
        'import "./helpers";',
        'import { money } from "./helpers.js";',
        'import { money } from "../lib/helpers";',
    ],
)
def test_every_spelling_of_a_js_import_resolves(tmp_path, statement):
    """Including `./helpers.js` for `helpers.ts`: ESM requires the extension and
    TypeScript requires it to be the *emitted* one, so the path as written
    exists nowhere in the source tree."""
    write(tmp_path, "lib/helpers.ts", "export const money = 1;\n")
    write(tmp_path, "lib/orders.ts", statement + "\n")
    assert "lib/helpers.ts" in build(tmp_path).imports["lib/orders.ts"], statement


@pytest.mark.parametrize(
    "layout, expected",
    [
        ({"lib/helpers.ts": ""}, "lib/helpers.ts"),
        ({"lib/helpers.tsx": ""}, "lib/helpers.tsx"),
        ({"lib/helpers.js": ""}, "lib/helpers.js"),
        ({"lib/helpers/index.ts": ""}, "lib/helpers/index.ts"),
        ({"lib/helpers/index.jsx": ""}, "lib/helpers/index.jsx"),
        # A file wins over a directory of the same name, as node resolves it.
        ({"lib/helpers.ts": "", "lib/helpers/index.ts": ""}, "lib/helpers.ts"),
    ],
)
def test_an_extensionless_specifier_finds_the_file_that_exists(tmp_path, layout, expected):
    for rel, text in layout.items():
        write(tmp_path, rel, text)
    write(tmp_path, "lib/orders.ts", 'import x from "./helpers";\n')
    assert build(tmp_path).imports["lib/orders.ts"] == {expected}


def test_a_bare_package_specifier_produces_no_edge(tmp_path):
    """`react` and `next/link` are not in the repository, so nothing about them
    can break a test that is."""
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "."}}')
    write(tmp_path, "src/next/link.ts", "export default 1;\n")
    write(
        tmp_path,
        "src/app/page.tsx",
        'import React from "react";\nimport Link from "next/link";\nimport "tailwindcss/tailwind.css";\n',
    )
    assert build(tmp_path).imports["src/app/page.tsx"] == set()


def test_a_baseurl_without_paths_still_resolves_a_repo_relative_import(tmp_path):
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src"}}')
    write(tmp_path, "src/lib/dates.ts", "export const d = 1;\n")
    write(tmp_path, "src/app/page.tsx", 'import { d } from "lib/dates";\n')
    assert build(tmp_path).imports["src/app/page.tsx"] == {"src/lib/dates.ts"}


def test_an_extended_tsconfig_carries_its_aliases_down(tmp_path):
    """Monorepos really do keep the aliases in a shared base config."""
    write(tmp_path, "tsconfig.base.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}')
    write(tmp_path, "tsconfig.json", '{"extends": "./tsconfig.base.json"}')
    write(tmp_path, "src/lib/dates.ts", "export const d = 1;\n")
    write(tmp_path, "src/app/page.tsx", 'import { d } from "@/lib/dates";\n')
    assert build(tmp_path).imports["src/app/page.tsx"] == {"src/lib/dates.ts"}


def test_vites_split_tsconfig_is_read_rather_than_shadowed(tmp_path):
    """Vite's template puts `references` in `tsconfig.json` and the aliases in
    `tsconfig.app.json`. Reading only the plain name found a config with nothing
    in it, and a config with nothing in it must not shadow the one beside it."""
    write(tmp_path, "tsconfig.json", '{"references": [{"path": "./tsconfig.app.json"}]}')
    write(tmp_path, "tsconfig.app.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}')
    write(tmp_path, "src/lib/dates.ts", "export const d = 1;\n")
    write(tmp_path, "src/page.tsx", 'import { d } from "@/lib/dates";\n')
    assert build(tmp_path).imports["src/page.tsx"] == {"src/lib/dates.ts"}


def test_the_nearest_tsconfig_wins_in_a_monorepo(tmp_path):
    """Two apps, each with its own `@/` meaning its own `src`. One table for the
    whole repository would send both of them to whichever was walked first."""
    for app in ("web", "admin"):
        write(tmp_path, f"apps/{app}/tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')
        write(tmp_path, f"apps/{app}/src/lib/dates.ts", "export const d = 1;\n")
        write(tmp_path, f"apps/{app}/src/page.tsx", 'import { d } from "@/lib/dates";\n')
    graph = build(tmp_path)
    assert graph.imports["apps/web/src/page.tsx"] == {"apps/web/src/lib/dates.ts"}
    assert graph.imports["apps/admin/src/page.tsx"] == {"apps/admin/src/lib/dates.ts"}


def test_a_commented_out_import_is_not_an_edge(tmp_path):
    """Not a tokeniser, so this is best-effort -- but a commented-out import at
    the start of its line is the realistic false edge and is cheap to drop."""
    write(tmp_path, "lib/helpers.ts", "export const money = 1;\n")
    write(tmp_path, "lib/gone.ts", "export const gone = 1;\n")
    write(
        tmp_path,
        "lib/orders.ts",
        '// import { gone } from "./gone";\n/* import { gone } from "./gone"; */\nimport { money } from "./helpers";\n',
    )
    assert build(tmp_path).imports["lib/orders.ts"] == {"lib/helpers.ts"}


def test_a_specifier_that_climbs_out_of_the_repository_reaches_nothing(tmp_path):
    write(tmp_path, "lib/orders.ts", 'import x from "../../../etc/passwd";\n')
    assert build(tmp_path).imports["lib/orders.ts"] == set()


def test_node_modules_is_not_walked_for_javascript_either(tmp_path):
    """A Node repository's `node_modules` is tens of thousands of files, and not
    one of them is what changed."""
    write(tmp_path, "lib/helpers.ts", "export const money = 1;\n")
    write(tmp_path, "lib/helpers.test.ts", 'import { money } from "./helpers";\n')
    write(tmp_path, "node_modules/dep/index.js", 'require("../../lib/helpers");\n')
    write(tmp_path, ".next/server/page.js", 'require("../../lib/helpers");\n')
    assert graph_rows(build(tmp_path).affected_tests(["lib/helpers.ts"])) == {
        "lib/helpers.test.ts": 1
    }


def test_one_graph_covers_both_languages(tmp_path):
    """The ordinary shape is a Python API beside a TS front end, and a change in
    either half has consumers in its own. Picking a winner would blind one."""
    write(tmp_path, "api/app/db.py", "X = 1\n")
    write(tmp_path, "api/tests/test_db.py", "from api.app.db import X\n")
    write(tmp_path, "web/src/lib/dates.ts", "export const d = 1;\n")
    write(tmp_path, "web/src/lib/dates.test.ts", 'import { d } from "./dates";\n')
    graph = build(tmp_path)
    assert graph_rows(graph.affected_tests(["api/app/db.py"])) == {"api/tests/test_db.py": 1}
    assert graph_rows(graph.affected_tests(["web/src/lib/dates.ts"])) == {
        "web/src/lib/dates.test.ts": 1
    }


@pytest.mark.parametrize(
    "name, expected",
    [
        ("orders.test.ts", True),
        ("orders.spec.tsx", True),
        ("orders.test.mjs", True),
        ("test.ts", True),
        ("orders.ts", False),
        ("testing.ts", False),
        # pytest's rule applied to a .ts file found exactly nothing, because
        # `test_orders.ts` is a convention nobody follows.
        ("__tests__/orders.ts", True),
        ("src/__tests__/nested/orders.tsx", True),
    ],
)
def test_js_test_discovery_matches_vitest_and_jest(name, expected):
    assert is_test_file(Path(name)) is expected


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


# -- comments are not found with a regex ------------------------------------


def test_the_alias_next_js_actually_ships_survives_comment_stripping(tmp_path):
    """`"@/*"` contains `/*`, and a regex stripper read it as a comment opener.

    `/\\*.*?\\*/` matched the `/*` inside that string and ran to the `*/` inside
    `"**/*.ts"` four lines below, taking `compilerOptions.paths` with it. The
    tsconfig then parsed to `{}` and every `@/` import in the repository
    resolved to nothing -- silently, since an unreadable config is "no aliases"
    by design. Against the real Next.js console this was 34 edges where there
    are 134: `src/data/metrics.ts` imports `@/lib/dates` and had no edge for it.

    This is verbatim the shape `create-next-app` generates.
    """
    write(tmp_path, "tsconfig.json", """{
  "compilerOptions": {
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"],
  "exclude": ["node_modules"]
}
""")
    write(tmp_path, "src/lib/dates.ts", "export const utcDayKey = (s: string) => s.slice(0, 10)\n")
    write(tmp_path, "src/data/metrics.ts", 'import { utcDayKey } from "@/lib/dates"\n')

    graph = build(tmp_path)
    assert "src/lib/dates.ts" in graph.imports["src/data/metrics.ts"]


def test_a_comment_marker_inside_a_string_is_not_a_comment(tmp_path):
    """The same flaw in source: a string may hold `/*` or `//` legitimately."""
    write(tmp_path, "src/a.ts", "export const A = 1\n")
    write(tmp_path, "src/b.ts", (
        'const glob = "/*"\n'
        'const url = "https://example.test/x"\n'
        'import { A } from "./a"   // trailing note about A\n'
        'export const B = A\n'
    ))

    graph = build(tmp_path)
    assert "src/a.ts" in graph.imports["src/b.ts"]


def test_a_real_comment_is_still_removed(tmp_path):
    """Stripping has to keep working, or a commented-out import becomes an edge."""
    write(tmp_path, "src/a.ts", "export const A = 1\n")
    write(tmp_path, "src/c.ts", "export const C = 1\n")
    write(tmp_path, "src/d.ts", (
        '/* import { C } from "./c" */\n'
        '// import { C } from "./c"\n'
        'import { A } from "./a"\n'
    ))

    graph = build(tmp_path)
    assert graph.imports["src/d.ts"] == {"src/a.ts"}
