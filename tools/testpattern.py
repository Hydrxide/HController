#!/usr/bin/env python3
"""A stand-in HController module for tests and demos (standard library only).

Serves ``/health`` and an animated test pattern at ``/render/frame`` (raw RGB with
X-Width/X-Height headers, like nhlscreen), so it can be scheduled on the panel:

    python tools/testpattern.py --port 9200 --color 00aaff

Flags make it misbehave on purpose: --crash-after, --exit-after, --ignore-sigterm, --slow-start.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_frame(width: int, height: int, color: tuple[int, int, int], now: float) -> bytes:
    """A dim background in ``color``, a 2px border, and a white bar sweeping left to right."""
    sweep = int(now * 32) % width
    dim = bytes(c // 5 for c in color)
    bright, white = bytes(color), b"\xff\xff\xff"
    rows = []
    for y in range(height):
        edge_row = y < 2 or y >= height - 2
        row = bytearray(dim * width)
        for x in range(width):
            if edge_row or x < 2 or x >= width - 2:
                row[x * 3 : x * 3 + 3] = bright
            elif abs(x - sweep) <= 1:
                row[x * 3 : x * 3 + 3] = white
        rows.append(bytes(row))
    return b"".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9200)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
    parser.add_argument("--color", default="ff8800", help="hex RGB (default ff8800)")
    parser.add_argument("--crash-after", type=float, help="exit with code 3 after this many seconds")
    parser.add_argument("--exit-after", type=float, help="exit cleanly (code 0) after this many seconds")
    parser.add_argument("--ignore-sigterm", action="store_true", help="ignore SIGTERM (to test SIGKILL fallback)")
    parser.add_argument("--slow-start", type=float, default=0, help="wait this long before listening")
    parser.add_argument("--unhealthy", action="store_true", help="/health returns 503")
    args = parser.parse_args(argv)

    color = tuple(int(args.color[i : i + 2], 16) for i in (0, 2, 4))
    if args.ignore_sigterm:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for seconds, code in ((args.crash_after, 3), (args.exit_after, 0)):
        if seconds is not None:
            timer = threading.Timer(seconds, lambda code=code: (print(f"exiting with {code}", flush=True), os._exit(code)))
            timer.daemon = True
            timer.start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                body = json.dumps({"ok": not args.unhealthy}).encode()
                self._send(503 if args.unhealthy else 200, "application/json", body)
            elif self.path == "/render/frame":
                body = make_frame(args.width, args.height, color, time.time())
                self._send(200, "application/octet-stream", body, {"X-Width": args.width, "X-Height": args.height})
            elif self.path == "/":
                self._send(200, "text/html", f"<h1>testpattern #{args.color}</h1>".encode())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, status: int, kind: str, body: bytes, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # one line per request would flood the logs
            pass

    if args.slow_start:
        print(f"starting in {args.slow_start:g}s", flush=True)
        time.sleep(args.slow_start)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"testpattern #{args.color} on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
