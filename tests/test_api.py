import pytest
from conftest import FAST, free_port, pattern_spec, pid_alive, wait_sync
from fastapi.testclient import TestClient

from hcontroller.api.app import create_app
from hcontroller.config import Config
from hcontroller.controller import Controller
from hcontroller.display.hub import decode_frame


def config_for(tmp_path, **modules) -> Config:
    return Config.model_validate(
        {
            "server": {"port": free_port()},
            "display": {"schedule": [{"module": "off", "start": "01:00", "end": "07:00"}]},
            "modules": modules,
        }
    )


@pytest.fixture
def offline(tmp_path):
    """No lifecycle: nothing is started, so these tests only touch config, schedule and state."""
    (tmp_path / "app.example.yaml").write_text("teams: [TOR]\n", encoding="utf-8")
    config = config_for(
        tmp_path,
        app={
            "command": ["app"],
            "cwd": str(tmp_path),
            "url": "http://127.0.0.1:9300",
            "display": {},
            "config_file": "app.yaml",
            "config_example": "app.example.yaml",
        },
        settings={"command": ["settings"], "cwd": str(tmp_path), "config_file": "settings.json"},
        ext={"url": "http://192.168.1.40:8080", "display": {}},
    )
    controller = Controller(config, state_path=tmp_path / "state.json")
    with TestClient(create_app(controller, manage_lifecycle=False)) as client:
        yield client


def test_lists_modules_and_rejects_unknown_or_external_actions(offline):
    ids = [m["id"] for m in offline.get("/api/modules").json()]
    assert ids == ["app", "settings", "ext"]
    assert offline.get("/api/modules/nope").status_code == 404
    response = offline.post("/api/modules/ext/start")
    assert response.status_code == 409 and "outside HController" in response.json()["detail"]
    assert offline.post("/api/modules/nope/stop").status_code == 404
    assert offline.get("/api/modules/ext/config").status_code == 409


def test_config_editing_checks_saves_backs_up_and_reverts(offline, tmp_path):
    first = offline.get("/api/modules/app/config").json()
    assert (first["exists"], first["from_example"], first["text"]) == (False, True, "teams: [TOR]\n")

    bad = offline.put("/api/modules/app/config", json={"text": "teams: [TOR\n"})
    assert bad.status_code == 400 and "line" in bad.json()["detail"]
    assert not (tmp_path / "app.yaml").exists()

    saved = offline.put("/api/modules/app/config", json={"text": "teams: [EDM]\n"}).json()
    assert saved["exists"] and not saved["backup"] and not saved["restarted"]
    offline.put("/api/modules/app/config", json={"text": "teams: [MTL]\n"})
    assert (tmp_path / "app.yaml.bak").read_text() == "teams: [EDM]\n"

    reverted = offline.post("/api/modules/app/config/revert").json()
    assert reverted["text"] == "teams: [EDM]\n"

    assert offline.post("/api/modules/settings/config/revert").status_code == 400  # nothing to revert to
    bad_json = offline.put("/api/modules/settings/config", json={"text": '{"a": 1,}'})
    assert bad_json.status_code == 400 and "line 1" in bad_json.json()["detail"]


def test_schedule_editing(offline):
    current = offline.get("/api/display/schedule").json()
    assert current["custom"] is False and current["default"] == "app"
    assert [a["id"] for a in current["apps"]] == ["app", "ext"]  # settings has no display

    bad = offline.put("/api/display/schedule", json={"schedule": {"windows": [{"module": "app", "start": "25:00", "end": "1:00"}], "default": "app"}})
    assert bad.status_code == 400 and bad.json()["detail"].startswith("window 1 start")
    unknown = offline.put("/api/display/schedule", json={"schedule": {"windows": [], "default": "settings"}})
    assert unknown.status_code == 400 and "no display.frame" in unknown.json()["detail"]
    assert offline.put("/api/display/schedule", json={}).status_code == 400

    new = {"windows": [{"module": "ext", "days": ["sat", "sun"], "start": "09:00", "end": "12:00", "brightness": 30}], "default": "off"}
    saved = offline.put("/api/display/schedule", json={"schedule": new}).json()
    assert saved["custom"] and saved["windows"][0]["days"] == "weekends" and saved["default"] == "off"

    reset = offline.put("/api/display/schedule", json={"schedule": None}).json()
    assert reset["custom"] is False and reset["windows"][0]["module"] == "off"


def test_show_now_and_brightness(offline):
    assert offline.post("/api/display/show", json={"module": "settings"}).status_code == 400
    shown = offline.post("/api/display/show", json={"module": "off"}).json()
    assert shown["off"] and shown["override"]["module"] == "off"
    resumed = offline.delete("/api/display/show").json()
    assert resumed["override"] is None
    assert offline.put("/api/display", json={"brightness": 0}).status_code == 422
    assert offline.put("/api/display", json={"brightness": 35}).json()["base_brightness"] == 35


def test_timeline_and_frame(offline):
    timeline = offline.get("/api/display/timeline").json()
    assert timeline["segments"][0]["module"] in ("app", None) and 0 <= timeline["now"] <= 1
    assert offline.get("/api/display/timeline?date=2026-11-01").json()["now"] is None
    assert offline.get("/api/display/timeline?date=tomorrow").status_code == 400
    frame = offline.get("/api/display/frame")
    assert frame.headers["x-width"] == "128" and len(frame.content) == 128 * 64 * 3
    assert "<title>HController</title>" in offline.get("/").text


def test_live_run_supervises_and_streams_a_real_module(tmp_path):
    """Lifespan on: a real child process is started, shown on the panel, stopped, restarted."""
    port = free_port()
    config = config_for(tmp_path, tp=pattern_spec(port, name="Test pattern").model_dump(exclude_unset=True))
    config.display.schedule = []
    controller = Controller(config, state_path=tmp_path / "state.json", runner_options=FAST)
    with TestClient(create_app(controller)) as client:
        wait_sync(lambda: client.get("/api/modules/tp").json()["state"] == "healthy")
        wait_sync(lambda: client.get("/api/display").json()["module"] == "tp")
        wait_sync(lambda: client.get("/api/display/frame").headers["x-module"] == "tp")
        assert any(client.get("/api/display/frame").content)  # the test pattern, not black

        with client.websocket_connect("/display/stream") as stream:
            width, height, brightness, rgb = decode_frame(stream.receive_bytes())
            assert (width, height, brightness) == (128, 64, 70) and len(rgb) == 128 * 64 * 3
        with client.websocket_connect("/ws") as updates:
            snapshot = updates.receive_json()
            assert snapshot["type"] == "snapshot" and snapshot["display"]["module"] == "tp"

        pid = client.get("/api/modules/tp").json()["pid"]
        stopped = client.post("/api/modules/tp/stop").json()
        assert stopped["state"] == "stopped" and not pid_alive(pid)
        wait_sync(lambda: client.get("/api/display").json()["module"] is None)
        assert client.get("/health").json()["modules"] == {"tp": "stopped"}

        client.post("/api/modules/tp/start")
        wait_sync(lambda: client.get("/api/modules/tp").json()["state"] == "healthy")
        logs = client.get("/api/modules/tp/logs?lines=50").json()
        assert any("started (pid" in line["text"] for line in logs["lines"])
        assert client.get(f"/api/modules/tp/logs?after={logs['last']}").json()["lines"] == []
        pid = client.get("/api/modules/tp").json()["pid"]

    assert not pid_alive(pid)  # shutting HController down stops its modules
