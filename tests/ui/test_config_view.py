"""The configuration read model: what this installation is set to.

Two properties carry this module, and both are asserted here rather than left
to review. It must import no web dependency, so it stays in the offline suite.
And it must never render a secret: a profile names an environment variable
precisely so the value stays out of the file, and putting it back into a web
page would undo that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qaas.config import load_config
from qaas.paths import Workspace
from qaas.ui import config_view

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "src" / "qaas" / "defaults" / "config"


@pytest.fixture
def view() -> config_view.ConfigView:
    return config_view.ConfigView(load_config(CONFIG))


def test_the_read_model_imports_no_web_dependency():
    """What keeps it in the default offline suite, exactly as `state.py` is."""
    source = Path(config_view.__file__).read_text()
    for package in ("starlette", "uvicorn", "sse_starlette", "fastapi"):
        assert package not in source, f"{package} would take this out of the offline suite"


def test_every_section_is_present_and_populated(view):
    payload = view.to_json()
    assert payload["loaded"] is True
    for section in ("agents", "prompts", "mcp_servers", "models", "skills",
                    "hooks", "run_modes", "thresholds"):
        assert payload[section], f"{section} rendered empty"


def test_a_missing_config_is_a_page_not_a_crash():
    """`pip install` then `qaas dashboard` in a directory holding only runs.

    The agent grid already renders without specs; the configuration half must
    degrade the same way rather than 500.
    """
    payload = config_view.ConfigView(None).to_json()
    assert payload["loaded"] is False
    assert payload["agents"] == []
    assert payload["settings"]["tracker"] == "local"
    # Still serialisable: the page fetches this and must not choke.
    json.dumps(payload)


def test_no_credential_value_reaches_the_payload(view, monkeypatch):
    """Presence, never the value.

    The page is served over loopback but it is still a page, and a token
    rendered into HTML is a token in a browser cache and in a screenshot.
    """
    secret = "s3cret-token-do-not-render"
    monkeypatch.setenv("JIRA_API_TOKEN", secret)
    monkeypatch.setenv("APP_ADMIN_PASSWORD", secret)
    payload = json.dumps(config_view.ConfigView(load_config(CONFIG)).to_json())
    assert secret not in payload
    variables = {row["name"] for row in view.settings()["env"]}
    assert "JIRA_API_TOKEN" in variables, "the variable is named even though the value is not"


def test_a_server_nobody_names_is_visibly_unused(view):
    """Declaring a server grants nothing; an agent receives it by naming it.

    `used_by` is that rule made visible, and a declared server with an empty
    list is almost always a typo in some agent's `mcp_servers`.
    """
    servers = {row["name"]: row for row in view.mcp_servers()}
    assert "envelope" in servers
    assert servers["envelope"]["used_by"], "the envelope server is named by most agents"
    assert servers["playwright"]["kind"] == "stdio"


def test_each_agent_reports_the_file_that_defined_it(view):
    """"I edited the YAML and nothing changed" is answered by a path.

    Agents union by name with the nearer layer shadowing, so more than one file
    of a name can exist and only the first is in force.
    """
    fixer = next(a for a in view.agents() if a["name"] == "FIXER")
    assert fixer["source"]["path"] == "agents/fixer.yaml"
    assert fixer["source"]["layer"] in ("packaged", "project", "state")
    assert fixer["prompt"]["source"]["path"] == "FIXER.md"


def test_a_shadowed_agent_file_names_what_it_shadows(tmp_path, monkeypatch):
    """The nearer file wins and the further one is reported, not hidden."""
    project = tmp_path / "proj"
    nearer = project / ".qaas" / "config" / "agents"
    nearer.mkdir(parents=True)
    (nearer / "fixer.yaml").write_text(
        (CONFIG / "agents" / "fixer.yaml").read_text(), encoding="utf-8"
    )
    (project / ".qaas" / "config" / "system.yaml").write_text(
        (CONFIG / "system.yaml").read_text(), encoding="utf-8"
    )
    monkeypatch.chdir(project)
    workspace = Workspace.resolve()
    view = config_view.ConfigView(load_config(CONFIG), workspace)
    fixer = next(a for a in view.agents() if a["name"] == "FIXER")
    assert fixer["source"]["layer"] == "state"
    assert fixer["source"]["shadows"], "the packaged file is still there and still shadowed"


def test_every_threshold_carries_why_it_exists(view):
    """A number with no explanation invites being raised."""
    for row in view.thresholds():
        assert row["note"], f"{row['name']} has no note saying what it costs"


def test_the_severity_floor_serialises_as_its_value(view):
    """`reproduce_min_severity` is a Severity enum; JSON needs the string."""
    floor = next(r for r in view.thresholds() if r["name"] == "reproduce_min_severity")
    assert floor["value"] == "major"
    json.dumps(floor)


def test_skills_report_the_qualified_name_the_sdk_matches_on(view):
    """The unqualified name loads on one channel but never matches the allow
    rule on the other, which is why `qualified_skills` rewrites it."""
    skills = view.skills()
    assert skills, "the packaged plugin ships 30 of them"
    for skill in skills:
        assert skill["qualified"] == f"qaas:{skill['name']}"
        assert skill["summary"], f"{skill['name']} has no frontmatter description"


def test_hooks_name_which_one_can_refuse_a_call(view):
    """Enforcement is the PreToolUse hook; the page must not blur that."""
    hooks = {row["event"]: row for row in view.hooks()}
    assert hooks["PreToolUse"]["blocking"] == "yes"
    assert hooks["PostToolUse"]["blocking"] == "no"
    assert hooks["Stop"]["blocking"] == "once"
