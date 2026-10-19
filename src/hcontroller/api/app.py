"""HTTP API, the dashboard, and two WebSockets: ``/ws`` (dashboard updates) and
``/display/stream`` (the panel's frames, for dashboard previews and the LED driver)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..controller import Controller, UnknownModule
from ..display.schedule import Window
from ..modules.configfile import ConfigFileError
from ..modules.runner import NotSupported, utcnow

_DASHBOARD = Path(__file__).with_name("dashboard.html")


class ConfigText(BaseModel):
    text: str


class ShowNow(BaseModel):
    module: str


class DisplaySettings(BaseModel):
    brightness: int = Field(ge=1, le=100)


class ScheduleIn(BaseModel):
    windows: list[Window] = Field(default_factory=list)
    default: str


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = list(error.get("loc", ()))
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        if len(loc) >= 2 and loc[0] == "windows" and isinstance(loc[1], int):
            where = f"window {loc[1] + 1}" + (f" {loc[2]}" if len(loc) > 2 else "")
        else:
            where = " ".join(str(part) for part in loc)
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except UnknownModule as exc:
        raise HTTPException(404, f"there's no module called {exc.args[0]!r}") from exc
    except NotSupported as exc:
        raise HTTPException(409, str(exc)) from exc
    except ConfigFileError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _pump(socket: WebSocket, queue: asyncio.Queue, send: Callable[[Any], Awaitable[None]]) -> None:
    """Forward queued items to the socket until the client goes away or the server shuts down."""

    async def until_closed() -> None:
        while (await socket.receive())["type"] != "websocket.disconnect":
            pass

    closed = asyncio.create_task(until_closed())
    try:
        while True:
            item = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({item, closed}, return_when=asyncio.FIRST_COMPLETED)
            if item not in done:
                item.cancel()
                return
            await send(item.result())
    except Exception:  # the client vanished mid-send
        return
    finally:
        closed.cancel()


def create_app(controller: Controller, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_lifecycle:
            await controller.start()
        yield
        if manage_lifecycle:
            await controller.stop()

    app = FastAPI(title="HController", version=__version__, lifespan=lifespan)
    registry, scheduler, hub = controller.registry, controller.scheduler, controller.hub

    @app.get("/health")
    def health() -> dict[str, Any]:
        snapshot = scheduler.snapshot()
        return {
            "ok": True,
            "version": __version__,
            "now": utcnow().isoformat(),
            "modules": {status["id"]: status["state"] for status in registry.statuses()},
            "panel": snapshot["module"],
        }

    # ------------------------------------------------------------------ modules

    @app.get("/api/modules")
    def modules() -> list[dict[str, Any]]:
        return registry.statuses()

    @app.get("/api/modules/{module_id}")
    def module(module_id: str) -> dict[str, Any]:
        with _errors():
            controller.runner(module_id)
            return registry.status(module_id)

    @app.post("/api/modules/{module_id}/start")
    async def start(module_id: str) -> dict[str, Any]:
        with _errors():
            return await controller.start_module(module_id)

    @app.post("/api/modules/{module_id}/stop")
    async def stop(module_id: str) -> dict[str, Any]:
        with _errors():
            return await controller.stop_module(module_id)

    @app.post("/api/modules/{module_id}/restart")
    async def restart(module_id: str) -> dict[str, Any]:
        with _errors():
            return await controller.restart_module(module_id)

    @app.get("/api/modules/{module_id}/logs")
    def logs(
        module_id: str, lines: int = Query(200, ge=0, le=2000), after: int | None = Query(None, ge=0)
    ) -> dict[str, Any]:
        with _errors():
            return controller.module_logs(module_id, lines, after)

    @app.get("/api/modules/{module_id}/config")
    def get_config(module_id: str) -> dict[str, Any]:
        with _errors():
            return controller.module_config(module_id)

    @app.put("/api/modules/{module_id}/config")
    async def put_config(module_id: str, body: ConfigText, restart: bool = False) -> dict[str, Any]:
        with _errors():
            return await controller.save_module_config(module_id, body.text, restart)

    @app.post("/api/modules/{module_id}/config/revert")
    async def revert_config(module_id: str, restart: bool = False) -> dict[str, Any]:
        with _errors():
            return await controller.revert_module_config(module_id, restart)

    # ------------------------------------------------------------------ the panel

    @app.get("/api/display")
    def display() -> dict[str, Any]:
        return scheduler.snapshot()

    @app.put("/api/display")
    def put_display(body: DisplaySettings) -> dict[str, Any]:
        scheduler.set_brightness(body.brightness)
        return scheduler.snapshot()

    @app.post("/api/display/show")
    def show_now(body: ShowNow) -> dict[str, Any]:
        try:
            scheduler.show_now(body.module)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return scheduler.snapshot()

    @app.delete("/api/display/show")
    def resume() -> dict[str, Any]:
        scheduler.resume()
        return scheduler.snapshot()

    @app.get("/api/display/schedule")
    def get_schedule() -> dict[str, Any]:
        return scheduler.schedule_snapshot()

    @app.put("/api/display/schedule")
    def put_schedule(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if "schedule" not in body:
            raise HTTPException(400, 'send {"schedule": {"windows": [...], "default": "..."}}, or {"schedule": null} to reset')
        if body["schedule"] is None:
            scheduler.reset_schedule()
            return scheduler.schedule_snapshot()
        try:
            parsed = ScheduleIn.model_validate(body["schedule"])
        except ValidationError as exc:
            raise HTTPException(400, _validation_message(exc)) from exc
        try:
            scheduler.set_schedule(parsed.windows, parsed.default)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return scheduler.schedule_snapshot()

    @app.get("/api/display/timeline")
    def get_timeline(day: str | None = Query(None, alias="date")) -> dict[str, Any]:
        try:
            parsed = date.fromisoformat(day) if day else None
        except ValueError as exc:
            raise HTTPException(400, "date should look like 2026-10-19") from exc
        return scheduler.timeline(parsed)

    @app.get("/api/display/frame")
    def frame() -> Response:
        return Response(
            hub.frame,
            media_type="application/octet-stream",
            headers={
                "X-Width": str(hub.width),
                "X-Height": str(hub.height),
                "X-Brightness": str(hub.brightness),
                "X-Module": hub.module or "",
                "Cache-Control": "no-store",
            },
        )

    @app.websocket("/display/stream")
    async def display_stream(socket: WebSocket) -> None:
        await socket.accept()
        queue = hub.subscribe()
        try:
            await _pump(socket, queue, socket.send_bytes)
        finally:
            hub.unsubscribe(queue)

    # ------------------------------------------------------------------ dashboard

    @app.websocket("/ws")
    async def updates(socket: WebSocket) -> None:
        await socket.accept()
        queue = controller.bus.subscribe()
        try:
            await socket.send_json(
                {
                    "type": "snapshot",
                    "modules": registry.statuses(),
                    "display": scheduler.snapshot(),
                    "schedule": scheduler.schedule_snapshot(),
                }
            )
            await _pump(socket, queue, socket.send_json)
        except Exception:
            pass
        finally:
            controller.bus.unsubscribe(queue)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _DASHBOARD.read_text(encoding="utf-8")

    return app
