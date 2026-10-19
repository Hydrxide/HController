"""The LED panel schedule: which app the panel shows at any moment.

Everything here is a pure function of (time, windows, healthy apps), so every rule can be
unit-tested with fixed datetimes. Instants are timezone-aware and compared in UTC: comparing
two datetimes that share a ``ZoneInfo`` compares wall-clock time, which is wrong across DST.

Rules:
- Windows are an ordered list. The first window that is active now and whose app is healthy
  gets the panel; an active window whose app is down is skipped. An active ``off`` window
  darkens the panel.
- With no window applying, the panel shows ``default`` (an app, or ``off``).
- A Show now override beats the schedule while it lasts, if its app is healthy.
- ``end <= start`` means the window runs past midnight; ``days`` names the day it starts.
  ``start == end`` is a full 24 hours.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

OFF = "off"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_FULL_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_GROUPS = {
    "daily": frozenset(range(7)),
    "weekdays": frozenset(range(5)),
    "weekends": frozenset({5, 6}),
}
HORIZON_DAYS = 8  # how far ahead to look for the next change
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


# ------------------------------------------------------------------ windows


def parse_days(value: Any) -> frozenset[int]:
    """``daily`` / ``weekdays`` / ``weekends``, or day names (``[mon, wed]`` or ``"mon,wed"``)."""
    if isinstance(value, str):
        items = [p for p in re.split(r"[,\s]+", value.strip().lower()) if p]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(p).strip().lower() for p in value]
    else:
        raise ValueError(f"days must be daily, weekdays, weekends or a list of days, not {value!r}")
    days: set[int] = set()
    for item in items:
        if item in DAY_GROUPS:
            days |= DAY_GROUPS[item]
        elif item in DAYS:
            days.add(DAYS.index(item))
        elif item in _FULL_DAYS:
            days.add(_FULL_DAYS.index(item))
        else:
            raise ValueError(f"{item!r} is not a day (use mon..sun, daily, weekdays or weekends)")
    if not days:
        raise ValueError("days can't be empty")
    return frozenset(days)


def days_label(days: frozenset[int]) -> str | list[str]:
    for name, group in DAY_GROUPS.items():
        if days == group:
            return name
    return [DAYS[d] for d in sorted(days)]


def parse_clock(value: Any, *, allow_24: bool = False) -> str:
    """``"HH:MM"`` -> normalized ``"HH:MM"``.

    YAML 1.1 reads an unquoted ``18:30`` as the base-60 integer 1110, so integers are taken as
    minutes past midnight.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        hours, minutes = divmod(value, 60)
    else:
        match = _TIME.match(str(value).strip())
        if not match:
            raise ValueError(f"{value!r} is not a time (use HH:MM)")
        hours, minutes = int(match[1]), int(match[2])
    if minutes > 59 or hours > 24 or (hours == 24 and (minutes or not allow_24)):
        raise ValueError(f"{value!r} is not a time (use HH:MM)")
    return f"{hours:02d}:{minutes:02d}"


def normalize_target(value: Any) -> str:
    """An app id, or ``off``. YAML 1.1 reads an unquoted ``off`` as False, so that means off too."""
    if value is False:
        return OFF
    text = str(value).strip()
    return OFF if text.lower() == OFF else text


def _clock(text: str) -> time:
    hours, minutes = (int(p) for p in text.split(":"))
    return time(0, 0) if hours == 24 else time(hours, minutes)


class Window(BaseModel):
    """One schedule entry: show ``module`` on ``days`` from ``start`` to ``end`` (local time)."""

    model_config = ConfigDict(extra="forbid")

    module: str
    days: str | list[str] = "daily"
    start: str
    end: str
    brightness: int | None = Field(None, ge=1, le=100)  # overrides the panel brightness while active

    _days: frozenset[int] = PrivateAttr()
    _start: time = PrivateAttr()
    _end: time = PrivateAttr()

    @field_validator("module", mode="before")
    @classmethod
    def _module(cls, value: Any) -> str:
        return normalize_target(value)

    @field_validator("days", mode="before")
    @classmethod
    def _days_field(cls, value: Any) -> str | list[str]:
        return days_label(parse_days(value))

    @field_validator("start", mode="before")
    @classmethod
    def _start_field(cls, value: Any) -> str:
        return parse_clock(value)

    @field_validator("end", mode="before")
    @classmethod
    def _end_field(cls, value: Any) -> str:
        return parse_clock(value, allow_24=True)

    def model_post_init(self, __context: Any) -> None:
        self._days = parse_days(self.days)
        self._start = _clock(self.start)
        self._end = _clock(self.end)

    @property
    def weekdays(self) -> frozenset[int]:
        return self._days

    @property
    def span(self) -> str:
        return f"{self.start}–{self.end}"

    def occurrences(self, tz: ZoneInfo, first: date, last: date) -> Iterator[tuple[datetime, datetime]]:
        """(start, end) in UTC for every occurrence starting on a day in [first, last]."""
        day = first
        while day <= last:
            if day.weekday() in self._days:
                end_day = day if self._end > self._start else day + timedelta(days=1)
                yield _at(day, self._start, tz), _at(end_day, self._end, tz)
            day += timedelta(days=1)


def _at(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz).astimezone(timezone.utc)


def _local_day(now: datetime, tz: ZoneInfo) -> date:
    return now.astimezone(tz).date()


def active_occurrence(window: Window, now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    today = _local_day(now, tz)
    for start, end in window.occurrences(tz, today - timedelta(days=1), today):
        if start <= now < end:
            return start, end
    return None


def check_schedule(windows: Sequence[Window], default: str | None, modules: Mapping[str, Any]) -> None:
    """Raise ValueError if a window (or the default) names an app that can't go on the panel."""

    def check(module: str, where: str) -> None:
        if module == OFF:
            return
        spec = modules.get(module)
        if spec is None:
            raise ValueError(f"{where}: there's no app called {module!r}")
        if getattr(spec, "display", None) is None:
            raise ValueError(f"{where}: {module!r} has no display.frame, so it can't go on the panel")

    for number, window in enumerate(windows, 1):
        check(window.module, f"schedule window {number}")
    if default is not None:
        check(default, "default")


# ------------------------------------------------------------------ resolving


@dataclass(frozen=True)
class Override:
    """Show now: ``module`` (or ``off``) holds the panel until ``until`` (None = until resumed)."""

    module: str
    until: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.until is None or now < self.until


@dataclass(frozen=True)
class Decision:
    module: str | None  # the app on the panel; None = dark
    off: bool  # dark on purpose (an off window, default off, or Show now: off)
    brightness: int
    source: str  # "override" | "window" | "default" | "none"
    window: int | None  # index of the window in charge
    reason: str
    next_change: datetime | None  # next window start or end
    skipped: tuple[str, ...] = ()  # apps passed over because they were down


def next_change(windows: Sequence[Window], now: datetime, tz: ZoneInfo) -> datetime | None:
    """The next instant any window starts or ends."""
    today = _local_day(now, tz)
    best: datetime | None = None
    for window in windows:
        for start, end in window.occurrences(tz, today - timedelta(days=1), today + timedelta(days=HORIZON_DAYS)):
            for edge in (start, end):
                if edge > now and (best is None or edge < best):
                    best = edge
    return best


def resolve(
    now: datetime,
    windows: Sequence[Window],
    default: str,
    healthy: Collection[str] | None,
    tz: ZoneInfo,
    brightness: int,
    override: Override | None = None,
) -> Decision:
    """What the panel shows at ``now``. ``healthy=None`` treats every app as healthy."""

    def ok(module: str) -> bool:
        return healthy is None or module in healthy

    upcoming = next_change(windows, now, tz)
    skipped: list[str] = []
    if override is not None and override.active(now):
        if override.module == OFF:
            return Decision(None, True, brightness, "override", None, "Show now", upcoming)
        if ok(override.module):
            return Decision(override.module, False, brightness, "override", None, "Show now", upcoming)
        skipped.append(override.module)

    for index, window in enumerate(windows):
        if active_occurrence(window, now, tz) is None:
            continue
        reason = f"window {index + 1} · {window.span}"
        level = window.brightness or brightness
        if window.module == OFF:
            return Decision(None, True, level, "window", index, reason, upcoming, tuple(skipped))
        if ok(window.module):
            return Decision(window.module, False, level, "window", index, reason, upcoming, tuple(skipped))
        skipped.append(window.module)

    if default == OFF:
        return Decision(None, True, brightness, "default", None, "default", upcoming, tuple(skipped))
    if ok(default):
        return Decision(default, False, brightness, "default", None, "default", upcoming, tuple(skipped))
    skipped.append(default)
    return Decision(None, False, brightness, "none", None, "no app is healthy", upcoming, tuple(skipped))


# ------------------------------------------------------------------ which apps should be running


def _candidates(when: datetime, windows: Sequence[Window], default: str, tz: ZoneInfo) -> set[str]:
    """Apps that could be on the panel at ``when``: every active window's app above the first
    active ``off`` window (lower ones can still take over if a higher app is down), or the
    default when no window is active."""
    apps: set[str] = set()
    any_active = False
    for window in windows:
        if active_occurrence(window, when, tz) is None:
            continue
        any_active = True
        if window.module == OFF:
            return apps
        apps.add(window.module)
    if not any_active and default != OFF:
        apps.add(default)
    return apps


def wanted(
    now: datetime,
    windows: Sequence[Window],
    default: str,
    tz: ZoneInfo,
    warmup: Mapping[str, float],
    override: Override | None = None,
) -> set[str]:
    """Of the apps in ``warmup`` (the ``process: scheduled`` ones, with their lead times), those
    that should be running: they could be on the panel now or within their warmup."""
    current = _candidates(now, windows, default, tz)
    result = {
        module
        for module, lead in warmup.items()
        if module in current or module in _candidates(now + timedelta(seconds=lead), windows, default, tz)
    }
    if override is not None and override.active(now) and override.module in warmup:
        result.add(override.module)
    return result


def next_wanted_change(
    module: str,
    now: datetime,
    windows: Sequence[Window],
    default: str,
    tz: ZoneInfo,
    lead: float,
) -> datetime | None:
    """When the schedule next flips whether ``module`` should be running (Show now aside)."""
    lead_delta = timedelta(seconds=lead)

    def is_wanted(when: datetime) -> bool:
        return module in _candidates(when, windows, default, tz) or module in _candidates(
            when + lead_delta, windows, default, tz
        )

    current = is_wanted(now)
    today = _local_day(now, tz)
    edges: set[datetime] = set()
    for window in windows:
        for start, end in window.occurrences(tz, today - timedelta(days=1), today + timedelta(days=HORIZON_DAYS)):
            edges.update(edge for edge in (start, end, start - lead_delta, end - lead_delta) if edge > now)
    for edge in sorted(edges):
        if is_wanted(edge) != current:
            return edge
    return None


# ------------------------------------------------------------------ a day at a glance


def timeline(day: date, windows: Sequence[Window], default: str, tz: ZoneInfo) -> list[dict[str, Any]]:
    """The panel's plan for one local day, assuming every app is healthy.

    Each segment has ISO ``start``/``end`` and ``from``/``to`` as fractions of the day (a DST
    day is 23 or 25 hours long, so fractions are the safe way to draw it).
    """
    day_start, day_end = _at(day, time(0), tz), _at(day + timedelta(days=1), time(0), tz)
    edges = {day_start, day_end}
    for window in windows:
        for start, end in window.occurrences(tz, day - timedelta(days=1), day + timedelta(days=1)):
            edges.update(edge for edge in (start, end) if day_start < edge < day_end)
    points = sorted(edges)
    total = (day_end - day_start).total_seconds()

    segments: list[dict[str, Any]] = []
    for begin, finish in zip(points, points[1:]):
        decision = resolve(begin, windows, default, None, tz, 0)
        key = (decision.module, decision.off, decision.window)
        if segments and segments[-1]["key"] == key:
            segments[-1]["end"] = finish
            continue
        segments.append({"key": key, "start": begin, "end": finish, "decision": decision})

    return [
        {
            "start": seg["start"].astimezone(tz).isoformat(),
            "end": seg["end"].astimezone(tz).isoformat(),
            "from": round((seg["start"] - day_start).total_seconds() / total, 6),
            "to": round((seg["end"] - day_start).total_seconds() / total, 6),
            "module": seg["decision"].module,
            "off": seg["decision"].off,
            "window": seg["decision"].window,
        }
        for seg in segments
    ]
