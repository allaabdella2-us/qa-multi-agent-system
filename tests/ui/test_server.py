"""The HTTP surface.

`TestClient` runs the app in-process: it binds no port and opens no socket, so
these belong in the default offline suite alongside everything else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from qaas.store import RunStore
from qaas.ui.server import Dashboard, build_app

from .conftest import SPECS, build_run

RUN = "run-20260101T000000-aaaaaa"


@pytest.fixture
def client(run_root: Path) -> TestClient:
    return TestClient(build_app(Dashboard(run_root, specs=SPECS)))


# -- run listing and selection --------------------------------------------


def test_runs_lists_newest_first_with_cost(client: TestClient) -> None:
    rows = client.get("/api/runs").json()
    assert rows[0]["run_id"] == RUN
    assert rows[0]["cost_usd"] == pytest.approx(2.0)


def test_live_picks_the_unfinished_run(tmp_path: Path) -> None:
    build_run(tmp_path, "run-20260101T000000-aaaaaa", finish=False)
    build_run(tmp_path, "run-20260102T000000-bbbbbb", finish=True)
    client = TestClient(build_app(Dashboard(tmp_path, specs=SPECS)))
    assert client.get("/api/runs/live").json()["run_id"] == "run-20260101T000000-aaaaaa"


def test_live_says_so_when_there_are_no_runs(tmp_path: Path) -> None:
    client = TestClient(build_app(Dashboard(tmp_path, specs=SPECS)))
    response = client.get("/api/runs/live")
    assert response.status_code == 404
    assert "no runs yet" in response.json()["error"]


def test_live_is_not_swallowed_by_the_run_id_route(client: TestClient) -> None:
    # `/api/runs/live` is declared before `/api/runs/{run_id}`; if that order
    # ever flips, "live" becomes a run id and this returns "no such run".
    assert "run_id" in client.get("/api/runs/live").json()


# -- the snapshot ---------------------------------------------------------


def test_snapshot_shape(client: TestClient) -> None:
    body = client.get(f"/api/runs/{RUN}").json()
    assert body["mode"] == "pr-check"
    assert body["target_name"] == "corvid"
    assert body["phase"] == "done"
    assert body["agents"]["BROWSER"]["status"] == "skipped"
    assert body["findings"][0]["severity"] == "blocker"
    assert body["denials"][0]["tool"] == "Bash"
    assert body["phase_status"]["verify"] == "absent"


def test_unknown_run_is_a_404_and_creates_nothing(client: TestClient, run_root: Path) -> None:
    assert client.get("/api/runs/run-nope").status_code == 404
    # `qaas show <typo>` used to leave a permanent empty run on disk. A URL typo
    # must not either.
    assert not (run_root / "runs" / "run-nope").exists()


# -- the ledger -----------------------------------------------------------


def test_events_are_quiet_by_default(client: TestClient) -> None:
    quiet = client.get(f"/api/runs/{RUN}/events").json()
    loud = client.get(f"/api/runs/{RUN}/events?quiet=0").json()
    kinds = {e["kind"] for e in quiet["entries"]}
    assert "tool_call" not in kinds
    assert loud["total"] > quiet["total"]


def test_events_filter_by_agent_and_kind(client: TestClient) -> None:
    body = client.get(f"/api/runs/{RUN}/events?agent=API&kind=denial").json()
    assert body["total"] == 1
    assert body["entries"][0]["agent"] == "API"
    assert "not in API's tool allowlist" in body["entries"][0]["text"]


def test_an_unknown_kind_is_a_400_naming_the_legal_set(client: TestClient) -> None:
    # "no output" is what a mistyped filter used to look like, and it is
    # indistinguishable from "this run has none of those" (trace.py:133-136).
    response = client.get(f"/api/runs/{RUN}/events?kind=denail")
    assert response.status_code == 400
    assert "Known kinds" in response.json()["error"]


def test_events_page(client: TestClient) -> None:
    first = client.get(f"/api/runs/{RUN}/events?quiet=0&limit=3").json()
    second = client.get(f"/api/runs/{RUN}/events?quiet=0&after=3&limit=3").json()
    assert len(first["entries"]) == 3
    assert first["entries"][0]["seq"] == 0
    assert second["entries"][0]["seq"] == 3


# -- findings and artifacts -----------------------------------------------


def test_a_finding_returns_the_whole_envelope(client: TestClient) -> None:
    envelope_id = client.get(f"/api/runs/{RUN}").json()["findings"][0]["id"]
    body = client.get(f"/api/runs/{RUN}/findings/{envelope_id}").json()
    assert body["envelope_version"] == "1.0"
    # The alias is the wire name: the field is `defect_class`, serialised `class`.
    assert body["class"] == "vulnerability"
    assert body["reproduction"]["status"] == "reproduced"


def test_artifacts_are_served(client: TestClient, run_root: Path) -> None:
    path = run_root / "runs" / RUN / "artifacts" / "cross-tenant.log"
    path.write_text("GET /v1/orders as org 2 -> 200\n", encoding="utf-8")
    assert client.get(f"/api/runs/{RUN}/artifacts").json()[0]["name"] == "cross-tenant.log"
    response = client.get(f"/api/runs/{RUN}/artifacts/cross-tenant.log")
    assert response.status_code == 200
    assert "org 2" in response.text


def test_an_artifact_path_cannot_escape_the_run(client: TestClient) -> None:
    for name in ("../../ledger.jsonl", "..%2f..%2fledger.jsonl", "a/../../../etc/passwd"):
        response = client.get(f"/api/runs/{RUN}/artifacts/{name}")
        assert response.status_code in (400, 404), name
        assert "kind" not in response.text


def test_an_artifact_is_never_served_as_html(client: TestClient, run_root: Path) -> None:
    # Artifacts are written by agents. Rendering one as HTML on the dashboard's
    # own origin would let a finding script the view auditing it.
    path = run_root / "runs" / RUN / "artifacts" / "evil.html"
    path.write_text("<script>alert(1)</script>", encoding="utf-8")
    response = client.get(f"/api/runs/{RUN}/artifacts/evil.html")
    assert response.headers["content-type"].startswith("text/plain")
    assert "sandbox" in response.headers["content-security-policy"]


def test_an_image_artifact_keeps_its_type(client: TestClient, run_root: Path) -> None:
    path = run_root / "runs" / RUN / "artifacts" / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    response = client.get(f"/api/runs/{RUN}/artifacts/shot.png")
    assert response.headers["content-type"] == "image/png"


# -- score ----------------------------------------------------------------


def test_score_says_why_when_the_target_has_no_golden_ledger(client: TestClient) -> None:
    response = client.get(f"/api/runs/{RUN}/score")
    assert response.status_code == 404
    assert "golden ledger" in response.json()["error"]


def test_score_reports_recall_and_precision_when_there_is_one(
    run_root: Path, tmp_path: Path
) -> None:
    golden = tmp_path / "defects.yaml"
    golden.write_text(
        "defects:\n"
        "  - id: SEED-1\n"
        "    domain: security\n"
        "    class: vulnerability\n"
        "    severity: blocker\n"
        "    title: Cross-tenant order read\n"
        "    endpoint: GET /v1/orders\n"
        "    paths: [api/app/routes/orders.py]\n"
        "    keywords: [cross-tenant, org, orders]\n",
        encoding="utf-8",
    )
    client = TestClient(build_app(Dashboard(run_root, specs=SPECS, ledger_path=golden)))
    body = client.get(f"/api/runs/{RUN}/score").json()
    assert body["of"] == 1
    assert "recall" in body and "precision" in body


# -- the page itself ------------------------------------------------------


def test_index_and_static_are_served(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert client.get("/static/index.html").status_code == 200


#: The single route on this page allowed to write, and what it may write. The
#: old form of this test asserted every route was a GET and said "if a POST
#: ever appears, this is the test that should have to change first". It did.
#: What replaces it is narrower, not weaker: one path, named here, and a second
#: test proving that path cannot reach a policy.
WRITE_ROUTES = {"/api/config/override"}


def test_only_the_override_route_writes(client: TestClient) -> None:
    for route in build_app(Dashboard(Path("."))).routes:
        methods = getattr(route, "methods", None) or {"GET"}
        if methods <= {"GET", "HEAD"}:
            continue
        assert route.path in WRITE_ROUTES, (
            f"{route.path} accepts {sorted(methods)}. A new writing route on "
            "the dashboard is an architectural change, not a feature: add it "
            "here deliberately or not at all."
        )


def test_the_write_route_cannot_touch_a_policy(tmp_path: Path) -> None:
    """Tuning, never permission — the reason a POST is acceptable here at all.

    A page on loopback is still reachable by anything running as this user. It
    may change which model an agent runs; it must not be able to widen what
    that agent may write, which is the matrix `guardrails.py` enforces.
    """
    import shutil

    from qaas.config import load_config

    config = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parents[2] / "src" / "qaas" / "defaults" / "config",
                    config)
    cfg = load_config(search=[config])
    before = list(cfg.agents["FIXER"].policy.write_paths)
    client = TestClient(build_app(Dashboard(tmp_path, cfg=cfg, config_dirs=[config])))

    for payload in (
        {"section": "agents", "agent": "FIXER", "values": {"policy": {"write_paths": ["/"]}}},
        {"section": "agents", "agent": "FIXER", "values": {"builtin_tools": ["Bash"]}},
        {"section": "agents", "agent": "FIXER", "values": {"must_call": []}},
        {"section": "agents", "agent": "FIXER", "values": {"mcp_servers": ["vcs"]}},
    ):
        response = client.post("/api/config/override", json=payload)
        assert response.status_code == 400, payload
        assert "guardrails" in response.json()["error"]

    assert load_config(search=[config]).agents["FIXER"].policy.write_paths == before


def test_a_tunable_field_round_trips(tmp_path: Path) -> None:
    """The thing the door is for, and that deleting the file undoes all of it."""
    import shutil

    from qaas.config import load_config

    config = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parents[2] / "src" / "qaas" / "defaults" / "config",
                    config)
    cfg = load_config(search=[config])
    client = TestClient(build_app(Dashboard(tmp_path, cfg=cfg, config_dirs=[config])))

    assert client.post("/api/config/override", json={
        "section": "agents", "agent": "FIXER", "values": {"model": "claude-opus-4-7"},
    }).status_code == 200
    reloaded = load_config(search=[config])
    assert reloaded.agents["FIXER"].model == "claude-opus-4-7"
    # The fork it is not: everything else about the agent still comes from the
    # packaged file, so a later change to its policy still reaches this project.
    assert reloaded.agents["FIXER"].policy.write_paths == cfg.agents["FIXER"].policy.write_paths

    assert client.post("/api/config/override", json={"reset": True}).status_code == 200
    assert load_config(search=[config]).agents["FIXER"].model == "claude-opus-5"


def test_a_value_that_would_not_load_is_refused_before_it_is_written(tmp_path: Path) -> None:
    """Validated against a real `load_config` first.

    Writing and validating afterwards leaves a project whose every command
    fails until someone finds the file.
    """
    import shutil

    from qaas.config import OVERRIDES_FILE, load_config

    config = tmp_path / "cfg"
    shutil.copytree(Path(__file__).resolve().parents[2] / "src" / "qaas" / "defaults" / "config",
                    config)
    client = TestClient(build_app(
        Dashboard(tmp_path, cfg=load_config(search=[config]), config_dirs=[config])))

    response = client.post("/api/config/override", json={
        "section": "thresholds", "values": {"min_confidence_to_file": "banana"},
    })
    assert response.status_code == 400
    assert not (config / OVERRIDES_FILE).exists(), "a refused value was written anyway"


def test_config_is_served_and_names_the_roster(run_root: Path) -> None:
    """The configuration half of the page, over the same read-only surface."""
    from qaas.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[2] / "src" / "qaas" / "defaults" / "config")
    client = TestClient(build_app(Dashboard(run_root, specs=SPECS, cfg=cfg)))
    payload = client.get("/api/config").json()
    assert payload["loaded"] is True
    assert {a["name"] for a in payload["agents"]} >= {"MAPPER", "FIXER", "VERIFIER"}
    assert payload["run_modes"]


def test_config_without_a_config_still_answers(run_root: Path) -> None:
    """A dashboard opened in a directory holding only `.qaas/runs/`.

    The agent grid already renders without specs. A 500 here would make the
    whole page unusable for the case the runs half was built to survive.
    """
    client = TestClient(build_app(Dashboard(run_root)))
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["loaded"] is False


def test_score_keeps_the_counts_as_counts(run_root: Path, tmp_path: Path) -> None:
    """The per-item lists ride alongside `summary()`, never over it.

    `Scorecard.summary()` exposes `false_positives` as an int. Attaching the id
    list under the same key turns a number on the page into a rendered array.
    """
    golden = tmp_path / "defects.yaml"
    golden.write_text(
        "defects:\n"
        "  - id: SEED-1\n    domain: security\n    class: vulnerability\n"
        "    severity: blocker\n    title: Something else entirely\n"
        "    paths: [api/app/nowhere.py]\n    keywords: [unrelated]\n",
        encoding="utf-8",
    )
    client = TestClient(build_app(Dashboard(run_root, specs=SPECS, ledger_path=golden)))
    body = client.get(f"/api/runs/{RUN}/score").json()
    assert isinstance(body["false_positives"], int)
    assert isinstance(body["false_positive_ids"], list)
    assert isinstance(body["missed"], list)


# -- theme ------------------------------------------------------------------
#
# The page ships two palettes. There is no Python behind them, so what can be
# asserted is the part that silently rots: a colour written as a literal inside
# a rule is one the second palette cannot reach, which is how a page that
# "supports light mode" ends up with three black panels in it.


def _css() -> str:
    from qaas.ui.server import STATIC_DIR

    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def test_both_palettes_define_every_token() -> None:
    """A half-swapped palette is worse than either theme on its own.

    The event colours were chosen against a near-black ground; `--k-bold` is
    `#f2f5fa`, which is invisible on white. Every token the dark palette
    defines has to be redefined rather than inherited.
    """
    import re

    def tokens(text: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", text))

    blocks = re.findall(r"\{([^{}]*)\}", _css())
    dark = tokens(blocks[0])
    assert "--bg" in dark, "the :root dark palette should be the first block"

    # *Every* light block, not the largest one. There are two -- the media
    # query for someone who never touched the toggle, and the attribute
    # selector for the choice itself -- and checking only one lets the other
    # drift until a theme is half applied depending on the system setting.
    light_blocks = [b for b in blocks[1:] if "--bg:" in b]
    assert len(light_blocks) == 2, (
        f"expected a media-query palette and a [data-theme] palette, "
        f"found {len(light_blocks)}"
    )
    # The type faces are the same in both themes; everything else is a colour.
    for index, block in enumerate(light_blocks):
        missing = dark - tokens(block) - {"--mono", "--sans"}
        assert not missing, f"light palette {index} never redefines: {sorted(missing)}"


def test_no_opaque_colour_is_written_outside_a_palette_block() -> None:
    """A literal hex in a rule cannot be themed.

    Translucent `rgba()` accents are fine — they tint whatever is beneath them
    and work in both. An opaque hex is a surface, and a surface must be a token.
    """
    import re

    css = _css()
    body = css[css.index("* { box-sizing"):]          # past both palettes
    literals = [m for m in re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", body)]
    assert not literals, f"untokenised surfaces: {literals}"


def test_the_theme_is_chosen_before_first_paint() -> None:
    """`app.js` is a module, so it runs after the document has rendered.

    Choosing the theme there shows a frame of the wrong one on every load. The
    stored value is read in a blocking inline script in the head instead.
    """
    from qaas.ui.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    head = html[: html.index("</head>")]
    assert "qaas-theme" in head, "the stored theme is not read before paint"
    assert 'data-theme="dark"' not in html, (
        "a hardcoded theme attribute would override the stored choice and "
        "prefers-color-scheme both"
    )
