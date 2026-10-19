"""Applies the schedule. Once a second, and whenever an app's health changes, it decides what the
panel shows, points the frame source at that app, and tells the registry which
``process: scheduled`` apps should be running.

The schedule lives in hcontroller.yaml; one edited on the dashboard is saved in the state file
and wins until "Reset to config" (the same pattern as nhlscreen's team picker).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from ..config import Config
from ..events import EventBus
from ..modules.registry import Registry
from ..modules.runner import utcnow
from ..state import State
from . import schedule as sched
from .hub import FrameHub
from .schedule import OFF, Decision, Override, Window, check_schedule
from .source import FrameSource

log = logging.getLogger(__name__)


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class DisplayScheduler:
    def __init__(
        self,
        config: Config,
        state: State,
        registry: Registry,
        source: FrameSource,
        hub: FrameHub,
        bus: EventBus,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self.tz = config.tz
        self.state = state
        self.registry = registry
        self.source = source
        self.hub = hub
        self.bus = bus
        self.now = now
        self.apps = [module_id for module_id, spec in config.modules.items() if spec.display]
        self.custom = False
        self.windows, self.default = self._load_schedule()
        self.override = self._load_override()
        self.brightness = self._load_brightness()
        self.decision: Decision | None = None

    # ------------------------------------------------------------------ saved choices

    def _load_schedule(self) -> tuple[list[Window], str]:
        saved = self.state.get("schedule")
        if saved is not None:
            try:
                windows = [Window.model_validate(item) for item in saved.get("windows", [])]
                default = str(saved.get("default", self.config.default))
                default = OFF if default.lower() == OFF else default
                check_schedule(windows, default, self.config.modules)
            except (ValueError, TypeError, AttributeError) as exc:
                log.warning("ignoring the schedule saved in %s (%s); using hcontroller.yaml's", self.state.path, exc)
            else:
                self.custom = True
                return windows, default
        return list(self.config.display.schedule), self.config.default

    def _load_override(self) -> Override | None:
        saved = self.state.get("override")
        if not isinstance(saved, dict):
            return None
        module, until = saved.get("module"), _parse_time(saved.get("until"))
        if (module != OFF and module not in self.apps) or (saved.get("until") and until is None):
            log.warning("ignoring saved Show now %r", saved)
            return None
        return Override(module, until)

    def _load_brightness(self) -> int:
        value = self.state.get("brightness")
        if isinstance(value, int) and 1 <= value <= 100:
            return value
        return self.config.display.brightness

    def warmups(self) -> dict[str, float]:
        return {
            module_id: spec.warmup_seconds
            for module_id, spec in self.config.modules.items()
            if spec.local and spec.process == "scheduled"
        }

    # ------------------------------------------------------------------ the loop

    def tick(self) -> None:
        now = self.now()
        if self.override is not None and not self.override.active(now):
            log.info("Show now (%s) ended; back to the schedule", self.override.module)
            self.override = None
            self.state.set("override", None)
        healthy = self.registry.healthy() & set(self.apps)
        decision = sched.resolve(now, self.windows, self.default, healthy, self.tz, self.brightness, self.override)
        self.registry.set_wanted(sched.wanted(now, self.windows, self.default, self.tz, self.warmups(), self.override))
        if decision == self.decision:
            return
        previous, self.decision = self.decision, decision
        if previous is None or (previous.module, previous.off) != (decision.module, decision.off):
            log.info("panel: %s (%s)", self.label(decision.module, decision.off), decision.reason)
        spec = self.config.modules.get(decision.module) if decision.module else None
        url = spec.endpoint(spec.display.frame) if spec and spec.display else None
        self.source.set_target(decision.module, url)
        self.hub.set_brightness(decision.brightness)
        self.publish()

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(1)

    def publish(self) -> None:
        self.bus.publish({"type": "display", "display": self.snapshot()})

    # ------------------------------------------------------------------ actions

    def show_now(self, module: str) -> None:
        module = OFF if module.lower() == OFF else module
        if module != OFF and module not in self.apps:
            raise ValueError(f"{module!r} can't go on the panel")
        until = sched.next_change(self.windows, self.now(), self.tz)
        self.override = Override(module, until)
        self.state.set("override", {"module": module, "until": until.isoformat() if until else None})
        log.info("Show now: %s until %s", module, until.astimezone(self.tz).strftime("%a %H:%M") if until else "resumed")
        self.tick()

    def resume(self) -> None:
        if self.override is not None:
            log.info("Show now (%s) cancelled; back to the schedule", self.override.module)
        self.override = None
        self.state.set("override", None)
        self.tick()

    def set_schedule(self, windows: Sequence[Window], default: str) -> None:
        default = OFF if default.lower() == OFF else default
        check_schedule(windows, default, self.config.modules)
        self.windows, self.default, self.custom = list(windows), default, True
        self.state.set(
            "schedule", {"windows": [window.model_dump() for window in self.windows], "default": default}
        )
        log.info("schedule saved: %d window(s), default %s", len(self.windows), default)
        self._schedule_changed()

    def reset_schedule(self) -> None:
        self.state.set("schedule", None)
        self.windows, self.default, self.custom = list(self.config.display.schedule), self.config.default, False
        log.info("schedule reset to hcontroller.yaml")
        self._schedule_changed()

    def _schedule_changed(self) -> None:
        self.tick()
        self.publish()
        self.bus.publish({"type": "schedule", "schedule": self.schedule_snapshot()})

    def set_brightness(self, value: int) -> None:
        if not 1 <= value <= 100:
            raise ValueError("brightness is 1-100")
        self.brightness = value
        self.state.set("brightness", value)
        self.tick()
        self.publish()

    def next_wanted_change(self, module: str) -> datetime | None:
        spec = self.config.modules[module]
        return sched.next_wanted_change(
            module, self.now(), self.windows, self.default, self.tz, spec.warmup_seconds
        )

    # ------------------------------------------------------------------ views

    def label(self, module: str | None, off: bool = False) -> str:
        if module is None:
            return "off" if off else "dark"
        spec = self.config.modules.get(module)
        return (spec.name if spec and spec.name else module) if spec else module

    def snapshot(self) -> dict[str, Any]:
        if self.decision is None:
            self.tick()
        decision = self.decision
        assert decision is not None
        upcoming = None
        if decision.next_change is not None:
            # Scheduled apps aren't running yet but will be by then (they start during warmup).
            healthy = (self.registry.healthy() | set(self.warmups())) & set(self.apps)
            after = sched.resolve(decision.next_change, self.windows, self.default, healthy, self.tz, self.brightness)
            upcoming = {
                "at": decision.next_change.astimezone(self.tz).isoformat(),
                "module": after.module,
                "off": after.off,
                "name": self.label(after.module, after.off),
            }
        override = self.override
        return {
            "module": decision.module,
            "name": self.label(decision.module, decision.off),
            "off": decision.off,
            "source": decision.source,
            "window": decision.window,
            "reason": decision.reason,
            "skipped": list(decision.skipped),
            "brightness": decision.brightness,
            "base_brightness": self.brightness,
            "override": {
                "module": override.module,
                "until": override.until.astimezone(self.tz).isoformat() if override.until else None,
            }
            if override
            else None,
            "next": upcoming,
            "frame_error": self.source.error,
            "width": self.hub.width,
            "height": self.hub.height,
            "fps": self.config.display.fps,
            "timezone": self.config.timezone,
        }

    def schedule_snapshot(self) -> dict[str, Any]:
        return {
            "windows": [window.model_dump() for window in self.windows],
            "default": self.default,
            "custom": self.custom,
            "timezone": self.config.timezone,
            "apps": [{"id": module_id, "name": self.label(module_id)} for module_id in self.apps],
        }

    def timeline(self, day: date | None = None) -> dict[str, Any]:
        now = self.now()
        today = now.astimezone(self.tz).date()
        day = day or today
        result: dict[str, Any] = {
            "date": day.isoformat(),
            "segments": sched.timeline(day, self.windows, self.default, self.tz),
            "now": None,
        }
        if day == today:
            # In UTC: subtracting two datetimes that share a ZoneInfo ignores DST.
            start = datetime.combine(day, time(0), tzinfo=self.tz).astimezone(timezone.utc)
            end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=self.tz).astimezone(timezone.utc)
            total = (end - start).total_seconds()
            result["now"] = round((now - start).total_seconds() / total, 6)
        return result
