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

**Two languages, one graph.** Python resolves through a table of dotted module
names; TypeScript and JavaScript resolve through the filesystem, plus whatever
`tsconfig.json` says `@/lib/dates` means. A repository holding both gets one
graph covering both rather than a winner -- the ordinary shape is a Python API
beside a TS front end, and a change in either half has consumers in its own.
TS/JS was added because `test_runner` learned to *run* vitest and jest and the
graph still could not read a word of them: their suites ran and their selection
fell back to filenames, which is the half that makes this more than a guess.

Three properties, all deliberate:

  * **Parse-only.** `ast.parse` on text that is never executed and never
    imported; for TS/JS a scanner over import syntax. The target is someone
    else's repository, cloned from a URL seconds ago in the `qaas run --repo`
    case, and running its module-level code inside the process holding this
    user's Anthropic, Jira and GitHub credentials is not a thing to do for a
    test ranking. That rules out `node`, a bundler and `tsc` exactly as firmly
    as it rules out `import`.
  * **It says what it could not read.** A Go or Ruby repository yields an empty
    graph, `affected_tests` notices and falls back to the filename heuristic,
    and the answer names both which method produced it and which languages were
    walked past (`unreadable`). Silently returning "no tests are affected" for
    a whole language would be worse than the heuristic it replaced.
  * **Never raises.** A syntax error, an unreadable file, a symlink loop, text
    that is not valid UTF-8, a `tsconfig.json` with comments and trailing
    commas in it -- all skipped. A ranking that crashes the verification phase
    is worse than a ranking that is merely incomplete.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Directories that are never the target's own source. `test_runner` walks these
#: same trees through `source_files()` rather than keeping a second copy of the
#: list -- it kept one, and a comment claiming the two were in step was already
#: wrong. A vendored tree holds tens of thousands of modules and none of them is
#: what changed.
SKIP_DIRS = frozenset(
    {
        ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".qaas",
        "build", "dist", "site-packages", ".eggs", ".next", ".turbo", "coverage",
    }
)

#: TS/JS extensions, in the order TypeScript itself tries them: a project that
#: holds `y.ts` beside a built `y.js` means the source when it writes `./y`.
JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

#: Extensions a compiler *emits*. An ESM-correct TypeScript file spells an
#: import of `./y.ts` as `./y.js` -- a path that exists nowhere in the
#: repository -- so resolution has to try the source back.
_JS_EMITTED = (".js", ".jsx", ".mjs", ".cjs")

SOURCE_SUFFIXES = frozenset({".py", *JS_SUFFIXES})

#: Languages this deliberately does not read, *counted* rather than ignored so a
#: caller can tell "nothing imports the changed file" from "I cannot read this
#: language". Not a list of every file type in a repository: JSON, CSS and
#: images are not a missing import graph, they are not code.
OTHER_SOURCE_SUFFIXES = frozenset(
    {
        ".go", ".rb", ".java", ".kt", ".kts", ".rs", ".php", ".cs", ".scala",
        ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".ex", ".exs", ".erl",
        ".clj", ".dart", ".lua", ".pl", ".vue", ".svelte",
    }
)

#: Where TypeScript's `paths`/`baseUrl` live. `jsconfig.json` is the same file
#: for a project that never adopted TypeScript, and Next.js writes one. The
#: `tsconfig.<name>.json` arm is Vite's template, which puts `references` in
#: `tsconfig.json` and the aliases in `tsconfig.app.json` -- reading only the
#: plain name found a config with nothing in it and resolved no alias at all.
_TSCONFIG_RE = re.compile(r"^(?:ts|js)config(?:\.[^.]+)?\.json$")

#: How deep an `extends:` chain is followed. Monorepos really do extend a shared
#: base config; a cycle between two of them must not be an infinite loop.
_MAX_EXTENDS = 5

#: Above this many modules, stop. A monorepo can hold a hundred thousand Python
#: files and this runs inside a tool call an agent is waiting on. The cap is a
#: latency bound, not a correctness one -- and it is reported rather than
#: silently applied, because "I looked at all of it" and "I looked at the first
#: eight thousand" are different answers.
MAX_MODULES = 8000

#: vitest's and jest's default discovery: `foo.test.ts`, `foo.spec.tsx`,
#: `bar.test.mjs`, and a bare `test.ts`.
_JS_TEST_RE = re.compile(r"(?:^|\.)(?:test|spec)\.[cm]?[jt]sx?$")


def is_test_file(path: Path) -> bool:
    """Whether the runner that owns this file would collect it.

    pytest's own default rule for Python, and vitest's/jest's for TS/JS -- a
    different rule entirely, which is why this cannot be one predicate.
    `test_orders.ts` is a convention nobody follows and `orders.test.ts` is, so
    applying pytest's rule to a `.ts` file found exactly nothing.
    """
    name = path.name
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    if not name.endswith(JS_SUFFIXES):
        return False
    if _JS_TEST_RE.search(name):
        return True
    # jest's other default: anything under a `__tests__` directory, however the
    # file itself is named.
    return "__tests__" in path.parts


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
    #: Extension -> how many files carrying it were walked past because this
    #: reads Python, TypeScript and JavaScript and nothing else. The difference
    #: between "nothing imports that" and "I cannot read this language", which
    #: the caller has to pass on rather than average away.
    unreadable: dict[str, int] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.imports)

    @property
    def files(self) -> set[str]:
        return set(self.imports)

    @property
    def unreadable_note(self) -> str | None:
        """One sentence naming the languages this walked past, or None.

        Lives here rather than in the caller's message so both the derived and
        the fallback branch say the same thing about the same graph.
        """
        if not self.unreadable:
            return None
        listed = ", ".join(
            f"{count} {ext}"
            for ext, count in sorted(self.unreadable.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        )
        return (
            "This graph reads Python, TypeScript and JavaScript; it walked past "
            f"{listed} file(s), so nothing written in those languages is in it."
        )

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
    """Parse every source file under `root` and resolve its imports.

    One pass to collect files -- and, for Python, the module names each can be
    reached by -- a second to read imports and resolve them against that table.
    Two passes because an import is resolved against the whole repository, not
    against what happened to be walked first. TS/JS resolves against the set of
    files that exist, which is the same fact in the shape that language needs.
    """
    root = Path(root).resolve()
    graph = ImportGraph(root=root)

    files: list[Path] = []
    configs: list[Path] = []
    for path in _walk(root):
        if _TSCONFIG_RE.match(path.name):
            configs.append(path)
            continue
        suffix = path.suffix.lower()
        if suffix in SOURCE_SUFFIXES:
            files.append(path)
            if len(files) >= max_modules:
                graph.truncated = True
                break
        elif suffix in OTHER_SOURCE_SUFFIXES:
            graph.unreadable[suffix] = graph.unreadable.get(suffix, 0) + 1
    if not files:
        return graph

    rels = [_rel(path, root) for path in files]

    # module name -> relative file path. A file is registered under every dotted
    # suffix of its path, so `src/api/app/db.py` answers to `api.app.db`,
    # `app.db` and `db`. Longer names win: a bare `db` is a weak clue and a
    # fully-qualified one is not, and registering both is what makes this work
    # across flat layouts, src-layouts and monorepos without being told which.
    modules: dict[str, str] = {}
    for rel in rels:
        if not rel.endswith(".py"):
            continue
        for name in _module_names(rel):
            existing = modules.get(name)
            if existing is None or name.count(".") > existing.count("."):
                modules[name] = rel

    # TS/JS needs the set of paths that exist, because its resolution is a
    # filesystem question ("does ./y, ./y.ts or ./y/index.tsx exist") rather
    # than a name lookup.
    known = set(rels)
    aliases = _alias_tables(configs, root)

    for path, rel in zip(files, rels):
        graph.imports.setdefault(rel, set())
        graph.imported_by.setdefault(rel, set())

        if rel.endswith(".py"):
            targets = _python_targets(path, rel, modules, graph)
        else:
            targets = _js_targets(path, rel, known, aliases, graph)

        for resolved in targets:
            if resolved == rel:
                continue
            graph.imports[rel].add(resolved)
            graph.imported_by.setdefault(resolved, set()).add(rel)

    return graph


def source_files(root: Path):
    """Every Python/TS/JS file under `root`, vendored and hidden trees pruned.

    Public because `test_runner._collect_test_files` needs the same walk and had
    its own: `Path.rglob` *filters* after descending, so on a Node repository it
    read its way through `node_modules` -- tens of thousands of files -- to rank
    a diff. Pruning is the walk's job, not the filter's.
    """
    for path in _walk(root):
        if path.suffix.lower() in SOURCE_SUFFIXES:
            yield path


# -- the parts ---------------------------------------------------------------


def _walk(root: Path):
    """Every file under root, skipping vendored, generated and hidden trees."""
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
                elif child.is_file():
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


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


# -- Python ------------------------------------------------------------------


def _python_targets(path: Path, rel: str, modules: dict[str, str], graph: ImportGraph) -> list[str]:
    tree = _parse(path)
    if tree is None:
        graph.unparsed.append(rel)
        return []
    resolved = (_resolve(name, modules) for name in _imported_names(tree, rel))
    return [hit for hit in resolved if hit is not None]


def _module_names(rel: str) -> list[str]:
    """Every dotted name a file can be imported as, longest first."""
    parts = rel[:-3].split("/")  # drop '.py'
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    return [".".join(parts[i:]) for i in range(len(parts))]


def _parse(path: Path) -> ast.AST | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError, RecursionError):
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


# -- TypeScript and JavaScript -----------------------------------------------
#
# There is no `ast` for this and adding a dependency or shelling out to node
# would break the parse-only rule (and `node` is not installed on most machines
# this runs on). So: a scanner over the four shapes an import actually takes.
# It is deliberately syntactic and deliberately incomplete -- it reads what a
# module *names*, never what it means, and everything it cannot resolve becomes
# no edge rather than a wrong one.

#: `import x from "./y"`, `import {a, b} from '../y'`, `import * as n from
#: "@/lib/y"`, `export {a} from "./y"`, `export * from "./y"`, `import type {T}
#: from "./y"`. `[^;]{0,400}?` rather than `[^\n]*?` because a named-import list
#: is routinely spread over a dozen lines; bounded by `;` and by length so this
#: cannot run away across a whole file looking for a `from`.
_JS_FROM_RE = re.compile(r"""\b(?:import|export)\b[^;]{0,400}?\bfrom\s*["']([^"'\n]+)["']""")
#: `import("./y")` (dynamic, and Next.js's `dynamic(() => import(...))`) and
#: `require("./y")`.
_JS_CALL_RE = re.compile(r"""\b(?:import|require)\s*\(\s*["']([^"'\n]+)["']""")
#: `import "./y"` -- a side-effect import, which is how CSS and polyfills arrive
#: and also how a test file pulls in its setup.
_JS_BARE_RE = re.compile(r"""\bimport\s+["']([^"'\n]+)["']""")

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
#: Only a `//` that *starts* a line. A `//` anywhere on a line would eat the
#: rest of `const base = "https://example.test"`, and a commented-out import --
#: the realistic false edge -- is written at the start of its line.
_LINE_COMMENT_RE = re.compile(r"^[ \t]*//[^\n]*$", re.MULTILINE)


@dataclass
class _Aliases:
    """What one `tsconfig.json` says non-relative specifiers mean.

    `dir` is the root-relative directory the config governs; the nearest
    enclosing config wins, which is what makes a monorepo with a config per app
    resolve each app's `@/` to its own `src`.
    """

    dir: str
    #: Root-relative `baseUrl`, or None when the config does not set one. With
    #: it, a bare `lib/dates` is also a repository path; without it, only the
    #: `paths` patterns are.
    base: str | None
    #: (pattern, root-relative targets), exact patterns first and longer
    #: prefixes before shorter ones -- TypeScript's own precedence.
    patterns: list[tuple[str, list[str]]] = field(default_factory=list)

    def expand(self, spec: str) -> list[str]:
        out: list[str] = []
        for pattern, targets in self.patterns:
            if "*" in pattern:
                prefix, _, suffix = pattern.partition("*")
                if len(spec) < len(prefix) + len(suffix):
                    continue
                if not (spec.startswith(prefix) and spec.endswith(suffix)):
                    continue
                middle = spec[len(prefix): len(spec) - len(suffix) if suffix else len(spec)]
                out.extend(target.replace("*", middle, 1) for target in targets)
            elif spec == pattern:
                out.extend(targets)
        if self.base is not None:
            out.append(_join(self.base, spec))
        return out


def _alias_tables(configs: list[Path], root: Path) -> list[_Aliases]:
    """`compilerOptions.paths` / `baseUrl` from every tsconfig in the repository.

    Without this, most imports in a modern TS repository resolve to nothing: a
    Next.js app writes `@/lib/dates` for `src/lib/dates` and the graph would be
    all leaves. Verified against a real Next.js console, where `@/` accounted
    for the large majority of first-party imports.
    """
    merged: dict[str, _Aliases] = {}
    for path in configs:
        options = _compiler_options(path, set())
        if not options:
            continue
        config_dir = posixpath.dirname(_rel(path, root))
        raw_base = options.get("baseUrl")
        base = _join(config_dir, raw_base) if isinstance(raw_base, str) else None

        # TypeScript resolves `paths` targets against `baseUrl` when one is set
        # and against the config's own directory when it is not (TS 4.1+, and
        # what Next.js's generated config relies on).
        anchor = base if base is not None else config_dir
        patterns: list[tuple[str, list[str]]] = []
        raw_paths = options.get("paths")
        if isinstance(raw_paths, dict):
            for pattern, targets in raw_paths.items():
                if not isinstance(pattern, str) or not isinstance(targets, list):
                    continue
                resolved = [_join(anchor, t) for t in targets if isinstance(t, str)]
                if resolved:
                    patterns.append((pattern, resolved))
        patterns.sort(key=lambda entry: ("*" in entry[0], -len(entry[0])))

        if base is None and not patterns:
            # A config that declares neither is not a config for this purpose,
            # and must not shadow the enclosing one that does -- which is how a
            # Vite `tsconfig.json` full of `references` used to blank out the
            # aliases its own `tsconfig.app.json` declares.
            continue

        # One directory can hold several (`tsconfig.json` beside
        # `tsconfig.app.json`); they describe one project, so they merge.
        existing = merged.get(config_dir)
        if existing is None:
            merged[config_dir] = _Aliases(dir=config_dir, base=base, patterns=patterns)
        else:
            existing.patterns.extend(patterns)
            existing.patterns.sort(key=lambda entry: ("*" in entry[0], -len(entry[0])))
            if existing.base is None:
                existing.base = base

    # Longest directory first, so `_table_for` can take the first match and have
    # it be the nearest enclosing config.
    return sorted(merged.values(), key=lambda table: -len(table.dir))


def _compiler_options(path: Path, seen: set[str]) -> dict:
    """`compilerOptions` from a tsconfig, following a relative `extends` chain.

    Never raises: a tsconfig is JSON-with-comments, which `json.loads` refuses,
    and the file belongs to somebody else's repository. Anything unreadable is
    an empty dict and therefore no aliases, which costs edges and never costs
    correctness.
    """
    key = str(path)
    if key in seen or len(seen) >= _MAX_EXTENDS:
        return {}
    seen.add(key)

    text = _read(path)
    if text is None:
        return {}
    try:
        data = json.loads(_strip_jsonc(text))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}

    options = data.get("compilerOptions")
    options = dict(options) if isinstance(options, dict) else {}

    parent = data.get("extends")
    # A package extends (`@tsconfig/next/tsconfig.json`) lives in node_modules,
    # which is not walked and not read; only a relative one is followed.
    if isinstance(parent, str) and parent.startswith("."):
        candidate = (path.parent / parent).resolve()
        if candidate.is_dir():
            candidate = candidate / "tsconfig.json"
        elif candidate.suffix != ".json":
            candidate = candidate.with_name(candidate.name + ".json")
        if candidate.is_file():
            inherited = _compiler_options(candidate, seen)
            # The child wins, which is what `extends` means.
            inherited.update(options)
            options = inherited
    return options


def _strip_comments(text: str) -> str:
    r"""Remove comments without reading inside string literals.

    A regex cannot do this, and the failure was not theoretical. `"@/*"` is the
    path alias every Next.js project ships, so `/\*.*?\*/` matched the `/*`
    *inside that string* and ran to the `*/` inside `"**/*.ts"` four lines
    later, deleting `compilerOptions.paths` along the way. The tsconfig then
    parsed to `{}`, every `@/` import in the repository resolved to nothing, and
    it was silent -- an unreadable config is "no aliases" by design, so the
    graph simply came back thin. Found against the real console: 34 edges with
    `src/data/metrics.ts` importing `@/lib/dates` and no edge to show for it.

    Doing it properly also lifts a restriction the regex needed: line comments
    were matched only at the start of a line, to avoid eating the `//` in a URL.
    Inside a scanner that knows what a string is, a trailing `// note` after an
    import is safe to remove and is the more common shape.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:      # an escaped quote does not close
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "*":
                end = text.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
            if text[i + 1] == "/":
                end = text.find("\n", i)
                i = n if end == -1 else end   # keep the newline; line count matters
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas -- which is what a tsconfig is.

    `json.loads` refuses both, and refusing to read a tsconfig means refusing to
    resolve `@/`, which is most of a modern TS repository's imports. Cheap and
    syntactic: the alternative is a JSON5 dependency for one file.
    """
    return re.sub(r",(\s*[}\]])", r"\1", _strip_comments(text))


def _table_for(rel: str, tables: list[_Aliases]) -> _Aliases | None:
    """The nearest enclosing tsconfig's aliases, if any covers this file."""
    directory = posixpath.dirname(rel)
    for table in tables:  # longest directory first
        if not table.dir or directory == table.dir or directory.startswith(table.dir + "/"):
            return table
    return None


def _js_targets(
    path: Path, rel: str, known: set[str], tables: list[_Aliases], graph: ImportGraph
) -> list[str]:
    text = _read(path)
    if text is None:
        graph.unparsed.append(rel)
        return []
    table = _table_for(rel, tables)
    hits = (_resolve_js(spec, rel, known, table) for spec in _js_specifiers(text))
    return [hit for hit in hits if hit is not None]


def _js_specifiers(text: str) -> list[str]:
    body = _strip_comments(text)
    found: list[str] = []
    for pattern in (_JS_FROM_RE, _JS_CALL_RE, _JS_BARE_RE):
        found.extend(match.group(1) for match in pattern.finditer(body))
    return found


def _resolve_js(spec: str, importer: str, known: set[str], table: _Aliases | None) -> str | None:
    """A specifier -> the file it names, if this repository has one.

    A bare specifier (`react`, `next/link`) is a package and correctly produces
    no edge -- it is not in the repository, so nothing about it can break a test
    in it.
    """
    # `./x?raw` and `./x#frag` are bundler suffixes, not part of the path.
    spec = spec.strip().split("?", 1)[0].split("#", 1)[0]
    if not spec:
        return None

    if spec.startswith("."):
        candidate = _join(posixpath.dirname(importer), spec)
        # `../../..` out of the repository root: nothing here can be named.
        return None if candidate.startswith("..") else _js_candidate(candidate, known)

    if table is None:
        return None
    for candidate in table.expand(spec):
        if candidate.startswith(".."):
            continue
        hit = _js_candidate(candidate, known)
        if hit is not None:
            return hit
    return None


def _js_candidate(candidate: str, known: set[str]) -> str | None:
    """Node/TypeScript resolution, minus the parts that need node_modules."""
    if not candidate:
        return None
    if candidate in known:
        return candidate
    for ext in JS_SUFFIXES:
        if candidate + ext in known:
            return candidate + ext
    # `import './dates.js'` in a TypeScript file means `dates.ts`: ESM requires
    # the extension and TypeScript requires it to be the *emitted* one, so the
    # path as written exists nowhere in the source tree.
    stem, dot, ext = candidate.rpartition(".")
    if dot and f".{ext}" in _JS_EMITTED:
        for alt in (".ts", ".tsx"):
            if stem + alt in known:
                return stem + alt
    for ext in JS_SUFFIXES:
        index = f"{candidate}/index{ext}"
        if index in known:
            return index
    return None


def _join(base: str, rel: str) -> str:
    """posix join+normalise, with the repository root spelled '' rather than '.'."""
    joined = posixpath.normpath(posixpath.join(base or ".", rel or "."))
    return "" if joined == "." else joined
