"""hcontroller-panel: draws HController's frame stream on the HUB75 LED matrix.

A separate process because the matrix library needs root for GPIO (and then drops privileges);
HController and its modules never run as root. It connects to ``WS /display/stream``, draws
every frame with rpi-rgb-led-matrix, and blanks the panel if the stream goes quiet.

    sudo .venv/bin/hcontroller-panel                  # on the Pi
    .venv/bin/hcontroller-panel --emulate             # anywhere, via RGBMatrixEmulator

The defaults draw 128x64 on two chained 64x64 panels with the Adafruit HAT/Bonnet pin mapping;
the flags cover other panels and wiring.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from typing import Any

from ..display.hub import decode_frame

log = logging.getLogger("hcontroller.panel")


class Panel:
    """Holds the matrix and its offscreen canvas; double-buffered so frames never tear."""

    def __init__(self, matrix: Any, image_module: Any) -> None:
        self.matrix = matrix
        self.image = image_module
        self.canvas = matrix.CreateFrameCanvas()
        self.brightness: int | None = None
        self.blank = True

    def show(self, width: int, height: int, brightness: int, rgb: bytes) -> None:
        if brightness != self.brightness:
            self.brightness = brightness
            # Brightness is applied as pixels are written, so set it on the matrix and the canvas.
            for target in (self.matrix, self.canvas):
                with contextlib.suppress(AttributeError, TypeError):
                    target.brightness = brightness
        self.canvas.SetImage(self.image.frombytes("RGB", (width, height), rgb), 0, 0)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)
        self.blank = False

    def clear(self) -> None:
        if not self.blank:
            self.matrix.Clear()
            self.blank = True


async def run(url: str, panel: Panel, *, stale_after: float = 5.0, connect: Any = None) -> None:
    """Draw frames from ``url`` forever, reconnecting as needed."""
    if connect is None:
        from websockets.asyncio.client import connect
    delay = 1.0
    while True:
        try:
            async with connect(url, max_size=2**22, open_timeout=5) as ws:
                log.info("connected to %s", url)
                delay = 1.0
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), stale_after)
                    except asyncio.TimeoutError:
                        if not panel.blank:
                            log.warning("no frames for %gs; blanking the panel", stale_after)
                        panel.clear()
                        continue
                    if isinstance(message, str):
                        continue
                    try:
                        panel.show(*decode_frame(message))
                    except ValueError as exc:
                        log.warning("bad frame: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # connection refused, closed, HController restarting...
            log.warning("stream %s unavailable (%s); retrying in %gs", url, exc or type(exc).__name__, delay)
            panel.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


def build_matrix(args: argparse.Namespace) -> Any:
    if args.emulate:
        from RGBMatrixEmulator import RGBMatrix, RGBMatrixOptions
    else:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions
    options = RGBMatrixOptions()
    options.rows = args.rows
    options.cols = args.cols
    options.chain_length = args.chain
    options.parallel = args.parallel
    options.hardware_mapping = args.gpio_mapping
    options.gpio_slowdown = args.gpio_slowdown
    options.brightness = 70
    if args.no_hardware_pulse:
        options.disable_hardware_pulsing = True
    return RGBMatrix(options=options)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hcontroller-panel", description="Draws HController's frames on the LED matrix")
    parser.add_argument("--url", default="ws://127.0.0.1:1630/display/stream", help="HController's frame stream")
    parser.add_argument("--rows", type=int, default=64, help="rows per panel")
    parser.add_argument("--cols", type=int, default=64, help="columns per panel")
    parser.add_argument("--chain", type=int, default=2, help="panels chained together")
    parser.add_argument("--parallel", type=int, default=1, help="parallel chains")
    parser.add_argument("--gpio-mapping", default="adafruit-hat", help="adafruit-hat, adafruit-hat-pwm, regular, ...")
    parser.add_argument("--gpio-slowdown", type=int, default=2, help="2-4 on a Pi 4")
    parser.add_argument("--no-hardware-pulse", action="store_true", help="if onboard sound can't be disabled")
    parser.add_argument("--stale-after", type=float, default=5.0, help="blank the panel after this long without frames")
    parser.add_argument("--emulate", action="store_true", help="draw with RGBMatrixEmulator instead of the real panel")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        from PIL import Image
    except ImportError:
        parser.exit(2, "hcontroller-panel needs Pillow: pip install -e '.[panel]'\n")
    try:
        matrix = build_matrix(args)
    except ImportError as exc:
        hint = "pip install -e '.[emulator]'" if args.emulate else "build rpi-rgb-led-matrix's Python bindings (see README)"
        parser.exit(2, f"hcontroller-panel: {exc}. {hint}\n")

    panel = Panel(matrix, Image)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.url, panel, stale_after=args.stale_after))
    panel.clear()


if __name__ == "__main__":
    main()
