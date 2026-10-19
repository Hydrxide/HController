"""The panel's current frame, fanned out to every viewer: dashboard previews and the LED driver.

Stream messages (``WS /display/stream``) are binary: a 5-byte header (width and height as
big-endian uint16, then brightness 1-100 as a byte) followed by width*height*3 RGB bytes.
"""

from __future__ import annotations

import asyncio
import struct
import time

from ..events import offer

HEADER = struct.Struct(">HHB")


def encode_frame(width: int, height: int, brightness: int, rgb: bytes) -> bytes:
    return HEADER.pack(width, height, brightness) + rgb


def decode_frame(message: bytes) -> tuple[int, int, int, bytes]:
    if len(message) < HEADER.size:
        raise ValueError("frame message is too short")
    width, height, brightness = HEADER.unpack_from(message)
    rgb = message[HEADER.size :]
    if len(rgb) != width * height * 3:
        raise ValueError(f"frame message has {len(rgb)} RGB bytes, expected {width * height * 3}")
    return width, height, brightness, rgb


class FrameHub:
    def __init__(self, width: int, height: int, brightness: int = 70) -> None:
        self.width = width
        self.height = height
        self.frame = bytes(width * height * 3)
        self.brightness = brightness
        self.module: str | None = None  # whose frame this is
        self.published_at = 0.0  # monotonic
        self._subscribers: set[asyncio.Queue] = set()

    def message(self) -> bytes:
        return encode_frame(self.width, self.height, self.brightness, self.frame)

    def publish(self, frame: bytes) -> None:
        self.frame = frame
        self.published_at = time.monotonic()
        message = self.message()
        for queue in list(self._subscribers):
            offer(queue, message)

    def set_brightness(self, value: int) -> None:
        if value != self.brightness:
            self.brightness = value
            self.publish(self.frame)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=3)
        queue.put_nowait(self.message())
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
