"""Client for releve's read-only JSON API.

The integration never talks to the MyElectricalData gateway: releve does, under
its quota governor, and this client only reads releve's local cache.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_SECONDS

type JsonObject = dict[str, Any]


class ReleveError(Exception):
    """releve could not answer."""


class ReleveConnectionError(ReleveError):
    """releve is unreachable, or answered with an error."""


class ReleveAuthError(ReleveError):
    """releve refused the token (web.auth_token)."""


class ReleveClient:
    """Read the state releve publishes for its usage points and the grid."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    @property
    def url(self) -> str:
        return self._url

    async def version(self) -> str | None:
        """releve's version, from /healthz (open even when a token is set)."""
        health = await self._get("/healthz", allow_unavailable=True)
        version = health.get("version") if isinstance(health, dict) else None
        return version if isinstance(version, str) else None

    async def usage_points(self) -> list[JsonObject]:
        """The configured usage points: id, name, datasets, last_success."""
        answer = await self._get("/api/v1/usage-points")
        if not isinstance(answer, list):
            raise ReleveConnectionError("unexpected answer from /api/v1/usage-points")
        return answer

    async def state(self, pdl: str) -> JsonObject:
        """Today's state of one usage point, the same JSON releve publishes on MQTT."""
        answer = await self._get(f"/api/v1/usage-points/{pdl}/state")
        if not isinstance(answer, dict):
            raise ReleveConnectionError(f"unexpected answer for usage point {pdl}")
        return answer

    async def grid_state(self) -> JsonObject | None:
        """Today's Tempo and Ecowatt, or None when releve has grid signals turned off."""
        answer = await self._get("/api/v1/rte/state", allow_missing=True)
        if answer is None:
            return None
        if not isinstance(answer, dict):
            raise ReleveConnectionError("unexpected answer from /api/v1/rte/state")
        return answer

    async def _get(
        self, path: str, *, allow_missing: bool = False, allow_unavailable: bool = False
    ) -> Any:
        try:
            async with self._session.get(
                f"{self._url}{path}",
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if response.status == 401:
                    raise ReleveAuthError("releve refused the token")
                if response.status == 404 and allow_missing:
                    return None
                # /healthz answers 503 with a JSON reason while the daemon struggles.
                if not (response.status == 503 and allow_unavailable):
                    response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:  # ValueError: bad JSON
            raise ReleveConnectionError(f"{path}: {err}") from err
