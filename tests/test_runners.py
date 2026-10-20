"""LocalRunner / ExternalRunner against real child processes (tools/testpattern.py)."""

import asyncio
import socket
import subprocess
import sys

from conftest import FAST, TESTPATTERN, free_port, pid_alive, pattern_spec, wait_for

from hcontroller.modules.external import ExternalRunner
from hcontroller.modules.health import port_open
from hcontroller.modules.local import LocalRunner, venv_program
from hcontroller.modules.manifest import ModuleSpec


def runner(spec, http, **options) -> LocalRunner:
    states: list[str] = []
    r = LocalRunner("tp", spec, http=lambda: http, on_change=lambda r: states.append(r.state), **{**FAST, **options})
    r.seen = states  # type: ignore[attr-defined]
    return r


def log_text(r) -> str:
    return "\n".join(line["text"] for line in r.logs.tail(2000))


async def test_start_becomes_healthy_and_stop_leaves_nothing_behind(http):
    port = free_port()
    r = runner(pattern_spec(port), http)
    r.start()
    await wait_for(lambda: r.state == "healthy")
    pid = r.status()["pid"]
    assert pid and r.status()["uptime"] is not None
    assert "starting" in r.seen
    assert any(line["source"] == "app" and "testpattern" in line["text"] for line in r.logs.tail())
    assert "started (pid" in log_text(r)

    await r.stop()
    assert r.state == "stopped" and not r.active
    assert not pid_alive(pid)
    assert not await port_open("127.0.0.1", port)


async def test_crash_is_restarted_with_backoff(http):
    r = runner(pattern_spec(free_port(), "--crash-after", "0.4"), http)
    r.start()
    await wait_for(lambda: r.restart_count >= 2, timeout=15)
    assert r.last_exit == 3
    assert "backoff" in r.seen
    assert "exited with code 3" in log_text(r)
    await r.stop()
    assert r.state == "stopped"


async def test_clean_exit_under_on_failure_stays_stopped(http):
    r = runner(pattern_spec(free_port(), "--exit-after", "0.4"), http)
    r.start()
    await wait_for(lambda: not r.active)
    assert (r.state, r.message, r.restart_count) == ("stopped", "exited with code 0", 0)


async def test_restart_never_reports_a_crash(http):
    r = runner(pattern_spec(free_port(), "--crash-after", "0.4", restart="never"), http)
    r.start()
    await wait_for(lambda: not r.active)
    assert (r.state, r.message) == ("crashed", "exited with code 3")


async def test_restart_always_restarts_clean_exits(http):
    r = runner(pattern_spec(free_port(), "--exit-after", "0.3", restart="always"), http)
    r.start()
    await wait_for(lambda: r.restart_count >= 1, timeout=10)
    await r.stop()


async def test_ignoring_sigterm_gets_sigkill(http):
    r = runner(pattern_spec(free_port(), "--ignore-sigterm", stop_timeout=0.5), http)
    r.start()
    await wait_for(lambda: r.state == "healthy")
    pid = r.status()["pid"]
    await r.stop()
    assert not pid_alive(pid)
    assert "killing it" in log_text(r)
    assert "SIGKILL" in log_text(r)


async def test_port_already_in_use_is_a_conflict_not_a_second_copy(http):
    port = free_port()
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        r = runner(pattern_spec(port), http)
        r.start()
        await wait_for(lambda: r.state == "conflict")
        assert r.proc is None
        assert f"127.0.0.1:{port}" in r.message
    # Once the port is free it starts on its own.
    await wait_for(lambda: r.state == "healthy", timeout=10)
    await r.stop()


async def test_missing_program_backs_off_with_a_clear_message(http, tmp_path):
    spec = ModuleSpec(command=["./not-here"], cwd=tmp_path)
    r = runner(spec, http)
    r.start()
    await wait_for(lambda: r.state == "backoff")
    assert "not-here doesn't exist" in r.message
    await r.stop()
    assert r.state == "stopped"


def test_venv_bin_python_finds_the_windows_venv(tmp_path):
    scripts = tmp_path / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").touch()
    unix = tmp_path / ".venv" / "bin" / "python"
    assert venv_program(unix, windows=True) == scripts / "python.exe"
    assert venv_program(unix, windows=False) == unix


def test_venv_program_leaves_other_paths_alone(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").touch()
    assert venv_program(tmp_path / "bin" / "python", windows=True) == tmp_path / "bin" / "python"  # exists
    # No Scripts copy either: keep the configured path, so the error names what the config says.
    assert venv_program(tmp_path / ".venv" / "bin" / "python", windows=True) == tmp_path / ".venv" / "bin" / "python"
    assert venv_program(tmp_path / "tools" / "run", windows=True) == tmp_path / "tools" / "run"  # not a bin/


async def test_failing_health_check_marks_unhealthy(http):
    r = runner(pattern_spec(free_port(), "--unhealthy"), http, start_grace=0.5)
    r.start()
    await wait_for(lambda: r.state == "unhealthy")
    assert "503" in r.message
    await r.stop()


async def test_module_without_url_is_healthy_while_running(http):
    spec = ModuleSpec(command=[sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(30)"])
    r = runner(spec, http)
    r.start()
    await wait_for(lambda: r.state == "healthy")
    await wait_for(lambda: "hi" in log_text(r))
    await r.stop()


async def test_log_ignore_drops_matching_lines(http):
    code = "print('INFO: \"GET /render/frame HTTP/1.1\" 200 OK'); print('keep me', flush=True); import time; time.sleep(30)"
    spec = ModuleSpec(command=[sys.executable, "-c", code], log_ignore=[r'"GET /render/frame HTTP/[\d.]+" 200'])
    r = runner(spec, http)
    r.start()
    await wait_for(lambda: "keep me" in log_text(r))
    assert [line["text"] for line in r.logs.tail() if line["source"] == "app"] == ["keep me"]
    await r.stop()


def test_bad_log_ignore_pattern_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="log_ignore"):
        ModuleSpec(command=["x"], log_ignore=["(unclosed"])


async def test_restart_gives_a_new_process(http):
    r = runner(pattern_spec(free_port()), http)
    r.start()
    await wait_for(lambda: r.state == "healthy")
    first = r.status()["pid"]
    await r.restart()
    await wait_for(lambda: r.state == "healthy")
    assert r.status()["pid"] != first
    await r.stop()


async def test_external_runner_follows_the_app_up_and_down(http):
    port = free_port()
    spec = ModuleSpec(url=f"http://127.0.0.1:{port}", health="/health")
    states: list[str] = []
    watcher = ExternalRunner("ext", spec, http=lambda: http, on_change=lambda r: states.append(r.state), interval=0.1)
    watcher.watch()
    await wait_for(lambda: watcher.state == "external-down")
    proc = subprocess.Popen([sys.executable, str(TESTPATTERN), "--port", str(port)])
    try:
        await wait_for(lambda: watcher.state == "external-up")
    finally:
        proc.terminate()
        proc.wait()
    await wait_for(lambda: watcher.state == "external-down")
    await watcher.unwatch()
    assert states[:3] == ["external-down", "external-up", "external-down"]


async def test_external_runner_refuses_lifecycle(http):
    watcher = ExternalRunner("ext", ModuleSpec(url="http://127.0.0.1:1"), http=lambda: http, on_change=lambda r: None)
    for action in (watcher.stop, watcher.restart):
        try:
            await action()
        except Exception as exc:
            assert "can't be started or stopped" in str(exc)
        else:
            raise AssertionError("expected NotSupported")
    await asyncio.sleep(0)
