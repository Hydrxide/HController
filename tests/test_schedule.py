from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from hcontroller.display.schedule import (
    OFF,
    Override,
    Window,
    next_change,
    next_wanted_change,
    parse_clock,
    resolve,
    timeline,
    wanted,
)

TZ = ZoneInfo("America/Toronto")
# 2026-10-19 is a Monday.


def at(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=TZ).astimezone(timezone.utc)


def w(module, start, end, days="daily", **extra) -> Window:
    return Window(module=module, start=start, end=end, days=days, **extra)


EVENING = [w("weather", "18:00", "23:00"), w("clock", "20:00", "22:00")]


def show(now, windows, healthy=("nhl", "weather", "clock"), default="nhl", override=None):
    return resolve(now, windows, default, set(healthy), TZ, 70, override)


# ------------------------------------------------------------------ priority and fall-through


def test_first_listed_active_window_wins():
    assert show(at(19, 21), EVENING).module == "weather"
    assert show(at(19, 21), list(reversed(EVENING))).module == "clock"


def test_down_app_falls_through_to_next_window_then_default():
    d = show(at(19, 21), EVENING, healthy=("nhl", "clock"))
    assert (d.module, d.window, d.skipped) == ("clock", 1, ("weather",))
    d = show(at(19, 21), EVENING, healthy=("nhl",))
    assert (d.module, d.source, d.skipped) == ("nhl", "default", ("weather", "clock"))


def test_nothing_healthy_leaves_panel_dark_but_not_off():
    d = show(at(19, 21), EVENING, healthy=())
    assert d.module is None and not d.off and d.source == "none"


def test_default_outside_windows_and_default_off():
    assert show(at(19, 12), EVENING).module == "nhl"
    d = show(at(19, 12), EVENING, default=OFF)
    assert d.module is None and d.off


def test_off_window_beats_windows_below_it():
    windows = [w(OFF, "21:00", "21:30"), *EVENING]
    d = show(at(19, 21, 15), windows)
    assert d.off and d.window == 0
    assert show(at(19, 21, 45), windows).module == "weather"


def test_window_brightness_applies_only_while_active():
    windows = [w("weather", "18:00", "23:00", brightness=30)]
    assert show(at(19, 19), windows).brightness == 30
    assert show(at(19, 12), windows).brightness == 70


# ------------------------------------------------------------------ days and midnight


def test_overnight_window_belongs_to_its_start_day():
    windows = [w("clock", "22:00", "02:00", days=["fri"])]
    assert show(at(23, 23), windows).module == "clock"  # Fri 23:00
    assert show(at(24, 1, 30), windows).module == "clock"  # Sat 01:30, still Friday's window
    assert show(at(23, 1, 30), windows).module == "nhl"  # Fri 01:30 would be Thursday's: not scheduled
    assert show(at(24, 2), windows).module == "nhl"  # end is exclusive


def test_start_equal_to_end_is_all_day():
    windows = [w("clock", "06:00", "06:00", days=["mon"])]
    assert show(at(19, 6), windows).module == "clock"
    assert show(at(20, 5, 59), windows).module == "clock"
    assert show(at(20, 6), windows).module == "nhl"


def test_weekdays_and_weekends():
    windows = [w("clock", "09:00", "10:00", days="weekdays"), w("weather", "09:00", "10:00", days="weekends")]
    assert show(at(19, 9, 30), windows).module == "clock"  # Monday
    assert show(at(24, 9, 30), windows).module == "weather"  # Saturday


def test_end_of_24_00_means_midnight():
    windows = [w("clock", "18:00", "24:00")]
    assert show(at(19, 23, 59), windows).module == "clock"
    assert show(at(20, 0, 0), windows).module == "nhl"


# ------------------------------------------------------------------ show now


def test_override_wins_until_it_expires():
    override = Override("clock", until=at(19, 18))
    assert show(at(19, 12), EVENING, override=override).module == "clock"
    assert show(at(19, 18), EVENING, override=override).module == "weather"


def test_override_off_and_down_override_falls_back():
    assert show(at(19, 12), EVENING, override=Override(OFF)).off
    d = show(at(19, 12), EVENING, healthy=("nhl",), override=Override("clock"))
    assert d.module == "nhl" and d.skipped == ("clock",)


# ------------------------------------------------------------------ next change, DST


def test_next_change_is_the_next_window_edge():
    assert next_change(EVENING, at(19, 12), TZ) == at(19, 18)
    assert next_change(EVENING, at(19, 18), TZ) == at(19, 20)
    assert next_change(EVENING, at(19, 23, 30), TZ) == at(20, 18)
    assert next_change([], at(19, 12), TZ) is None


def test_fall_back_day_is_25_hours():
    # 2026-11-01: clocks go back an hour at 02:00, so 00:00-12:00 lasts 13 real hours.
    segments = timeline(date(2026, 11, 1), [w("clock", "00:00", "12:00")], "nhl", TZ)
    assert segments[0]["module"] == "clock"
    assert segments[0]["to"] == pytest.approx(13 / 25, abs=1e-5)
    assert segments[-1]["to"] == 1.0


def test_spring_forward_day_is_23_hours():
    segments = timeline(date(2026, 3, 8), [w("clock", "00:00", "12:00")], "nhl", TZ)
    assert segments[0]["to"] == pytest.approx(11 / 23, abs=1e-5)


def test_timeline_merges_and_labels_segments():
    segments = timeline(date(2026, 10, 19), EVENING, "nhl", TZ)
    assert [(s["module"], s["window"]) for s in segments] == [("nhl", None), ("weather", 0), ("nhl", None)]
    assert segments[1]["start"].startswith("2026-10-19T18:00")
    assert timeline(date(2026, 10, 19), [], OFF, TZ) == [
        {
            "start": "2026-10-19T00:00:00-04:00",
            "end": "2026-10-20T00:00:00-04:00",
            "from": 0.0,
            "to": 1.0,
            "module": None,
            "off": True,
            "window": None,
        }
    ]


# ------------------------------------------------------------------ which scheduled apps run


def test_wanted_starts_apps_warmup_early():
    windows = [w("weather", "18:00", "19:00")]
    lead = {"weather": 60}
    assert wanted(at(19, 17, 58, 59), windows, "nhl", TZ, lead) == set()
    assert wanted(at(19, 17, 59), windows, "nhl", TZ, lead) == {"weather"}
    assert wanted(at(19, 18, 30), windows, "nhl", TZ, lead) == {"weather"}
    assert wanted(at(19, 19), windows, "nhl", TZ, lead) == set()


def test_wanted_skips_apps_under_an_off_window_and_handles_default():
    windows = [w(OFF, "01:00", "07:00"), w("weather", "00:00", "00:00")]
    assert wanted(at(19, 3), windows, "nhl", TZ, {"weather": 0}) == set()
    assert wanted(at(19, 8), windows, "nhl", TZ, {"weather": 0}) == {"weather"}
    # A scheduled default runs only when no window applies.
    assert wanted(at(19, 12), [w("clock", "18:00", "19:00")], "weather", TZ, {"weather": 0}) == {"weather"}
    assert wanted(at(19, 18, 30), [w("clock", "18:00", "19:00")], "weather", TZ, {"weather": 0}) == set()


def test_wanted_includes_show_now_target():
    assert wanted(at(19, 12), EVENING, "nhl", TZ, {"clock": 0}, Override("clock")) == {"clock"}


def test_next_wanted_change_accounts_for_warmup():
    windows = [w("weather", "18:00", "19:00")]
    assert next_wanted_change("weather", at(19, 12), windows, "nhl", TZ, 60) == at(19, 17, 59)
    assert next_wanted_change("weather", at(19, 18), windows, "nhl", TZ, 60) == at(19, 19)
    assert next_wanted_change("weather", at(19, 12), [], "nhl", TZ, 60) is None


# ------------------------------------------------------------------ parsing


def test_times_and_days_are_normalized():
    assert parse_clock(1110) == "18:30"  # YAML 1.1 read unquoted 18:30 as base-60
    assert parse_clock("7:05") == "07:05"
    window = Window(module="Off", start="7:00", end="24:00", days="mon, wed")
    assert (window.module, window.start, window.end, window.days) == (OFF, "07:00", "24:00", ["mon", "wed"])
    assert Window(module="x", start="1:00", end="2:00", days=["sat", "Sunday"]).days == "weekends"
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert Window(module="x", start="1:00", end="2:00", days=every_day).days == "daily"
    assert Window(module=False, start="1:00", end="2:00").module == OFF  # YAML's unquoted off


@pytest.mark.parametrize(
    "fields",
    [
        {"start": "24:00", "end": "01:00"},
        {"start": "12:60", "end": "13:00"},
        {"start": "noon", "end": "13:00"},
        {"start": "12:00", "end": "13:00", "days": "funday"},
        {"start": "12:00", "end": "13:00", "days": []},
        {"start": "12:00", "end": "13:00", "brightness": 0},
    ],
)
def test_bad_windows_are_rejected(fields):
    with pytest.raises(ValidationError):
        Window(module="x", **fields)


def test_horizon_finds_changes_a_week_away():
    windows = [w("clock", "09:00", "10:00", days=["sun"])]
    assert next_change(windows, at(19, 12), TZ) == at(25, 9)
    assert at(25, 9) - at(19, 12) < timedelta(days=7)
