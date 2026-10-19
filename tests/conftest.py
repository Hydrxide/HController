from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from hcontroller.modules.manifest import ModuleSpec

ROOT = Path(__file__).resolve().parent.parent
TESTPATTERN = ROOT / "tools" / "testpattern.py"

# Runner timings shrunk so supervisor tests take seconds, not minutes.
FAST = dict(backoff_base=0.2, backoff_max=1.0, stable_after=60.0, health_interval=0.2, start_grace=10.0, conflict_retry=0.3)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pattern_spec(port: int, *flags: str, **fields) -> ModuleSpec:
    """A real child process (tools/testpattern.py) serving /health and /render/frame on ``port``."""
    return ModuleSpec(
        command=[sys.executable, str(TESTPATTERN), "--port", str(port), *flags],
        url=f"http://127.0.0.1:{port}",
        health="/health",
        display={},
        **fields,
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


def wait_sync(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(interval)


@pytest.fixture
async def http():
    async with httpx.AsyncClient(timeout=2.0) as client:
        yield client
