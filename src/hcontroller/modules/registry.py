"""Every module's runner, and keeping each one running or stopped as it should be.

Whether a local module should run:
- ``process: always``: ``autostart``, or the last Start/Stop pressed on the dashboard (saved).
- ``process: scheduled``: while the panel scheduler wants it (its window, plus warmup), unless a
  Start/Stop from the dashboard holds it until its next scheduled start or stop.

The registry acts only when that answer changes, so a module that exits on its own under its
restart policy stays stopped until someone (or the schedule) starts it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ..events import EventBus
from ..state import State
from .external import ExternalRunner
from .local import LocalRunner
from .manifest import ModuleSpec, is_loopback
from .runner import HEALTHY, HttpGetter, utcnow

log = logging.getLogger(__name__)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class Registry:
    def __init__(
        self,
        specs: dict[str, ModuleSpec],
        state: State,
        bus: EventBus,
        *,
        http: HttpGetter,
        runner_options: dict[str, Any] | None = None,
    ) -> None:
        self.specs = specs
        self.state = state
        self.bus = bus
        self.runners: dict[str, LocalRunner | ExternalRunner] = {}
        for module_id, spec in specs.items():
            if spec.local:
                self.runners[module_id] = LocalRunner(
                    module_id, spec, http=http, on_change=self._changed, **(runner_options or {})
                )
            else:
                self.runners[module_id] = ExternalRunner(module_id, spec, http=http, on_change=self._changed)
        self.wanted: set[str] = set()  # scheduled apps the panel scheduler wants running
        self.listeners: list[Callable[[], None]] = []  # told about every status change
        self._poke = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ status

    def _changed(self, runner: LocalRunner | ExternalRunner) -> None:
        self.bus.publish({"type": "module", "module": self.status(runner.id)})
        for listener in self.listeners:
            listener()

    def healthy(self) -> set[str]:
        return {module_id for module_id, runner in self.runners.items() if runner.state in HEALTHY}

    def status(self, module_id: str) -> dict[str, Any]:
        runner = self.runners[module_id]
        spec = runner.spec
        hold = self._hold(module_id, utcnow()) if spec.process == "scheduled" else None
        ui = None
        if spec.ui and spec.url:
            host, port = spec.address() or ("", 0)
            ui = {
                "url": spec.endpoint(spec.ui),
                "scheme": urlsplit(spec.url).scheme,
                "port": port,
                "path": spec.ui,
                "loopback": is_loopback(host),
            }
        return {
            "id": module_id,
            "name": spec.name or module_id,
            "kind": runner.kind,
            "process": spec.process if spec.local else None,
            "restart_policy": spec.restart if spec.local else None,
            **runner.status(),
            "desired": runner.desired,
            "hold": {"running": hold["running"], "until": hold["until"].isoformat() if hold["until"] else None}
            if hold
            else None,
            "url": spec.url,
            "ui": ui,
            "display": spec.display is not None,
            "config": spec.config_file is not None,
        }

    def statuses(self) -> list[dict[str, Any]]:
        return [self.status(module_id) for module_id in self.runners]

    # ------------------------------------------------------------------ desired state

    def _hold(self, module_id: str, now: datetime) -> dict[str, Any] | None:
        hold = (self.state.get("holds") or {}).get(module_id)
        if not isinstance(hold, dict):
            return None
        until = _parse_time(hold.get("until"))
        if until is not None and now >= until:
            return None
        return {"running": bool(hold.get("running")), "until": until}

    def desired(self, module_id: str, now: datetime) -> bool:
        spec = self.specs[module_id]
        if not spec.local:
            return False
        if spec.process == "always":
            return bool((self.state.get("running") or {}).get(module_id, spec.autostart))
        hold = self._hold(module_id, now)
        if hold is not None:
            return hold["running"]
        return module_id in self.wanted

    def set_wanted(self, wanted: set[str]) -> None:
        if wanted != self.wanted:
            self.wanted = set(wanted)
            self.poke()

    def set_running(self, module_id: str, running: bool) -> None:
        """Remember a dashboard Start/Stop for a ``process: always`` module."""
        choices = self.state.get("running") or {}
        choices[module_id] = running
        self.state.set("running", choices)

    def set_hold(self, module_id: str, running: bool, until: datetime | None) -> None:
        """Hold a ``process: scheduled`` module running/stopped until ``until`` (None = indefinitely)."""
        holds = self.state.get("holds") or {}
        holds[module_id] = {"running": running, "until": until.isoformat() if until else None}
        self.state.set("holds", holds)

    def _expire_holds(self, now: datetime) -> None:
        holds = self.state.get("holds") or {}
        live = {mid: hold for mid, hold in holds.items() if mid in self.specs and self._hold(mid, now)}
        if live != holds:
            self.state.set("holds", live or None)

    async def reconcile(self) -> None:
        now = utcnow()
        self._expire_holds(now)
        stopping = []
        for module_id, runner in self.runners.items():
            if not isinstance(runner, LocalRunner):
                continue
            want = self.desired(module_id, now)
            if want == runner.desired:
                continue
            runner.desired = want
            if want:
                runner.start()
            else:
                stopping.append(runner.stop())
        if stopping:
            await asyncio.gather(*stopping)

    def poke(self) -> None:
        self._poke.set()

    # ------------------------------------------------------------------ lifecycle

    async def _loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._poke.wait(), 2)
            self._poke.clear()

    async def start(self) -> None:
        for runner in self.runners.values():
            if isinstance(runner, ExternalRunner):
                runner.watch()
        await self.reconcile()
        self._task = asyncio.create_task(self._loop(), name="registry")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await asyncio.gather(
            *(
                runner.stop() if isinstance(runner, LocalRunner) else runner.unwatch()
                for runner in self.runners.values()
            )
        )
