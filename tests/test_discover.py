"""Repository inspection: the guesses `qaas init` writes into a target profile.

These are guesses by design, so the bar is not "correct" but "wrong in the ways
a person can see and fix". What must not happen is a guess drawn from a vendored
tree — that is confidently wrong rather than obviously wrong.
"""

from pathlib import Path

import pytest

from qaas.discover import build_profile, inspect

REPO = Path(__file__).resolve().parents[1]


def write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "repo"


def test_frontend_fallback_ignores_vendored_component_source(repo):
    """A .tsx inside node_modules is someone else's code, not this app's frontend."""
    write(repo / "app" / "node_modules" / "some-pkg" / "Widget.tsx", "export const W = 1")
    write(repo / "app" / "models.py", "")

    found = inspect(repo)

    assert "app" not in found.layout.frontend


def test_frontend_fallback_finds_real_component_source(repo):
    write(repo / "web" / "src" / "App.tsx", "export const App = 1")

    found = inspect(repo)

    assert "web" in found.layout.frontend


def test_a_node_package_is_classified_by_its_dependencies(repo):
    write(repo / "ui" / "package.json", '{"dependencies": {"react": "^18"}}')
    write(repo / "server" / "package.json", '{"dependencies": {"express": "^4"}}')

    found = inspect(repo)

    assert "ui" in found.layout.frontend
    assert "server" in found.layout.backend


def test_tests_and_migrations_are_located(repo):
    write(repo / "pyproject.toml", "")
    write(repo / "tests" / "test_thing.py", "")
    write(repo / "migrations" / "001.sql", "")

    found = inspect(repo)

    assert found.layout.tests == ["tests"]
    assert found.layout.migrations == ["migrations"]


def test_missing_spec_and_ownership_are_reported_as_notes(repo):
    write(repo / "pyproject.toml", "")

    found = inspect(repo)

    assert found.layout.spec is None
    assert found.layout.ownership is None
    assert any("OpenAPI" in n for n in found.notes)
    assert any("CODEOWNERS" in n for n in found.notes)


def test_a_compose_file_does_not_switch_the_environment_on(repo):
    """Bringing up someone's stack unasked is not a tool's decision to make."""
    write(repo / "docker-compose.yml", "services: {}")

    found = inspect(repo)

    assert found.environment.mode == "none"
    assert any("docker-compose.yml" in n for n in found.notes)


def test_the_repository_root_is_dropped_when_a_real_backend_exists(repo):
    write(repo / "pyproject.toml", "")
    write(repo / "api" / "pyproject.toml", "")

    found = inspect(repo)

    assert "." not in found.layout.backend
    assert "api" in found.layout.backend


def test_inspecting_the_bundled_target_app_matches_its_committed_profile():
    """The one case with a known answer: the profile in config/targets/corvid.yaml."""
    found = inspect(REPO / "target-app")

    assert found.layout.spec == "openapi.yaml"
    assert found.layout.ownership == "CODEOWNERS"


def test_build_profile_produces_a_valid_profile_and_its_caveats(repo):
    write(repo / "pyproject.toml", "")

    profile, notes = build_profile("my-app", repo, repo_url="https://example.test/x.git")

    assert profile.name == "my-app"
    assert profile.environment.mode == "none"
    assert profile.auth.mode == "none"
    assert profile.ledger is None  # only a calibration target has one
    assert notes
