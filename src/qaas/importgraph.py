"""Which files import which, derived rather than guessed.

The one place in this system where a real parse buys something a model cannot
reliably do for itself. `affected_tests` answers "what should VERIFIER run
against this diff", and it answered it by comparing *filenames*: `orders.py` ->
`test_orders.py` scores 100, a test whose text mentions the stem scores 40. A
test that reaches the changed module through a caller -- which is step 3 of
`regression-suite-selection/SKILL.md`, "a fix inside a shared helper breaks its
consumers, not itself" -- scored zero, so the step the procedure names as
essential was delegated to the model's unaided guess against a ranking that
could not see it.

This is a *transitive* answer. Change `api/app/db.py`; `api/app/orders.py`
imports it; `tests/test_orders.py` imports that. The filename heuristic sees
nothing. A reverse-reachable closure over the import graph sees it in two hops.

Three properties, all deliberate:

  * **Parse-only.** `ast.parse` on text that is never executed and never
    imported. The target is someone else's repository, cloned from a URL
    seconds ago in the `qaas run --repo` case, and running its module-level code
    inside the process holding this user's Anthropic, Jira and GitHub
    credentials is not a thing to do for a test ranking.
  * **Python-only, and it says so.** The target can be any language; a Go or
    TypeScript repository yields an empty graph, `affected_tests` notices and
    falls back to the filename heuristic, and the result says which answer it
    gave. Silently returning "no tests are affected" for a whole language would
    be worse than the heuristic it replaced.
  * **Never raises.** A syntax error, an unreadable file, a symlink loop, a
    file that is not valid UTF-8 -- all skipped. A ranking that crashes the
    verification phase is worse than a ranking that is merely incomplete.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

#: Directories that are never the target's own source. Kept in step with
#: `test_runner._SKIP_DIRS`; a vendored tree holds tens of thousands of modules
#: and none of them is what changed.
SKIP_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".qaas",
        "build", "dist", "site-packages", ".eggs",
    }
)

#: Above this many modules, stop. A monorepo can hold a hundred thousand Python
#: files and this runs inside a tool call an agent is waiting on. The cap is a
#: latency bound, not a correctness one -- and it is reported rather than
#: silently applied, because "I looked at all of it" and "I looked at the first
#: eight thousand" are different answers.
MAX_MODULES = 8000


def is_test_file(path: Path) -> bool:
    """pytest's own default discovery rule, which is what will run these."""
    return path.name.startswith("test_") or path.name.endswith("_test.py")


@dataclass
class ImportGraph:
    """Who imports whom, as file paths relative to the root."""

    root: Path
    #: file -> the files it imports directly.
    imports: dict[str, set[str]] = field(default_factory=dict)
    #: file -> the files that import it directly. The inverse, precomputed,
    #: because every question asked of this graph is asked in that direction.
    imported_by: dict[str, set[str]] = field(default_factory=dict)
    #: Files that could not be parsed. Surfaced, never swallowed: a repository
    #: whose source does not parse produces a thin graph, and a caller reporting
    #: "no affected tests" should be able to say why.
    unparsed: list[str] = field(default_factory=list)
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.imports)

    @property
    def files(self) -> set[str]:
        return set(self.imports)

    def dependents_of(self, changed: list[str], *, max_depth: int = 6) -> dict[str, int]:
        """Every file that reaches one of `changed`, with its hop count.

        Breadth-first over `imported_by`, so the distance is the shortest import
        chain from the changed file to its consumer. `max_depth` bounds a
        pathological graph and, more usefully, keeps the ranking meaningful: a
        test six hops from a change is related to it in the way everything in a
        codebase is related to everything.

        A changed file is its own dependent at distance 0, which is what makes a
        direct test of the changed module rank above a test of its caller.
        """
        seen: dict[str, int] = {}
        frontier = [f for f in (_normalise(c) for c in changed) if f in self.imports or f in self.imported_by]
        for f in frontier:
            seen[f] = 0

        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            nxt: list[str] = []
            for node in frontier:
                for importer in self.imported_by.get(node, ()):
                    if importer not in seen:
                        seen[importer] = depth
                        nxt.append(importer)
            frontier = nxt
        return seen

    def affected_tests(self, changed: list[str], *, max_depth: int = 6) -> list[dict]:
        """Test files that reach a changed file, nearest first."""
        reach = self.dependents_of(changed, max_depth=max_depth)
        rows = [
            {"test_file": path, "distance": distance}
            for path, distance in reach.items()
            if is_test_file(Path(path))
        ]
        rows.sort(key=lambda r: (r["distance"], r["test_file"]))
        return rows


def build(root: Path | str, *, max_modules: int = MAX_MODULES) -> ImportGraph:
    """Parse every Python file under `root` and resolve its imports.

    One pass to collect files and the module names each can be reached by, a
    second to read imports and resolve them against that table. Two passes
    because an import is resolved against the whole repository, not against what
    happened to be walked first.
    """
    root = Path(root).resolve()
    graph = ImportGraph(root=root)

    files: list[Path] = []
    for path in _walk(root):
        files.append(path)
        if len(files) >= max_modules:
            graph.truncated = True
            break
    if not files:
        return graph

    # module name -> relative file path. A file is registered under every dotted
    # suffix of its path, so `src/api/app/db.py` answers to `api.app.db`,
    # `app.db` and `db`. Longer names win: a bare `db` is a weak clue and a
    # fully-qualified one is not, and registering both is what makes this work
    # across flat layouts, src-layouts and monorepos without being told which.
    modules: dict[str, str] = {}
    for path in files:
        rel = _rel(path, root)
        for name in _module_names(rel):
            existing = modules.get(name)
            if existing is None or name.count(".") > existing.count("."):
                modules[name] = rel

    for path in files:
        rel = _rel(path, root)
        graph.imports.setdefault(rel, set())
        graph.imported_by.setdefault(rel, set())

        tree = _parse(path)
        if tree is None:
            graph.unparsed.append(rel)
            continue

        for target in _imported_names(tree, rel):
            resolved = _resolve(target, modules)
            if resolved is None or resolved == rel:
                continue
            graph.imports[rel].add(resolved)
            graph.imported_by.setdefault(resolved, set()).add(rel)

    return graph


# -- the parts ---------------------------------------------------------------


def _walk(root: Path):
    """Every .py file under root, skipping vendored and generated trees."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for child in children:
            try:
                if child.is_dir():
                    # `is_symlink` first: a symlink to an ancestor is an
                    # infinite walk, and repositories do contain them.
                    #
                    # Hidden directories are skipped wholesale, matching
                    # `discover._walk`. Found by running this against qaas
                    # itself: `.claude/worktrees/` held two stale checkouts of
                    # the whole repository, so every changed file reported three
                    # copies of every test and the real one ranked no higher.
                    if child.is_symlink() or child.name.startswith(".") or child.name in SKIP_DIRS:
                        continue
                    stack.append(child)
                elif child.suffix == ".py":
                    yield child
            except OSError:
                continue


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _normalise(raw: str) -> str:
    """A caller's path as this graph spells it: posix, no line suffix, relative."""
    text = str(raw).strip().replace("\\", "/")
    # `api/app/db.py:112-118` is how an agent naturally cites a region.
    head = text.split(":", 1)[0]
    return head.lstrip("./")


def _module_names(rel: str) -> list[str]:
    """Every dotted name a file can be imported as, longest first."""
    parts = rel[:-3].split("/")  # drop '.py'
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    return [".".join(parts[i:]) for i in range(len(parts))]


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError, OSError, RecursionError):
        # A file that does not parse is a file this cannot speak for. Python 2
        # left behind, a template with placeholders in it, something generated.
        return None


def _imported_names(tree: ast.AST, rel: str) -> list[str]:
    """Dotted module names this file imports, with relative ones made absolute."""
    package = rel[:-3].split("/")
    if package and package[-1] == "__init__":
        package = package[:-1]
    else:
        package = package[:-1]

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from ..x import y` in `a/b/c.py` means `a.x`. One level is
                # the containing package, each further level climbs one more.
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                prefix = ".".join(base)
                head = f"{prefix}.{node.module}" if node.module else prefix
            else:
                head = node.module or ""
            if not head:
                continue
            names.append(head)
            # `from a.b import c` may be importing the *module* `a.b.c`, and
            # that is the edge worth having: it is the specific file, not the
            # package, that changed.
            names.extend(f"{head}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def _resolve(target: str, modules: dict[str, str]) -> str | None:
    """A dotted import name -> the file it names, if this repository has one.

    Longest match wins, walking prefixes down: `a.b.c` before `a.b` before `a`.
    Anything that resolves to nothing is a third-party or stdlib import, which
    is the common case and correctly produces no edge.
    """
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        hit = modules.get(".".join(parts[:i]))
        if hit is not None:
            return hit
    return None
