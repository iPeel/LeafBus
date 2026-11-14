"""Client for the LeafBus board's JSON API (firmware/web.py)."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT


class LeafBusError(Exception):
    """The board answered with an error."""


class LeafBusConnectionError(LeafBusError):
    """The board could not be reached."""


class LeafBusClient:
    """Talks to one board over HTTP. The board serves one request at a time,
    so callers keep the request rate low (one poll every few seconds)."""

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        self._session = session
        self.host = host.strip().rstrip("/")
        if "://" in self.host:
            self.host = self.host.split("://", 1)[1]
        self.base = f"http://{self.host}"

    async def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method, self.base + path, json=body
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        msg = data.get("error") if isinstance(data, dict) else None
                        raise LeafBusError(msg or f"HTTP {resp.status}")
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise LeafBusConnectionError(
                f"{self.base}{path}: {err or type(err).__name__}"
            ) from err

    async def info(self) -> dict[str, Any]:
        """Firmware, network and board status (/api/info)."""
        return await self._request("GET", "/api/info")

    async def state(self) -> dict[str, Any]:
        """Power, brightness, effect, preset, sleep timer (/api/state)."""
        return await self._request("GET", "/api/state")

    async def presets(self) -> list[str]:
        """Names of the saved presets, in the board's order."""
        data = await self._request("GET", "/api/presets")
        return [p["name"] for p in data if isinstance(p, dict) and "name" in p]

    async def set_state(self, **changes: Any) -> dict[str, Any]:
        """Change power / brightness / preset / timer; returns the new state."""
        return await self._request("POST", "/api/state", changes)
