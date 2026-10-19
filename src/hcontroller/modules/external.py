"""ExternalRunner: an app HController watches but doesn't run (on another device, or started
some other way). It can still be shown on the panel; start/stop/restart don't apply."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from .health import probe
from .manifest import ModuleSpec
from .runner import HttpGetter, LogBuffer, NotSupported


class ExternalRunner:
    kind = "external"

    def __init__(
        self,
        module_id: str,
        spec: ModuleSpec,
        *,
        http: HttpGetter,
        on_change: Callable[[Any], None],
        interval: float = 5.0,
    ) -> None:
        self.id = module_id
        self.spec = spec
        self.http = http
        self.on_change = on_change
        self.interval = interval
        self.log = logging.getLogger(f"[{module_id}]")
        self.logs = LogBuffer()
        self.state = "checking"
        self.message: str | None = None
        self.desired = False
        self._task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "pid": None,
            "started_at": None,
            "uptime": None,
            "restart_count": 0,
            "last_exit": None,
            "next_restart_at": None,
        }

    def watch(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._monitor(), name=f"watch {self.id}")

    async def unwatch(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _monitor(self) -> None:
        failures = 0
        while True:
            ok, why = await probe(self.http(), self.spec)
            if ok:
                failures = 0
                self._set("external-up")
            else:
                failures += 1
                if failures >= 2 or self.state == "checking":
                    self._set("external-down", why)
            await asyncio.sleep(self.interval)

    def _set(self, state: str, message: str | None = None) -> None:
        if (state, message) == (self.state, self.message):
            return
        if state != self.state:
            text = "answering" if state == "external-up" else f"not answering: {message}"
            self.logs.append(text, source="hcontroller")
            self.log.info("%s", text)
        self.state, self.message = state, message
        self.on_change(self)

    def _refuse(self) -> NotSupported:
        return NotSupported(f"{self.id} runs outside HController (it only has a url), so it can't be started or stopped here")

    def start(self) -> None:
        raise self._refuse()

    async def stop(self) -> None:
        raise self._refuse()

    async def restart(self) -> None:
        raise self._refuse()
