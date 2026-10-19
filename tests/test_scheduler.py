"""DisplayScheduler with a fake registry and clock: what reaches the frame source and the state file."""

from datetime import datetime, timedelta, timezone

import pytest

from hcontroller.config import Config
from hcontroller.display.hub import FrameHub
from hcontroller.display.schedule import OFF, Window
from hcontroller.display.scheduler import DisplayScheduler
from hcontroller.events import EventBus
from hcontroller.state import State

TZ_NAME = "America/Toronto"


def local(day, hour, minute=0):
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ_NAME)).astimezone(timezone.utc)


class FakeRegistry:
    def __init__(self, healthy=("nhl", "weather")):
        self.up = set(healthy)
        self.wanted: set[str] = set()

    def healthy(self):
        return set(self.up)

    def set_wanted(self, wanted):
        self.wanted = set(wanted)


class FakeSource:
    def __init__(self):
        self.module = self.url = self.error = self.fps = None

    def set_fps(self, fps):
        self.fps = fps

    def set_target(self, module, url):
        self.module, self.url = module, url


def make_config(**display) -> Config:
    return Config.model_validate(
        {
            "timezone": TZ_NAME,
            "display": {"default": "nhl", **display},
            "modules": {
                "nhl": {"url": "http://127.0.0.1:9134", "display": {}, "name": "NHL"},
                "weather": {
                    "command": ["weather"],
                    "url": "http://127.0.0.1:9200",
                    "display": {"frame": "/frame"},
                    "process": "scheduled",
                    "warmup_seconds": 120,
                },
                "logger": {"command": ["logger"]},
            },
        }
    )


class Harness:
    def __init__(self, tmp_path, now, config=None, healthy=("nhl", "weather")):
        self.clock = [now]
        self.config = config or make_config(schedule=[{"module": "weather", "start": "18:00", "end": "19:00"}])
        self.state = State(tmp_path / "state.json")
        self.registry = FakeRegistry(healthy)
        self.source = FakeSource()
        self.hub = FrameHub(128, 64)
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.scheduler = DisplayScheduler(
            self.config, self.state, self.registry, self.source, self.hub, self.bus, now=lambda: self.clock[0]
        )

    def at(self, when):
        self.clock[0] = when
        self.scheduler.tick()
        return self.scheduler.decision


def test_switches_apps_with_the_schedule(tmp_path):
    h = Harness(tmp_path, local(19, 12))
    assert h.at(local(19, 12)).module == "nhl"
    assert h.source.url == "http://127.0.0.1:9134/render/frame"
    assert h.at(local(19, 18, 30)).module == "weather"
    assert h.source.url == "http://127.0.0.1:9200/frame"
    assert h.at(local(19, 19)).module == "nhl"
    assert h.events.get_nowait()["type"] == "display"


def test_tells_the_registry_which_scheduled_apps_to_run(tmp_path):
    h = Harness(tmp_path, local(19, 12))
    h.at(local(19, 17, 57))
    assert h.registry.wanted == set()
    h.at(local(19, 17, 58))  # two minutes of warmup
    assert h.registry.wanted == {"weather"}
    assert h.scheduler.next_wanted_change("weather") == local(19, 19)


def test_down_app_falls_through_and_off_goes_dark(tmp_path):
    h = Harness(tmp_path, local(19, 18, 30), healthy=("nhl",))
    d = h.at(local(19, 18, 30))
    assert d.module == "nhl" and d.skipped == ("weather",)
    h.registry.up = set()
    d = h.at(local(19, 18, 30))
    assert d.module is None and h.source.url is None


def test_show_now_lasts_until_the_next_change_and_survives_a_restart(tmp_path):
    h = Harness(tmp_path, local(19, 12))
    h.at(local(19, 12))
    h.scheduler.show_now("weather")
    assert h.scheduler.decision.module == "weather" and h.scheduler.decision.source == "override"
    assert h.state.get("override")["until"] == local(19, 18).isoformat()

    again = Harness(tmp_path, local(19, 13))  # same state file
    assert again.at(local(19, 13)).module == "weather"
    assert again.at(local(19, 18, 30)).source == "window"  # override expired at 18:00
    assert again.state.get("override") is None


def test_show_now_off_and_resume(tmp_path):
    h = Harness(tmp_path, local(19, 12))
    h.scheduler.show_now("OFF")
    assert h.scheduler.decision.off
    h.scheduler.resume()
    assert h.scheduler.decision.module == "nhl" and h.scheduler.override is None
    with pytest.raises(ValueError):
        h.scheduler.show_now("logger")  # no display.frame


def test_edited_schedule_is_saved_validated_and_resettable(tmp_path):
    h = Harness(tmp_path, local(19, 12))
    with pytest.raises(ValueError, match="no app called"):
        h.scheduler.set_schedule([Window(module="nope", start="1:00", end="2:00")], "nhl")
    with pytest.raises(ValueError, match="no display.frame"):
        h.scheduler.set_schedule([], "logger")
    h.scheduler.set_schedule([Window(module="weather", start="11:00", end="13:00")], OFF)
    assert h.scheduler.decision.module == "weather" and h.scheduler.custom

    again = Harness(tmp_path, local(19, 12))
    assert again.scheduler.custom and again.scheduler.default == OFF
    assert again.at(local(19, 14)).off
    again.scheduler.reset_schedule()
    assert not again.scheduler.custom and again.state.get("schedule") is None
    assert again.at(local(19, 14)).module == "nhl"


def test_bad_saved_schedule_is_ignored(tmp_path):
    State(tmp_path / "state.json").set("schedule", {"windows": [{"module": "gone", "start": "1:00", "end": "2:00"}]})
    h = Harness(tmp_path, local(19, 12))
    assert not h.scheduler.custom and h.at(local(19, 12)).module == "nhl"


def test_brightness_from_slider_and_windows(tmp_path):
    config = make_config(schedule=[{"module": "weather", "start": "18:00", "end": "19:00", "brightness": 25}])
    h = Harness(tmp_path, local(19, 12), config=config)
    h.at(local(19, 12))
    assert h.hub.brightness == 70
    h.scheduler.set_brightness(40)
    assert h.hub.brightness == 40 and h.state.get("brightness") == 40
    h.at(local(19, 18, 30))
    assert h.hub.brightness == 25
    snapshot = h.scheduler.snapshot()
    assert snapshot["brightness"] == 25 and snapshot["base_brightness"] == 40
    with pytest.raises(ValueError):
        h.scheduler.set_brightness(0)


def test_frame_rate_is_saved_and_applied(tmp_path):
    h = Harness(tmp_path, local(19, 12), config=make_config(fps=12))
    assert h.source.fps == 12 and h.scheduler.snapshot()["fps"] == 12
    h.scheduler.set_fps(25)
    assert h.source.fps == 25 and h.state.get("fps") == 25
    events = []
    while not h.events.empty():
        events.append(h.events.get_nowait())
    assert any(e["type"] == "display" and e["display"]["fps"] == 25 for e in events)  # dashboards update
    with pytest.raises(ValueError):
        h.scheduler.set_fps(0.5)
    again = Harness(tmp_path, local(19, 12), config=make_config(fps=12))  # restarted: the saved rate wins
    assert again.source.fps == 25


def test_snapshot_says_what_comes_next(tmp_path):
    h = Harness(tmp_path, local(19, 12), healthy=("nhl",))  # weather is scheduled: not running yet
    snapshot = h.scheduler.snapshot()
    assert snapshot["name"] == "NHL"
    assert snapshot["next"]["module"] == "weather"
    assert snapshot["next"]["at"].startswith("2026-10-19T18:00")
    timeline = h.scheduler.timeline()
    assert timeline["date"] == "2026-10-19"
    assert timeline["now"] == pytest.approx(0.5)
    assert [s["module"] for s in timeline["segments"]] == ["nhl", "weather", "nhl"]
    assert h.scheduler.timeline(local(20, 12).date() + timedelta(days=0))["now"] is None
