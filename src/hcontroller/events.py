"""Fan-out of live updates (module status, panel decisions) to every open dashboard."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


def offer(queue: asyncio.Queue, item: Any) -> None:
    """Put without blocking; a slow subscriber loses its oldest item instead of stalling everyone."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(item)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            offer(queue, message)

    def subscribe(self, maxsize: int = 200) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
