"""The dashboard's edges: what it reads, where it writes, and who it answers.

Each test here pins a defect a reviewer reproduced against the 0.0.2 candidate:
a query string that read any JSON file on disk, an override written into
site-packages, a `--repo` run scored against the demo's defect list, a save
route that turned malformed input into a 500, and a Host check that refused the
page's own IPv6 address.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from qaas.config import OVERRIDES_FILE, load_config
from qaas.store import SystemMapStore
from qaas.ui import config_write
from qaas.ui.config_write import OverrideError
from qaas.ui.server import Dashboard, build_app
from support import write_scratch_target

from .conftest import SPECS, build_run, local_client

RUN = "run-20260101T000000-aaaaaa"
PACKAGED = Path(__file__).resolve().parents[2] / "src" / "qaas" / "defaults" / "config"


# -- ?version= on the map route ----------------------------------------------


def test_a_map_version_cannot_name_a_file_outside_the_map_store(run_root: Path) -> None:
    """`?version=../../secret` returned `<root>/../secret.json`."""
    secret = run_root.parent / "secret.json"
    secret.write_text(json.dumps({"token": "hunter2"}), encoding="utf-8")
    maps = SystemMapStore(run_root)
    version = maps.put({"services": ["orders-api"]})
    client = local_client(build_app(Dashboard(run_root, specs=SPECS)))

    for bad in ("../secret", "../../secret", "..%2Fsecret", "/etc/passwd", "a/b", "nope"):
        response = client.get(f"/api/runs/{RUN}/map", params={"version": bad})
        assert response.status_code in (400, 404), bad
        assert "hunter2" not in response.text, bad

    assert client.get(f"/api/runs/{RUN}/map", params={"version": version}).json() == {
        "services": ["orders-api"]
    }
    assert client.get(f"/api/runs/{RUN}/map").json() == {"services": ["orders-api"]}


def test_a_tampered_latest_pointer_is_not_followed(run_root: Path) -> None:
    (run_root.parent / "secret.json").write_text('{"token": "hunter2"}', encoding="utf-8")
    maps = SystemMapStore(run_root)
    maps.put({"services": []})
    (maps.dir / "latest").write_text("../../secret", encoding="utf-8")
    client = local_client(build_app(Dashboard(run_root, specs=SPECS)))
    response = client.get(f"/api/runs/{RUN}/map")
    assert "hunter2" not in response.text


def test_reading_the_map_creates_no_directory(run_root: Path) -> None:
    client = local_client(build_app(Dashboard(run_root, specs=SPECS)))
    assert client.get(f"/api/runs/{RUN}/map").status_code == 404
    assert not (run_root / "system-map").exists(), "a reader that writes"


# -- where an override is written --------------------------------------------


@pytest.fixture
def fake_packaged(tmp_path: Path, monkeypatch) -> Path:
    """A copy of the packaged config standing in for site-packages.

    Pointed at by `qaas.paths.packaged_config`, so a regression writes into
    this copy and never into the real source tree.
    """
    copy = tmp_path / "site-packages" / "qaas" / "defaults" / "config"
    shutil.copytree(PACKAGED, copy)
    write_scratch_target(copy, tmp_path / "app")
    monkeypatch.setattr("qaas.paths.packaged_config", lambda: copy.resolve())
    return copy


def test_the_packaged_config_is_never_the_override_layer(tmp_path: Path, fake_packaged: Path) -> None:
    state = tmp_path / "project" / ".qaas"
    layer = config_write.override_layer([fake_packaged], state_root=state)
    assert layer == state.resolve() / "config"

    config_write.set_values(
        [fake_packaged], section="agents", key="FIXER",
        values={"model": "claude-opus-4-7"}, state_root=state,
    )
    assert not (fake_packaged / OVERRIDES_FILE).exists(), "wrote into site-packages"
    written = state / "config" / OVERRIDES_FILE
    assert yaml.safe_load(written.read_text())["agents"]["FIXER"]["model"] == "claude-opus-4-7"
    # And it is where the loader reads it back from.
    cfg = load_config(search=[state / "config", fake_packaged])
    assert cfg.agents["FIXER"].model == "claude-opus-4-7"


def test_a_project_layer_still_wins_over_the_fallback(tmp_path: Path, fake_packaged: Path) -> None:
    project = tmp_path / "proj-config"
    project.mkdir()
    assert config_write.override_layer(
        [project, fake_packaged], state_root=tmp_path / "state"
    ) == project


def test_an_override_file_left_in_the_packaged_layer_is_carried_not_dropped(
    tmp_path: Path, fake_packaged: Path
) -> None:
    """One written there by the old bug is what the loader reads today; the
    first write now lands nearer and must not silently discard it."""
    (fake_packaged / OVERRIDES_FILE).write_text(
        "agents:\n  FIXER:\n    model: claude-opus-4-7\n", encoding="utf-8"
    )
    state = tmp_path / "state"
    data = config_write.set_values(
        [fake_packaged], section="thresholds", key=None,
        values={"flake_runs": 7}, state_root=state,
    )
    assert data["agents"]["FIXER"]["model"] == "claude-opus-4-7"
    assert data["thresholds"]["flake_runs"] == 7


def test_the_route_writes_beside_the_state_root_and_reports_that_layer(
    tmp_path: Path, fake_packaged: Path
) -> None:
    state = tmp_path / "project" / ".qaas"
    cfg = load_config(search=[fake_packaged])
    dash = Dashboard(tmp_path, cfg=cfg, config_dirs=[fake_packaged], state_root=state)
    client = local_client(build_app(dash))

    response = client.post("/api/config/override", json={
        "section": "agents", "agent": "FIXER", "values": {"model": "claude-opus-4-7"},
    })
    assert response.status_code == 200, response.text
    assert not (fake_packaged / OVERRIDES_FILE).exists()
    assert (state / "config" / OVERRIDES_FILE).is_file()
    # The reload saw it, because the new layer joined the dashboard's path.
    assert dash.cfg.agents["FIXER"].model == "claude-opus-4-7"
    shown = [d["path"] for d in client.get("/api/config").json()["settings"]["config_dirs"]]
    assert shown[0] == str((state / "config").resolve())


def test_the_config_page_reports_the_dashboards_own_search_path(tmp_path: Path) -> None:
    """`/api/config` re-resolved the workspace from the cwd, so a dashboard
    started with `--config` showed one search path and wrote to another."""
    explicit = tmp_path / "explicit"
    shutil.copytree(PACKAGED, explicit)
    write_scratch_target(explicit, tmp_path / "app")
    cfg = load_config(search=[explicit])
    client = local_client(build_app(Dashboard(tmp_path, cfg=cfg, config_dirs=[explicit])))
    shown = [d["path"] for d in client.get("/api/config").json()["settings"]["config_dirs"]]
    assert shown == [str(explicit)]


# -- the override route's robustness ------------------------------------------


@pytest.fixture
def scratch(tmp_path: Path) -> tuple[Path, object]:
    config = tmp_path / "cfg"
    shutil.copytree(PACKAGED, config)
    write_scratch_target(config, tmp_path / "app")
    cfg = load_config(search=[config])
    return config, local_client(build_app(Dashboard(tmp_path, cfg=cfg, config_dirs=[config])))


def test_an_agent_left_empty_by_hand_does_not_break_every_save(scratch) -> None:
    config, client = scratch
    (config / OVERRIDES_FILE).write_text("agents:\n  FIXER:\n  REVIEWER:\n", encoding="utf-8")
    response = client.post("/api/config/override", json={
        "section": "agents", "agent": "FIXER", "values": {"max_turns": 40},
    })
    assert response.status_code == 200, response.text
    data = yaml.safe_load((config / OVERRIDES_FILE).read_text())
    assert data["agents"]["FIXER"] == {"max_turns": 40}


def test_a_malformed_overrides_file_is_a_400_naming_it(scratch) -> None:
    config, client = scratch
    broken = "agents: [unclosed\n"
    (config / OVERRIDES_FILE).write_text(broken, encoding="utf-8")
    response = client.post("/api/config/override", json={
        "section": "thresholds", "values": {"flake_runs": 3},
    })
    assert response.status_code == 400
    assert OVERRIDES_FILE in response.json()["error"]
    assert (config / OVERRIDES_FILE).read_text() == broken, "a refused save rewrote the file"
    # Reset is how someone gets out of it from the page.
    assert client.post("/api/config/override", json={"reset": True}).status_code == 200


@pytest.mark.parametrize("text", ["- a\n- b\n", "agents: 5\n", "thresholds: [1]\n",
                                  "agents:\n  FIXER: opus\n"])
def test_a_wrongly_shaped_overrides_file_is_refused_not_clobbered(scratch, text) -> None:
    config, client = scratch
    (config / OVERRIDES_FILE).write_text(text, encoding="utf-8")
    for payload in (
        {"section": "agents", "agent": "FIXER", "values": {"max_turns": 40}},
        {"section": "thresholds", "values": {"flake_runs": 3}},
    ):
        response = client.post("/api/config/override", json=payload)
        if response.status_code == 200:
            continue          # the section it touched was not the malformed one
        assert response.status_code == 400, (text, payload, response.text)


@pytest.mark.parametrize("body", [
    [], "x", 5, None, {"reset": "yes"},
    {"section": "agents", "agent": [], "values": {"model": "m"}},
    {"section": "agents", "agent": ["FIXER"], "values": {"model": "m"}},
    {"section": "agents", "agent": {"a": 1}, "values": {"model": "m"}},
    {"section": "agents", "agent": "FIXER", "values": "ab"},
    {"section": "agents", "agent": "FIXER", "values": ["ab"]},
    {"section": "thresholds", "values": [["flake_runs", 3]]},
    {"section": ["agents"], "agent": "FIXER", "values": {"model": "m"}},
])
def test_a_body_of_the_wrong_shape_is_a_400_not_a_500(scratch, body) -> None:
    config, client = scratch
    response = client.post(
        "/api/config/override", content=json.dumps(body),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400, (body, response.status_code, response.text)
    assert response.json()["error"]
    assert not (config / OVERRIDES_FILE).exists()


def test_an_unknown_agent_is_refused(scratch) -> None:
    """`_apply_overrides` skips a name it does not know, so this was written,
    reported as saved, and never read by anything."""
    config, client = scratch
    for name in ("NOPE", "fixer"):
        response = client.post("/api/config/override", json={
            "section": "agents", "agent": name, "values": {"model": "claude-opus-4-7"},
        })
        assert response.status_code == 400, name
        assert "FIXER" in response.json()["error"], "the refusal should list real names"
    assert not (config / OVERRIDES_FILE).exists()


def test_a_stale_entry_for_a_departed_agent_can_still_be_removed(scratch) -> None:
    config, client = scratch
    (config / OVERRIDES_FILE).write_text(
        "agents:\n  FORGE:\n    model: claude-opus-4-7\n", encoding="utf-8"
    )
    response = client.post("/api/config/override", json={
        "section": "agents", "agent": "FORGE", "values": {"model": None},
    })
    assert response.status_code == 200, response.text
    assert "FORGE" not in (yaml.safe_load((config / OVERRIDES_FILE).read_text()) or {}).get(
        "agents", {}
    )


def test_a_failed_write_leaves_the_previous_file_whole(scratch, monkeypatch) -> None:
    """`write_text` truncates, then writes: a crash between left a torn file
    that every command loading a config then read."""
    import os

    config, client = scratch
    before = "agents:\n  FIXER:\n    max_turns: 12\n"
    (config / OVERRIDES_FILE).write_text(before, encoding="utf-8")

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        config_write.set_values(
            [config], section="agents", key="FIXER", values={"max_turns": 40}
        )
    assert (config / OVERRIDES_FILE).read_text() == before
    assert [p.name for p in config.iterdir() if p.name.endswith(".tmp")] == []


def test_the_route_reports_an_unwritable_layer_as_json(scratch, monkeypatch) -> None:
    import os

    config, client = scratch

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(config / OVERRIDES_FILE))

    monkeypatch.setattr(os, "replace", denied)
    response = client.post("/api/config/override", json={
        "section": "thresholds", "values": {"flake_runs": 3},
    })
    assert response.status_code == 500
    assert "Permission denied" in response.json()["error"]


def test_set_values_refuses_a_non_mapping_directly(tmp_path: Path) -> None:
    with pytest.raises(OverrideError):
        config_write.set_values([tmp_path], section="agents", key="FIXER", values="ab")  # type: ignore[arg-type]
    with pytest.raises(OverrideError):
        config_write.set_values([tmp_path], section="agents", key=["X"], values={})  # type: ignore[arg-type]


# -- scoring the right application -------------------------------------------


GOLDEN = (
    "defects:\n"
    "  - id: SEED-1\n    domain: security\n    class: vulnerability\n"
    "    severity: blocker\n    title: Cross-tenant order read\n"
    "    paths: [api/app/routes/orders.py]\n    keywords: [cross-tenant]\n"
)


def test_a_run_against_another_application_is_refused_a_score(run_root: Path, tmp_path: Path) -> None:
    """build_run records `target_root=/tmp/corvid`. A golden ledger for any
    other root is another application's defect list."""
    golden = tmp_path / "defects.yaml"
    golden.write_text(GOLDEN, encoding="utf-8")
    client = local_client(build_app(Dashboard(
        run_root, specs=SPECS, ledger_path=golden, target_root=tmp_path / "demo-app",
    )))
    response = client.get(f"/api/runs/{RUN}/score")
    assert response.status_code == 409
    error = response.json()["error"]
    assert "/tmp/corvid" in error and "--target" in error


def test_a_run_against_the_configured_application_is_scored(run_root: Path, tmp_path: Path) -> None:
    golden = tmp_path / "defects.yaml"
    golden.write_text(GOLDEN, encoding="utf-8")
    client = local_client(build_app(Dashboard(
        run_root, specs=SPECS, ledger_path=golden, target_root="/tmp/corvid",
    )))
    response = client.get(f"/api/runs/{RUN}/score")
    assert response.status_code == 200
    assert response.json()["of"] == 1


def test_the_configured_root_comes_from_the_profile(run_root: Path, tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    shutil.copytree(PACKAGED, config)
    write_scratch_target(config, tmp_path / "elsewhere")
    cfg = load_config(search=[config])
    golden = tmp_path / "defects.yaml"
    golden.write_text(GOLDEN, encoding="utf-8")
    dash = Dashboard(run_root, specs=SPECS, ledger_path=golden, cfg=cfg)
    assert dash.target_root == (tmp_path / "elsewhere").resolve()
    assert local_client(build_app(dash)).get(f"/api/runs/{RUN}/score").status_code == 409


@pytest.mark.parametrize("text", ["", "- just\n- a list\n", "defects: 5\n",
                                  "defects:\n  - title: no id\n"])
def test_an_unreadable_golden_ledger_is_a_sentence_not_a_500(
    run_root: Path, tmp_path: Path, text: str
) -> None:
    golden = tmp_path / "defects.yaml"
    golden.write_text(text, encoding="utf-8")
    client = local_client(build_app(Dashboard(run_root, specs=SPECS, ledger_path=golden)))
    response = client.get(f"/api/runs/{RUN}/score")
    assert response.status_code == 422, response.text
    assert "defects.yaml" in response.json()["error"]


# -- hosts --------------------------------------------------------------------


@pytest.mark.parametrize("host", ["[::1]:7777", "[::1]", "::1", "127.0.0.1:7777", "localhost:7777",
                                  "LOCALHOST"])
def test_every_loopback_spelling_is_answered(run_root: Path, host: str) -> None:
    """`[::1]:7777` -- how a browser spells the page it was served on `--host
    ::1` -- was refused 421 because the port was split off only after one colon."""
    client = local_client(build_app(Dashboard(run_root, specs=SPECS)))
    assert client.get("/api/runs", headers={"host": host}).status_code == 200, host


@pytest.mark.parametrize("host", ["evil.example", "evil.example:7777", "[::2]:7777",
                                  "127.0.0.1.evil.example", "[::1].evil.example"])
def test_a_non_loopback_host_is_still_refused(run_root: Path, host: str) -> None:
    client = local_client(build_app(Dashboard(run_root, specs=SPECS)))
    assert client.get("/api/runs", headers={"host": host}).status_code == 421, host


def test_an_ipv6_page_may_write_to_itself(scratch) -> None:
    config, client = scratch
    response = client.post(
        "/api/config/override",
        json={"section": "thresholds", "values": {"flake_runs": 3}},
        headers={"host": "[::1]:7777", "origin": "http://[::1]:7777"},
    )
    assert response.status_code == 200, response.text


def test_free_port_probes_with_the_hosts_own_address_family(monkeypatch) -> None:
    """It probed with AF_INET only, so `--host ::1` could never bind."""
    import socket

    from qaas.ui import serve

    families: list[int] = []

    class Probe:
        def __init__(self, family, kind, proto=0):
            families.append(family)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            pass

    monkeypatch.setattr(socket, "socket", Probe)
    assert serve.free_port("::1", 7777) == 7777
    assert serve.free_port("[::1]", 7777) == 7777
    assert serve.free_port("127.0.0.1", 7777) == 7777
    assert families[:2] == [socket.AF_INET6, socket.AF_INET6]
    assert families[2] == socket.AF_INET


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "example.com", ""])
def test_a_non_loopback_bind_is_refused_with_a_reason(host: str) -> None:
    """The Host check guards against pages in the browser. On a non-loopback
    bind any peer can send `Host: 127.0.0.1`, so the check guards nothing."""
    import click

    from qaas.ui import serve

    with pytest.raises(serve.NonLoopbackHost) as caught:
        serve.free_port(host, 7777)
    assert "loopback" in str(caught.value.message)
    # Printed by click as `Error: ...`, not as a traceback.
    assert isinstance(caught.value, click.UsageError)
    assert isinstance(caught.value, ValueError)
    with pytest.raises(serve.NonLoopbackHost):
        serve.serve_in_background(object(), host, 7777)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"])
def test_loopback_binds_are_accepted(host: str) -> None:
    from qaas.ui import serve

    assert serve.bind_host(host) in ("127.0.0.1", "127.0.0.2", "::1", "localhost")
