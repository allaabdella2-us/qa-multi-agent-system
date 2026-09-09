"""The `.env` loader: convenience that cannot override a real environment.

Credentials come from the environment and never from `config/`, which is
committed. This exists only so that rule is liveable — four exports in every new
shell is how a token ends up pasted into a config file. The two rules that keep
it from becoming a second configuration system are both tested here.
"""

from __future__ import annotations

import os

import pytest

from qaas.envfile import ENV_FILE_VAR, load_env_file, parse_env


@pytest.fixture(autouse=True)
def _allow_the_loader(monkeypatch):
    """The suite disables the loader session-wide; these tests are about it."""
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)


def test_comments_blanks_exports_and_quotes_are_all_tolerated():
    parsed = parse_env(
        "# a comment\n"
        "\n"
        "export JIRA_EMAIL=bot@acme.example\n"
        'JIRA_API_TOKEN="tok-123"\n'
        "  JIRA_PROJECT_KEY = KAN  \n"
        "NOT_AN_ASSIGNMENT\n"
    )
    assert parsed == {
        "JIRA_EMAIL": "bot@acme.example",
        "JIRA_API_TOKEN": "tok-123",
        "JIRA_PROJECT_KEY": "KAN",
    }


def test_a_quote_inside_a_value_survives():
    """Only wrapping quotes are stripped: a token containing one is likelier
    than a caller who meant to keep the wrapper."""
    assert parse_env("T=ab\"cd")["T"] == 'ab"cd'


def test_the_real_environment_always_wins(tmp_path, monkeypatch):
    """The load-bearing rule. A stale `.env` must not be able to redirect a run
    away from the project the operator named on the command line."""
    (tmp_path / ".env").write_text("JIRA_PROJECT_KEY=FROM_FILE\nJIRA_EMAIL=bot@acme.example\n")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "FROM_SHELL")
    monkeypatch.delenv("JIRA_EMAIL", raising=False)

    path, applied = load_env_file(tmp_path)

    assert path == tmp_path / ".env"
    assert applied == ["JIRA_EMAIL"]          # the one it actually contributed
    assert os.environ["JIRA_PROJECT_KEY"] == "FROM_SHELL"
    assert os.environ["JIRA_EMAIL"] == "bot@acme.example"


def test_the_state_directory_is_preferred_over_the_repository_root(tmp_path, monkeypatch):
    """`.qaas/` is already gitignored, so a credential there cannot be committed
    by accident. A `.env` at a repository root can."""
    (tmp_path / ".env").write_text("WHICH=root\n")
    (tmp_path / ".qaas").mkdir()
    (tmp_path / ".qaas" / ".env").write_text("WHICH=state\n")
    monkeypatch.delenv("WHICH", raising=False)

    path, _ = load_env_file(tmp_path)

    assert path == tmp_path / ".qaas" / ".env"
    assert os.environ["WHICH"] == "state"


def test_an_empty_override_turns_the_mechanism_off(tmp_path, monkeypatch):
    """What CI and this suite use: real environment variables, and no chance
    that a stray `.env` in a checkout silently replaces them."""
    (tmp_path / ".env").write_text("WHICH=file\n")
    monkeypatch.setenv(ENV_FILE_VAR, "")
    monkeypatch.delenv("WHICH", raising=False)

    assert load_env_file(tmp_path) == (None, [])
    assert "WHICH" not in os.environ


def test_an_override_path_is_read_instead_of_the_search(tmp_path, monkeypatch):
    elsewhere = tmp_path / "creds" / "jira.env"
    elsewhere.parent.mkdir()
    elsewhere.write_text("WHICH=explicit\n")
    (tmp_path / ".env").write_text("WHICH=root\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(elsewhere))
    monkeypatch.delenv("WHICH", raising=False)

    path, _ = load_env_file(tmp_path)

    assert path == elsewhere
    assert os.environ["WHICH"] == "explicit"


def test_no_env_file_anywhere_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path) == (None, [])
