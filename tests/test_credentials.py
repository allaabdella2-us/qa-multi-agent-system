"""Where a credential can end up, and where it must not.

Three leaks, one theme. `.qaas/.env` -- where the docs tell people to put
`JIRA_API_TOKEN` -- was not in the `.gitignore` `qaas init` writes. A token in a
clone URL survived redaction on the reuse path and sat in the clone's own
`.git/config`. And the agent's process inherited this one's whole environment,
so a target's `conftest.py` run through Bash read the Jira and GitHub tokens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qaas import cli
from qaas.registry import scrubbed_credentials


# -- the agent's environment ---------------------------------------------------


def test_qaas_credentials_are_blanked_for_the_agent():
    env = {
        "JIRA_API_TOKEN": "t", "JIRA_EMAIL": "me@example.com", "GITHUB_TOKEN": "g",
        "GH_TOKEN": "g", "NPM_TOKEN": "n", "DATABASE_PASSWORD": "p", "OPENAI_API_KEY": "o",
        # The word first, as a real `.env` spelled it -- the suffix-only
        # pattern let this one through.
        "TOKEN_PYPI": "pypi-x", "SECRET_KEY_BASE": "s", "GOOGLE_CREDENTIALS": "c",
    }
    assert scrubbed_credentials(env) == {name: "" for name in env}


@pytest.mark.parametrize(
    "name",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
     "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "QAAS_TARGET_PASSWORD", "PATH", "HOME",
     "TOKENIZERS_PARALLELISM", "PATHEXT", "COMPOSE_PROJECT_NAME"],
)
def test_what_claude_code_itself_needs_is_left_alone(name):
    assert name not in scrubbed_credentials({name: "value"})


def test_build_options_hands_the_agent_blank_credentials(monkeypatch, tmp_path):
    from support import CONFIG_SEARCH

    from qaas.config import load_config
    from qaas.mcp.context import ToolContext
    from qaas.registry import build_options
    from qaas.store import RunStore, SystemMapStore

    monkeypatch.setenv("JIRA_API_TOKEN", "secret-jira")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-gh")
    cfg = load_config(search=CONFIG_SEARCH)
    ctx = ToolContext(
        store=RunStore.new(root=tmp_path), maps=SystemMapStore(tmp_path), config=cfg,
        agent=cfg.agents["VERIFIER"], target_root=cfg.target_root(),
    )
    env = build_options(cfg.agents["VERIFIER"], ctx).env
    assert env["JIRA_API_TOKEN"] == "" and env["GITHUB_TOKEN"] == ""
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"


# -- .qaas/.gitignore ------------------------------------------------------------


def _ignored(repo: Path, rel: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", rel], check=False
    ).returncode == 0


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_a_fresh_state_dir_ignores_env_clones_and_scores(git_repo):
    cli._ensure_state_gitignore(git_repo / ".qaas")
    for rel in (".qaas/.env", ".qaas/targets/api/x.py", ".qaas/scores/r.json", ".qaas/runs/r/ledger.jsonl"):
        assert _ignored(git_repo, rel), rel
    assert not _ignored(git_repo, ".qaas/config/system.yaml")


def test_an_older_gitignore_is_upgraded_not_skipped(git_repo):
    """Every project initialised before 0.0.2 has the template without `.env`."""
    state = git_repo / ".qaas"
    state.mkdir()
    (state / ".gitignore").write_text("runs/\ntickets/\nmemory.db\n# mine\nlocal-notes/\n")
    cli._ensure_state_gitignore(state)
    text = (state / ".gitignore").read_text()
    assert "local-notes/" in text
    assert _ignored(git_repo, ".qaas/.env")
    assert _ignored(git_repo, ".qaas/targets/x")
    cli._ensure_state_gitignore(state)
    assert (state / ".gitignore").read_text() == text  # idempotent


# -- a credential in a clone URL ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://u:ghp_SECRET@github.com/o/api.git", "https://github.com/o/api.git"),
        ("https://gitlab.example.com/o/api.git?private_token=SECRET", "https://gitlab.example.com/o/api.git"),
        ("git@github.com:o/api.git", "git@github.com:o/api.git"),
    ],
)
def test_redaction_drops_userinfo_and_query(url, shown):
    assert cli._redact_url(url) == shown


def test_a_token_in_the_query_does_not_name_the_target():
    assert cli._slug(cli._repo_basename("https://h/o/api.git?private_token=SECRET")) == "api"


def _fake_clone(monkeypatch):
    """`git clone` without a network: a real repo whose origin is the URL given."""
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[:2] == ["git", "clone"]:
            url, dest = argv[-2], argv[-1]
            real_run(["git", "init", "-q", dest], check=True)
            real_run(["git", "-C", dest, "remote", "add", "origin", url], check=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)


def test_the_clone_keeps_no_credential_on_disk(tmp_path, monkeypatch):
    _fake_clone(monkeypatch)
    url = "https://someone:ghp_SECRET@github.com/octo/hello.git"
    root, stored = cli._materialise_repo(url, tmp_path)
    assert "ghp_SECRET" not in stored
    assert "ghp_SECRET" not in (root / ".git" / "config").read_text()


def test_reusing_a_clone_returns_the_redacted_url(tmp_path, monkeypatch):
    _fake_clone(monkeypatch)
    url = "https://someone:ghp_SECRET@github.com/octo/hello.git"
    cli._materialise_repo(url, tmp_path)
    _, stored = cli._materialise_repo(url, tmp_path)
    assert "ghp_SECRET" not in stored
