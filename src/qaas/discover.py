"""Inspecting an unfamiliar repository well enough to write a target profile.

This is deliberately dumb pattern-matching, not analysis. Its job is to save a
person ten minutes of typing and to be obviously wrong when it is wrong — every
value it produces is a guess a human is expected to correct, and `qaas init`
says so. MAPPER does the real mapping later, with a model and the whole
repository in front of it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from qaas.target import DEFAULT_EXCLUDES, Auth, Environment, Layout, Role, TargetProfile

SPEC_NAMES = [
    "openapi.yaml", "openapi.yml", "openapi.json",
    "swagger.yaml", "swagger.yml", "swagger.json",
    "api/openapi.yaml", "docs/openapi.yaml", "spec/openapi.yaml",
]
OWNERSHIP_NAMES = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"]
COMPOSE_NAMES = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]

BACKEND_MARKERS = {
    "python": ("requirements.txt", "pyproject.toml", "manage.py", "Pipfile"),
    "node": ("package.json",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "ruby": ("Gemfile",),
    "rust": ("Cargo.toml",),
    "php": ("composer.json",),
}
BACKEND_HINTS = re.compile(
    r"\b(fastapi|flask|django|express|nestjs|gin|echo|spring|rails|sinatra|laravel|actix|axum)\b",
    re.I,
)
FRONTEND_HINTS = re.compile(r'"(react|vue|svelte|@angular/core|next|nuxt|solid-js)"', re.I)

TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "e2e", "integration_tests"}
MIGRATION_DIR_NAMES = {"migrations", "migrate", "alembic", "db/migrate", "prisma/migrations"}

#: `Layout.docs` existed and nothing ever filled it, so a documentation-heavy
#: repository profiled as having no documentation at all and ARCHITECT was told
#: to go and find it. Prose is a real surface: specs that contradict the code,
#: tickets that describe behaviour nobody built, standards nothing follows.
DOC_DIR_NAMES = {"docs", "doc", "documentation", "adr", "rfcs", "specs"}


@dataclass
class Discovery:
    """What inspection found, with a note on anything it could not settle."""

    layout: Layout
    environment: Environment
    auth: Auth
    languages: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _walk(root: Path, excludes: set[str], max_depth: int = 4):
    """Directories worth looking at, breadth-first, skipping vendored trees."""
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            children = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        yield current, children
        for child in children:
            if child.is_dir() and child.name not in excludes and not child.name.startswith("."):
                stack.append((child, depth + 1))


def _has_component_source(path: Path, excludes: set[str]) -> bool:
    """Whether a directory holds component source of its own.

    Deliberately not `rglob`: that descends into `node_modules`, where a great
    many packages ship .tsx, so it answers yes for almost any directory in a
    JavaScript repository — and walks a vendored tree to get there. `_walk`
    honours the same exclusions as the rest of this module.
    """
    return any(
        c.suffix in {".tsx", ".jsx"}
        for _, children in _walk(path, excludes)
        for c in children
    )


def inspect(root: Path) -> Discovery:
    """Guess a repository's shape. Every field is a hint, not a conclusion."""
    root = root.resolve()
    excludes = set(DEFAULT_EXCLUDES)
    notes: list[str] = []
    languages: set[str] = set()

    backend: list[str] = []
    frontend: list[str] = []
    tests: list[str] = []
    migrations: list[str] = []
    docs: list[str] = []

    for directory, children in _walk(root, excludes):
        names = {c.name for c in children}
        rel = _rel(directory, root) if directory != root else "."

        for language, markers in BACKEND_MARKERS.items():
            if names & set(markers):
                languages.add(language)
                if language == "node" and (directory / "package.json").exists():
                    # package.json alone says nothing; the dependencies do.
                    try:
                        pkg = (directory / "package.json").read_text(encoding="utf-8")
                    except OSError:
                        pkg = ""
                    if FRONTEND_HINTS.search(pkg):
                        frontend.append(rel)
                        continue
                    if BACKEND_HINTS.search(pkg):
                        backend.append(rel)
                        continue
                elif rel not in backend:
                    backend.append(rel)

        if directory.name in TEST_DIR_NAMES and rel not in tests:
            tests.append(rel)
        if directory.name in MIGRATION_DIR_NAMES and rel not in migrations:
            migrations.append(rel)
        # Only the top of a documentation tree: `docs` and `docs/specs` and
        # `docs/tickets` are one surface, and listing all three says nothing
        # the first does not.
        if directory.name in DOC_DIR_NAMES and not any(
            rel == d or rel.startswith(f"{d}/") for d in docs
        ):
            docs.append(rel)

    # A source directory with .tsx/.jsx in it is a frontend even without a
    # package.json of its own — monorepos often hoist dependencies.
    if not frontend:
        for candidate in ("web", "frontend", "client", "ui", "app"):
            path = root / candidate
            if path.is_dir() and _has_component_source(path, excludes):
                frontend.append(candidate)
                break

    spec = next((s for s in SPEC_NAMES if (root / s).exists()), None)
    ownership = next((o for o in OWNERSHIP_NAMES if (root / o).exists()), None)
    compose = next((c for c in COMPOSE_NAMES if (root / c).exists()), None)

    if not spec:
        notes.append(
            "No OpenAPI document found. API can still audit the API, but it has "
            "no declared contract to diff against — set layout.spec if one exists "
            "somewhere this did not look."
        )
    if not ownership:
        notes.append(
            "No CODEOWNERS file. Tickets will be filed unassigned unless you add one "
            "or set layout.ownership."
        )
    if not backend and not frontend:
        notes.append(
            "Could not identify backend or frontend directories. Set layout.backend "
            "and layout.frontend by hand — leaving them empty makes MAPPER "
            "explore blind, which is slower and less accurate."
        )

    environment = Environment(mode="none")
    if compose:
        notes.append(
            f"Found {compose}. Environment mode is still 'none': review the compose "
            "file, then set mode to 'compose' with the service names and URLs. This "
            "is not switched on automatically because bringing up someone's stack "
            "unasked is not a decision a tool should make."
        )

    # "." is not a service. A root pyproject.toml or package.json is usually
    # tooling or workspace config, and listing the root as a backend directory
    # tells MAPPER nothing while making it read the whole repository.
    if len(set(backend)) > 1:
        backend = [b for b in backend if b != "."]

    return Discovery(
        layout=Layout(
            backend=sorted(set(backend))[:6],
            frontend=sorted(set(frontend))[:4],
            tests=sorted(set(tests))[:4],
            migrations=sorted(set(migrations))[:3],
            docs=sorted(set(docs))[:4],
            spec=spec,
            ownership=ownership,
            exclude=list(DEFAULT_EXCLUDES),
        ),
        environment=environment,
        auth=Auth(mode="none"),
        languages=languages,
        notes=notes,
    )


def build_profile(
    name: str,
    root: Path,
    *,
    repo_url: str | None = None,
    description: str = "",
    default_branch: str = "main",
) -> tuple[TargetProfile, list[str]]:
    found = inspect(root)
    profile = TargetProfile(
        name=name,
        root=str(root),
        repo_url=repo_url,
        description=description or _describe(root, found),
        default_branch=default_branch,
        layout=found.layout,
        environment=found.environment,
        auth=found.auth,
    )
    return profile, found.notes


def _describe(root: Path, found: Discovery) -> str:
    langs = ", ".join(sorted(found.languages)) or "unknown stack"
    readme = next((root / n for n in ("README.md", "readme.md", "README.rst") if (root / n).exists()), None)
    first_line = ""
    if readme:
        for line in readme.read_text(errors="ignore").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and not stripped.startswith(("!", "[", "<")):
                first_line = stripped
                break
    return f"{first_line} ({langs})".strip() if first_line else f"A {langs} project."
