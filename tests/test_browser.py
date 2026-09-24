"""Whether BROWSER and GUIDE have a browser to drive, asked before they run.

The first run of the whole roster dispatched both into a Playwright server whose
browser build was not installed -- the documented `npx playwright install
chromium` installs a different build -- and they spent $5.06 without opening a
page. These pin the question, the answer's three values, and that a missing
browser stops the dispatch rather than being discovered by it.
"""

from __future__ import annotations

import subprocess

import pytest

from qaas import browser
from qaas.registry import PLAYWRIGHT_MCP_VERSION
from qaas.store import RunStore
from test_router import cfg, fake_agents, make_conductor  # noqa: F401 - fixtures

#: Taken at import: the root conftest replaces `status` for every test, so the
#: suite never runs `npx`.
real_status = browser.status

_BANNER = "║ WARNING: It looks like you are running 'npx playwright install' ║\n"


def _dry_run(monkeypatch, stdout: str, returncode: int = 0, raises: Exception | None = None):
    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(argv)
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    browser._install_locations.cache_clear()
    monkeypatch.setattr(browser.subprocess, "run", run)
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/bin/npx")
    return seen


@pytest.fixture(autouse=True)
def _fresh_cache():
    browser._install_locations.cache_clear()
    yield
    browser._install_locations.cache_clear()


def _listing(*dirs) -> str:
    return _BANNER + "".join(f"Chrome for Testing\n  Install location:    {d}\n" for d in dirs)


def test_the_pinned_server_is_asked_not_the_playwright_cli(monkeypatch, tmp_path):
    (tmp_path / "chromium-1246").mkdir()
    seen = _dry_run(monkeypatch, _listing(tmp_path / "chromium-1246"))
    installed, why = real_status()
    assert installed is True and "chromium-1246" in why
    assert seen[0] == [
        "npx", "-y", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
        "install-browser", "chrome-for-testing", "--dry-run",
    ]


def test_a_missing_build_names_the_command_that_installs_it(monkeypatch, tmp_path):
    _dry_run(monkeypatch, _listing(tmp_path / "chromium-1246", tmp_path / "ffmpeg-1011"))
    installed, why = real_status()
    assert installed is False
    assert "chromium-1246" in why and "ffmpeg" not in why
    assert browser.install_command() in why
    assert f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION} install-browser chrome-for-testing" in why


def test_ffmpeg_alone_missing_does_not_cost_the_browser_agents(monkeypatch, tmp_path):
    (tmp_path / "chromium-1246").mkdir()
    _dry_run(monkeypatch, _listing(tmp_path / "chromium-1246", tmp_path / "ffmpeg-1011"))
    assert real_status()[0] is True


@pytest.mark.parametrize(
    ("stdout", "returncode", "raises"),
    [
        ("", 1, None),
        (_BANNER, 0, None),  # ran, but said nothing parseable
        ("", 0, subprocess.TimeoutExpired(["npx"], 120)),
        ("", 0, FileNotFoundError("npx")),
    ],
)
def test_a_check_that_cannot_run_is_unknown_never_missing(monkeypatch, stdout, returncode, raises):
    """Refusing BROWSER because npm was slow is the same mistake the other way."""
    _dry_run(monkeypatch, stdout, returncode, raises)
    installed, why = real_status()
    assert installed is None and "could not ask" in why


def test_no_node_at_all_is_missing(monkeypatch):
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    installed, why = real_status()
    assert installed is False and "npx" in why


def test_a_project_that_declares_its_own_playwright_is_not_second_guessed(monkeypatch):
    class Cfg:
        mcp_servers = {"playwright": object()}

    def boom(*a, **k):  # pragma: no cover - asserting it is never reached
        raise AssertionError("asked npx about a server the project replaced")

    monkeypatch.setattr(browser.subprocess, "run", boom)
    assert real_status(Cfg())[0] is None


def test_the_server_runs_headless_and_writes_beside_the_run(tmp_path):
    from support import CONFIG_SEARCH

    from qaas.config import load_config
    from qaas.mcp.context import ToolContext
    from qaas.registry import build_mcp_servers
    from qaas.store import RunStore, SystemMapStore

    cfg = load_config(search=CONFIG_SEARCH)
    store = RunStore.new(root=tmp_path / ".qaas")
    ctx = ToolContext(
        store=store, maps=SystemMapStore(tmp_path / ".qaas"), config=cfg,
        agent=cfg.agents["BROWSER"], target_root=cfg.target_root(),
    )
    args = build_mcp_servers(cfg.agents["BROWSER"], ctx)["playwright"]["args"]
    assert "--headless" in args
    # Not the server's cwd, which is the target checkout.
    assert args[args.index("--output-dir") + 1] == str(store.dir / "playwright" / "BROWSER")


# -- the router and doctor act on it ---------------------------------------------


async def test_a_missing_browser_stops_the_dispatch_and_says_how_to_fix_it(
    cfg, tmp_path, fake_agents, monkeypatch
):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    fix = browser.install_command()
    monkeypatch.setattr(browser, "status", lambda config=None: (False, f"not installed. Run: {fix}"))

    report = await make_conductor(cfg, tmp_path).run("full-loop")
    ran = {name for name, _ in calls}
    assert not {"BROWSER", "GUIDE"} & ran, "dispatched a browser agent with no browser"
    assert "API" in ran
    store = RunStore(report.run_id, root=tmp_path, create=False)
    skipped = [e for e in store.ledger("skipped") if "BROWSER" in e.detail.get("agents", [])]
    assert skipped and fix in skipped[0].detail["reason"]
    assert any(fix in note for note in report.escalations)


async def test_an_unknown_answer_dispatches_as_before(cfg, tmp_path, fake_agents, monkeypatch):
    calls, behaviour = fake_agents
    behaviour["MAPPER"] = {"publish_map": True}
    monkeypatch.setattr(browser, "status", lambda config=None: (None, "could not ask"))
    await make_conductor(cfg, tmp_path).run("nightly")
    assert "BROWSER" in {name for name, _ in calls}



def test_doctor_reports_the_browser_agents_it_would_skip(monkeypatch):
    from typer.testing import CliRunner

    from qaas import cli

    monkeypatch.setenv("CORVID_PASSWORD", "x")
    fix = browser.install_command()
    monkeypatch.setattr(browser, "status", lambda config=None: (False, f"not installed. Run: {fix}"))
    out = CliRunner().invoke(cli.app, ["doctor", "--target", "corvid"], terminal_width=400).output
    cannot = next(line for line in out.splitlines() if line.startswith("agents that cannot"))
    assert "BROWSER" in cannot and "GUIDE" in cannot
    assert fix in out.replace("\n", "")


def test_the_docs_install_the_build_the_pinned_server_wants():
    """Bumping `PLAYWRIGHT_MCP_VERSION` changes which browser build it wants;
    a doc still naming the old version installs the wrong one."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    # README is the published one; the rest are working documents that live in
    # a checkout and not in the repository, so they are checked where present.
    docs = ["README.md"] + [
        d for d in ("MANUAL.md", "CLAUDE.md", "UNDERSTANDING_QAAS.md") if (repo / d).exists()
    ]
    for doc in docs:
        text = (repo / doc).read_text(encoding="utf-8")
        # As an instruction -- a command line or a table cell -- not as history.
        wrong = re.search(r"(^|\|)\s*`?npx playwright install", text, re.M)
        assert not wrong, f"{doc} tells people to use the wrong installer"
        for version in re.findall(r"@playwright/mcp@([\w.]+) install-browser", text):
            assert version == PLAYWRIGHT_MCP_VERSION, f"{doc} installs for {version}"
