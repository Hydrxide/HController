from datetime import timedelta

from conftest import FAST, free_port, pattern_spec, wait_for

from hcontroller.events import EventBus
from hcontroller.modules.registry import Registry
from hcontroller.modules.runner import utcnow
from hcontroller.state import State


def registry(specs, tmp_path, http=None) -> Registry:
    return Registry(specs, State(tmp_path / "state.json"), EventBus(), http=lambda: http, runner_options=FAST)


async def test_always_module_autostarts_and_remembers_a_stop(tmp_path, http):
    specs = {"tp": pattern_spec(free_port())}
    reg = registry(specs, tmp_path, http)
    await reg.start()
    try:
        await wait_for(lambda: reg.healthy() == {"tp"})
        reg.set_running("tp", False)
        await reg.reconcile()
        assert reg.runners["tp"].state == "stopped"
    finally:
        await reg.stop()

    again = registry(specs, tmp_path, http)  # the Stop survives a restart of HController
    await again.start()
    try:
        assert again.runners["tp"].state == "stopped"
        assert not again.runners["tp"].active
    finally:
        await again.stop()


async def test_scheduled_module_runs_only_while_wanted(tmp_path, http):
    specs = {"tp": pattern_spec(free_port(), process="scheduled")}
    reg = registry(specs, tmp_path, http)
    await reg.start()
    try:
        assert reg.runners["tp"].state == "stopped"
        reg.set_wanted({"tp"})
        await reg.reconcile()
        await wait_for(lambda: "tp" in reg.healthy())
        reg.set_wanted(set())
        await reg.reconcile()
        assert reg.runners["tp"].state == "stopped"
    finally:
        await reg.stop()


def test_hold_beats_the_schedule_until_it_expires(tmp_path):
    reg = registry({"tp": pattern_spec(1234, process="scheduled")}, tmp_path)
    now = utcnow()
    reg.set_wanted({"tp"})
    assert reg.desired("tp", now)
    reg.set_hold("tp", False, now + timedelta(hours=1))
    assert not reg.desired("tp", now)
    assert reg.desired("tp", now + timedelta(hours=2))
    assert reg.status("tp")["hold"]["running"] is False
    reg._expire_holds(now + timedelta(hours=2))
    assert reg.state.get("holds") is None


def test_status_describes_the_module(tmp_path):
    reg = registry({"tp": pattern_spec(9134, ui="/", name="Test")}, tmp_path)
    status = reg.status("tp")
    assert status["name"] == "Test" and status["kind"] == "local" and status["process"] == "always"
    assert status["display"] is True and status["config"] is False
    assert status["ui"] == {"url": "http://127.0.0.1:9134/", "scheme": "http", "port": 9134, "path": "/", "loopback": True}


async def test_status_changes_reach_the_bus_and_listeners(tmp_path, http):
    reg = registry({"tp": pattern_spec(free_port())}, tmp_path, http)
    queue = reg.bus.subscribe()
    ticks = []
    reg.listeners.append(lambda: ticks.append(1))
    await reg.start()
    try:
        await wait_for(lambda: reg.healthy() == {"tp"})
    finally:
        await reg.stop()
    states = []
    while not queue.empty():
        states.append(queue.get_nowait()["module"]["state"])
    assert states[:2] == ["starting", "healthy"] and states[-1] == "stopped"
    assert ticks
