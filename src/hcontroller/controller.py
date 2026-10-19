"""Wires the module registry, the panel scheduler and the frame pipeline together, and holds the
actions the API exposes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .display.hub import FrameHub
from .display.scheduler import DisplayScheduler
from .display.source import FrameSource
from .events import EventBus
from .modules import configfile
from .modules.local import LocalRunner
from .modules.registry import Registry
from .modules.runner import NotSupported, utcnow
from .state import State

log = logging.getLogger(__name__)


class UnknownModule(KeyError):
    pass


class Controller:
    def __init__(
        self,
        config: Config,
        *,
        state_path: Path | None = None,
        now: Callable[[], datetime] = utcnow,
        runner_options: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.state = State(state_path)
        self.bus = EventBus()
        self._http: httpx.AsyncClient | None = None
        self.registry = Registry(config.modules, self.state, self.bus, http=self.http, runner_options=runner_options)
        display = config.display
        self.hub = FrameHub(display.width, display.height, display.brightness)
        self.source = FrameSource(self.hub, http=self.http, fps=display.fps)
        self.scheduler = DisplayScheduler(
            config, self.state, self.registry, self.source, self.hub, self.bus, now=now
        )
        self.registry.listeners.append(self.scheduler.tick)
        self.source.on_error = self.scheduler.publish
        self._tasks: list[asyncio.Task] = []

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("the controller hasn't been started")
        return self._http

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=2.0)
        self.scheduler.tick()  # work out which scheduled apps are wanted before the first reconcile
        await self.registry.start()
        self._tasks = [
            asyncio.create_task(self.scheduler.run(), name="scheduler"),
            asyncio.create_task(self.source.run(), name="frames"),
        ]
        log.info("HController started with %d module(s)", len(self.config.modules))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self.registry.stop()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------ modules

    def runner(self, module_id: str):
        runner = self.registry.runners.get(module_id)
        if runner is None:
            raise UnknownModule(module_id)
        return runner

    def _local(self, module_id: str) -> LocalRunner:
        runner = self.runner(module_id)
        if not isinstance(runner, LocalRunner):
            raise NotSupported(f"{module_id} runs outside HController (it only has a url), so it can't be started or stopped here")
        return runner

    def _remember(self, runner: LocalRunner, running: bool) -> None:
        if runner.spec.process == "scheduled":
            self.registry.set_hold(runner.id, running, self.scheduler.next_wanted_change(runner.id))
        else:
            self.registry.set_running(runner.id, running)
        runner.desired = running

    async def start_module(self, module_id: str) -> dict[str, Any]:
        runner = self._local(module_id)
        self._remember(runner, True)
        runner.start()
        return self.registry.status(module_id)

    async def stop_module(self, module_id: str) -> dict[str, Any]:
        runner = self._local(module_id)
        self._remember(runner, False)
        await runner.stop()
        return self.registry.status(module_id)

    async def restart_module(self, module_id: str) -> dict[str, Any]:
        runner = self._local(module_id)
        if runner.active:
            await runner.restart()
            return self.registry.status(module_id)
        return await self.start_module(module_id)

    def module_logs(self, module_id: str, lines: int = 200, after: int | None = None) -> dict[str, Any]:
        runner = self.runner(module_id)
        return {"lines": runner.logs.tail(lines, after), "last": runner.logs.last_seq}

    # ------------------------------------------------------------------ module config files

    def _config_spec(self, module_id: str):
        spec = self.runner(module_id).spec
        if spec.config_file is None:
            raise NotSupported(f"{module_id} has no config_file")
        return spec

    def module_config(self, module_id: str) -> dict[str, Any]:
        return configfile.read(self._config_spec(module_id))

    async def save_module_config(self, module_id: str, text: str, restart: bool = False) -> dict[str, Any]:
        configfile.save(self._config_spec(module_id), text)
        log.info("saved %s's config", module_id)
        return await self._after_config_change(module_id, restart)

    async def revert_module_config(self, module_id: str, restart: bool = False) -> dict[str, Any]:
        configfile.revert(self._config_spec(module_id))
        log.info("restored %s's previous config", module_id)
        return await self._after_config_change(module_id, restart)

    async def _after_config_change(self, module_id: str, restart: bool) -> dict[str, Any]:
        runner = self.runner(module_id)
        restarted = False
        if restart and isinstance(runner, LocalRunner) and runner.active:
            await runner.restart()
            restarted = True
        return {**self.module_config(module_id), "restarted": restarted}
