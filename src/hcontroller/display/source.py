"""Pulls frames from whichever app the scheduler put on the panel, at a steady rate.

Apps render server-side (nhlscreen's ``GET /render/frame``: raw RGB, size in X-Width/X-Height),
so their frames already include anything they animate, like nhlscreen's counting-down clock.
A failed fetch keeps the last good frame for ``GRACE`` seconds before going black, so one slow
response doesn't flash the panel.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import httpx

from ..modules.health import describe
from ..modules.runner import HttpGetter
from .hub import FrameHub

log = logging.getLogger(__name__)


class FrameError(Exception):
    pass


class FrameSource:
    GRACE = 2.0  # seconds to keep the last good frame through fetch errors
    KEEPALIVE = 1.0  # resend an unchanged frame this often so the LED driver knows we're alive

    def __init__(self, hub: FrameHub, *, http: HttpGetter, fps: float) -> None:
        self.hub = hub
        self.http = http
        self.set_fps(fps)
        self.black = bytes(hub.width * hub.height * 3)
        self.module: str | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.on_error: Callable[[], None] | None = None  # called when ``error`` changes
        self._last_good = 0.0

    def set_fps(self, fps: float) -> None:
        """Frames fetched per second; takes effect from the next frame."""
        self.fps = fps
        self.interval = 1.0 / fps

    def set_target(self, module: str | None, url: str | None) -> None:
        if (module, url) == (self.module, self.url):
            return
        self.module, self.url = module, url
        self._last_good = 0.0
        self._set_error(None)
        if url is None:
            self._show(self.black)

    async def run(self) -> None:
        while True:
            began = time.monotonic()
            try:
                await self.tick()
            except Exception:
                log.exception("frame source failed")
            await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - began)))

    async def tick(self) -> None:
        url = self.url
        if url is None:
            self._show(self.black)
            return
        try:
            frame = await self._fetch(url)
        except FrameError as exc:
            if url != self.url:  # the scheduler switched apps mid-fetch
                return
            self._set_error(str(exc))
            if time.monotonic() - self._last_good > self.GRACE:
                self._show(self.black)
            return
        if url != self.url:
            return
        self._last_good = time.monotonic()
        self._set_error(None)
        self._show(frame)

    async def _fetch(self, url: str) -> bytes:
        try:
            response = await self.http().get(url, timeout=1.0)
        except httpx.HTTPError as exc:
            raise FrameError(f"no frame: {describe(exc)}") from exc
        if response.status_code != 200:
            raise FrameError(f"frame request returned {response.status_code}")
        try:
            width, height = int(response.headers["X-Width"]), int(response.headers["X-Height"])
        except (KeyError, ValueError) as exc:
            raise FrameError("frame response has no X-Width / X-Height headers") from exc
        if (width, height) != (self.hub.width, self.hub.height):
            raise FrameError(f"the app sends {width}×{height} frames; the panel is {self.hub.width}×{self.hub.height}")
        content = response.content
        if len(content) != width * height * 3:
            raise FrameError(f"frame has {len(content)} bytes, expected {width * height * 3}")
        return content

    def _show(self, frame: bytes) -> None:
        hub = self.hub
        if (
            frame == hub.frame
            and hub.module == self.module
            and time.monotonic() - hub.published_at < self.KEEPALIVE
        ):
            return
        hub.module = self.module
        hub.publish(frame)

    def _set_error(self, error: str | None) -> None:
        if error != self.error:
            self.error = error
            if error:
                log.warning("panel frame from %s: %s", self.module, error)
            if self.on_error:
                self.on_error()
