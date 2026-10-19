"""Is a module answering? Its health URL if it has one, otherwise whether its port is open."""

from __future__ import annotations

import asyncio
import contextlib

import httpx

from .manifest import ModuleSpec


async def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def probe(client: httpx.AsyncClient, spec: ModuleSpec, timeout: float = 2.0) -> tuple[bool, str | None]:
    """(answering, why not)."""
    if spec.health:
        try:
            response = await client.get(spec.endpoint(spec.health), timeout=timeout)
        except httpx.HTTPError as exc:
            return False, f"health check failed: {describe(exc)}"
        if response.is_success:
            return True, None
        return False, f"health check returned {response.status_code}"
    address = spec.address()
    if address is None:
        return False, "no url to check"
    if await port_open(*address, timeout=timeout):
        return True, None
    return False, f"nothing is listening on {address[0]}:{address[1]}"


def describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__
