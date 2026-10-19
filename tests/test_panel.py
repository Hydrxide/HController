"""The LED driver's loop, with a fake matrix in place of rpi-rgb-led-matrix."""

import asyncio
import contextlib

from conftest import wait_for
from PIL import Image
from websockets.asyncio.server import serve

from hcontroller.display.hub import encode_frame
from hcontroller.panel.driver import Panel, run


class FakeCanvas:
    def __init__(self):
        self.images = []
        self.brightness = None

    def SetImage(self, image, x, y):  # noqa: N802 (the library's names)
        self.images.append((image.size, image.getpixel((1, 0))))


class FakeMatrix:
    def __init__(self):
        self.canvas = FakeCanvas()
        self.brightness = None
        self.swaps = 0
        self.cleared = 0

    def CreateFrameCanvas(self):  # noqa: N802
        return self.canvas

    def SwapOnVSync(self, canvas):  # noqa: N802
        self.swaps += 1
        return canvas

    def Clear(self):  # noqa: N802
        self.cleared += 1


async def test_draws_frames_then_blanks_when_the_stream_goes_quiet():
    rgb = bytes([0, 0, 0, 10, 20, 30]) + bytes(18)  # pixel (1, 0) is (10, 20, 30)

    async def handler(ws):
        await ws.send(encode_frame(4, 2, 55, rgb))
        await ws.send("ignored text message")
        await ws.send(encode_frame(4, 2, 55, rgb))
        await ws.wait_closed()  # then go quiet

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        matrix = FakeMatrix()
        task = asyncio.create_task(run(f"ws://127.0.0.1:{port}", Panel(matrix, Image), stale_after=0.3))
        try:
            await wait_for(lambda: matrix.swaps >= 2)
            assert matrix.canvas.images[0] == ((4, 2), (10, 20, 30))
            assert matrix.brightness == 55 and matrix.canvas.brightness == 55
            await wait_for(lambda: matrix.cleared == 1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def test_keeps_retrying_while_hcontroller_is_down():
    matrix = FakeMatrix()
    attempts = []

    def connect(url, **_):
        attempts.append(url)
        raise OSError("connection refused")

    task = asyncio.create_task(run("ws://127.0.0.1:9/display/stream", Panel(matrix, Image), connect=connect))
    try:
        await wait_for(lambda: len(attempts) >= 2, timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert matrix.swaps == 0
