"""LocalRunner: runs a module as a child process and keeps it running.

- Each module gets its own session / process group, so stopping it also stops anything it
  started, and Ctrl+C in HController's terminal doesn't reach the modules: HController stops
  them itself, gracefully (SIGTERM, then SIGKILL after ``stop_timeout``).
- Output (stdout + stderr) goes to a ring buffer for the dashboard and to HController's log.
- Restarts back off exponentially (1s .. 60s); the backoff resets after 60s of uptime.
- Before spawning, it checks the module's port. If something already answers there (a copy
  started by hand, or nhlscreen's own systemd unit), it reports a conflict instead of starting
  a second copy that would fail to bind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .health import describe, port_open, probe
from .manifest import ModuleSpec
from .runner import HttpGetter, LogBuffer, utcnow

MAX_LINE = 4000


def exit_text(code: int) -> str:
    if code < 0:
        with contextlib.suppress(ValueError):
            return f"was killed by {signal.Signals(-code).name}"
    return f"exited with code {code}"


class LocalRunner:
    kind = "local"

    def __init__(
        self,
        module_id: str,
        spec: ModuleSpec,
        *,
        http: HttpGetter,
        on_change: Callable[[Any], None],
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        stable_after: float = 60.0,
        health_interval: float = 5.0,
        start_grace: float = 60.0,
        conflict_retry: float = 5.0,
    ) -> None:
        self.id = module_id
        self.spec = spec
        self.http = http
        self.on_change = on_change
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.stable_after = stable_after
        self.health_interval = health_interval
        self.start_grace = start_grace
        self.conflict_retry = conflict_retry

        self.log = logging.getLogger(f"[{module_id}]")
        self.logs = LogBuffer()
        self.state = "stopped"
        self.message: str | None = None
        self.desired = False  # what the registry last asked for
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at: datetime | None = None
        self.restart_count = 0
        self.last_exit: int | None = None
        self.next_restart_at: datetime | None = None
        self._started = 0.0
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ public

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def active(self) -> bool:
        """Supervising: running, starting, or waiting to restart."""
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        running = self.running
        return {
            "state": self.state,
            "message": self.message,
            "pid": self.proc.pid if running and self.proc else None,
            "started_at": self.started_at.isoformat() if running and self.started_at else None,
            "uptime": round(time.monotonic() - self._started) if running else None,
            "restart_count": self.restart_count,
            "last_exit": self.last_exit,
            "next_restart_at": self.next_restart_at.isoformat()
            if self.state == "backoff" and self.next_restart_at
            else None,
        }

    def start(self) -> None:
        if self.active:
            return
        self._stopping = False
        self._wake.clear()
        self._task = asyncio.create_task(self._supervise(), name=f"module {self.id}")

    async def stop(self) -> None:
        task = self._task
        if task is None or task.done():
            self._task = None
            self._set("stopped", self.message if self.state == "crashed" else None)
            return
        self._stopping = True
        self._wake.set()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            await self._terminate(proc)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None
        self._stopping = False
        self._set("stopped")

    async def restart(self) -> None:
        await self.stop()
        self.start()

    # ------------------------------------------------------------------ supervision

    async def _supervise(self) -> None:
        delay = self.backoff_base
        try:
            while not self._stopping:
                failure = self._precheck()
                proc = None
                if failure is None:
                    if await self._port_taken():
                        host, port = self.spec.address() or ("?", 0)
                        self._set("conflict", f"something else is already answering on {host}:{port}; another copy?")
                        if await self._pause(self.conflict_retry):
                            break
                        continue
                    try:
                        proc = await self._spawn()
                    except OSError as exc:
                        failure = f"couldn't start: {describe(exc)}"
                if proc is None:
                    if self.spec.restart == "never":
                        self._note(failure or "couldn't start")
                        self._set("crashed", failure)
                        return
                    if await self._backoff(failure or "couldn't start", delay):
                        break
                    delay = min(delay * 2, self.backoff_max)
                    continue

                code, uptime = await self._run(proc)
                if self._stopping:
                    break
                if uptime >= self.stable_after:
                    delay = self.backoff_base
                reason = exit_text(code)
                if self.spec.restart == "never" or (self.spec.restart == "on-failure" and code == 0):
                    self._set("stopped" if code == 0 else "crashed", reason)
                    return
                self.restart_count += 1
                if await self._backoff(reason, delay):
                    break
                delay = min(delay * 2, self.backoff_max)
        except Exception:
            self.log.exception("supervisor failed")
            self._set("crashed", "HController's supervisor failed; see its log")
        finally:
            proc = self.proc
            if proc is not None and proc.returncode is None:  # cancelled while running
                self._signal(proc, hard=True)

    async def _run(self, proc: asyncio.subprocess.Process) -> tuple[int, float]:
        self.proc = proc
        self._started = time.monotonic()
        self.started_at = utcnow()
        self._note(f"started (pid {proc.pid}): {' '.join(self._argv())}")
        self._set("starting")
        assert proc.stdout is not None
        reader = asyncio.create_task(self._read_output(proc.stdout))
        health = asyncio.create_task(self._watch_health(proc))
        if self._stopping:
            await self._terminate(proc)
        code = await proc.wait()
        uptime = time.monotonic() - self._started
        health.cancel()
        self._reap_group(proc)
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(reader, 2)
        with contextlib.suppress(asyncio.CancelledError):
            await health
        self.last_exit = code
        if self._stopping:
            # Servers like uvicorn re-raise SIGTERM after shutting down cleanly: that's a normal stop.
            graceful = code in (0, -signal.SIGTERM)
            self._note("stopped" if graceful else f"stopped ({exit_text(code)})")
        else:
            self._note(f"{exit_text(code)} after {uptime:.0f}s")
        return code, uptime

    async def _backoff(self, reason: str, delay: float) -> bool:
        self.next_restart_at = utcnow() + timedelta(seconds=delay)
        self._note(f"{reason}; trying again in {delay:g}s")
        self._set("backoff", f"{reason}; trying again in {delay:g}s")
        return await self._pause(delay)

    async def _pause(self, seconds: float) -> bool:
        """Sleep, waking early if stop() is called. True = stop requested."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        return self._stopping

    async def _watch_health(self, proc: asyncio.subprocess.Process) -> None:
        spec = self.spec
        if spec.url is None:  # nothing to ask: running counts as healthy
            self._set("healthy")
            return
        started = time.monotonic()
        answered = False
        failures = 0
        while proc.returncode is None and not self._stopping:
            ok, why = await probe(self.http(), spec)
            if self._stopping or proc.returncode is not None:
                return
            if ok:
                failures, answered = 0, True
                self._set("healthy")
            else:
                failures += 1
                if answered and failures >= 2:
                    self._set("unhealthy", why)
                elif not answered and time.monotonic() - started > self.start_grace:
                    self._set("unhealthy", f"no healthy answer {self.start_grace:g}s after starting ({why})")
            await asyncio.sleep(self.health_interval if self.state == "healthy" else 1.0)

    # ------------------------------------------------------------------ processes

    def _argv(self) -> list[str]:
        argv = list(self.spec.command or [])
        program = argv[0]
        if self.spec.cwd and not os.path.isabs(program) and ("/" in program or os.sep in program):
            argv[0] = str(self.spec.cwd / program)
        return argv

    def _env(self) -> dict[str, str]:
        return {**os.environ, "PYTHONUNBUFFERED": "1", **self.spec.env}

    def _precheck(self) -> str | None:
        cwd = self.spec.cwd
        if cwd is not None and not cwd.is_dir():
            return f"cwd {cwd} doesn't exist"
        program = self._argv()[0]
        if "/" in program or os.sep in program:
            if not Path(program).exists():
                return f"{program} doesn't exist"
        elif shutil.which(program, path=self._env().get("PATH")) is None:
            return f"{program} isn't on PATH"
        return None

    async def _port_taken(self) -> bool:
        address = self.spec.address()
        return address is not None and await port_open(*address)

    async def _spawn(self) -> asyncio.subprocess.Process:
        if os.name == "nt":
            group: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            group = {"start_new_session": True}
        return await asyncio.create_subprocess_exec(
            *self._argv(),
            cwd=self.spec.cwd,
            env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **group,
        )

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        self._set("stopping")
        self._signal(proc, hard=False)
        try:
            await asyncio.wait_for(proc.wait(), self.spec.stop_timeout)
        except asyncio.TimeoutError:
            self._note(f"still running {self.spec.stop_timeout:g}s after SIGTERM; killing it")
            self._signal(proc, hard=True)
            await proc.wait()

    def _signal(self, proc: asyncio.subprocess.Process, *, hard: bool) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                (proc.kill if hard else proc.terminate)()
            else:
                os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            with contextlib.suppress(ProcessLookupError):
                (proc.kill if hard else proc.terminate)()

    def _reap_group(self, proc: asyncio.subprocess.Process) -> None:
        """Kill anything the module left behind in its process group, so a restart can bind its port."""
        if os.name == "nt":
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        self._note("killed processes it left behind")

    # ------------------------------------------------------------------ output

    async def _read_output(self, stream: asyncio.StreamReader) -> None:
        pending = b""
        while chunk := await stream.read(65536):
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                self._line(line)
            if len(pending) > MAX_LINE:
                self._line(pending)
                pending = b""
        if pending:
            self._line(pending)

    def _line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").rstrip("\r")[:MAX_LINE]
        if self.spec.ignores(text):
            return
        self.logs.append(text)
        self.log.info("%s", text)

    def _note(self, text: str) -> None:
        """HController's own notes about the module, kept in its log next to the app's output."""
        self.logs.append(text, source="hcontroller")
        self.log.info("%s", text)

    def _set(self, state: str, message: str | None = None) -> None:
        if (state, message) == (self.state, self.message):
            return
        self.state, self.message = state, message
        self.on_change(self)
