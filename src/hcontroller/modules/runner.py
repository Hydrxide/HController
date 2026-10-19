"""What every runner offers, whatever runs the module (a local process, or nothing for an
external app). A future RemoteRunner (HController agent on another device) fits the same shape."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from .manifest import ModuleSpec

HEALTHY = frozenset({"healthy", "external-up"})

HttpGetter = Callable[[], httpx.AsyncClient]


class NotSupported(Exception):
    """The action doesn't apply to this module (e.g. starting an app HController only watches)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LogBuffer:
    """The last ``size`` output lines, numbered so a viewer can ask for just the new ones."""

    def __init__(self, size: int = 2000) -> None:
        self._lines: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = 0

    def append(self, text: str, source: str = "app") -> None:
        self._seq += 1
        self._lines.append({"seq": self._seq, "time": utcnow().isoformat(), "source": source, "text": text})

    def tail(self, lines: int = 200, after: int | None = None) -> list[dict[str, Any]]:
        items = [line for line in self._lines if after is None or line["seq"] > after]
        return items[-lines:] if lines > 0 else []

    @property
    def last_seq(self) -> int:
        return self._seq


class Runner(Protocol):
    id: str
    spec: ModuleSpec
    kind: str
    state: str
    message: str | None
    desired: bool
    logs: LogBuffer

    def status(self) -> dict[str, Any]: ...
    def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def restart(self) -> None: ...
