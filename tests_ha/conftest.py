"""A fake releve API, served through Home Assistant's aiohttp mock."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

pytest_plugins = ["pytest_homeassistant_custom_component"]

URL = "http://releve.test:8080"
PDL = "01234567890123"
USAGE_POINTS: list[dict[str, Any]] = [
    {
        "id": PDL,
        "name": "",
        "datasets": ["daily_consumption", "max_power"],
        "last_success": "2026-09-12T10:00:00+00:00",
    }
]
STATE: dict[str, Any] = {
    "energy_yesterday_kwh": 6.028,
    "energy_last_7_days_kwh": None,
    "energy_last_30_days_kwh": None,
    "latest_day": "2026-09-11",
    "max_power_yesterday_va": 6100,
}
GRID: dict[str, Any] = {
    "day": "2026-09-12",
    "tempo_today": "WHITE",
    "ecowatt_today": 2,
    "ecowatt_message": "tendu",
    "tempo_days_left_blue": None,
    "tempo_days_left_white": None,
    "tempo_days_left_red": None,
    "tempo_prices": None,
}

type Serve = Callable[..., None]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/releve."""


@pytest.fixture
def releve_api(aioclient_mock: AiohttpClientMocker) -> Serve:
    """Serve releve's API; call it again to change what it answers."""

    def serve(
        *,
        usage_points: list[dict[str, Any]] = USAGE_POINTS,
        state: dict[str, Any] = STATE,
        grid: dict[str, Any] | None = GRID,
        status: int = 200,
        exc: Exception | None = None,
    ) -> None:
        aioclient_mock.clear_requests()
        if exc is not None:
            aioclient_mock.get(f"{URL}/api/v1/usage-points", exc=exc)
            return
        aioclient_mock.get(f"{URL}/api/v1/usage-points", json=usage_points, status=status)
        aioclient_mock.get(f"{URL}/api/v1/usage-points/{PDL}/state", json=state)
        if grid is None:
            aioclient_mock.get(
                f"{URL}/api/v1/rte/state", json={"error": "grid signals are disabled"}, status=404
            )
        else:
            aioclient_mock.get(f"{URL}/api/v1/rte/state", json=grid)
        aioclient_mock.get(f"{URL}/healthz", json={"ok": True, "version": "0.2.0"})

    serve()
    return serve
