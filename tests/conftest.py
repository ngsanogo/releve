"""Shared fixtures: a real Store on a throwaway file, a clock the tests wind by hand,
settings built in code, and an isolated environment (no RELEVE_* or XDG leaks)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from releve.config import Settings
from releve.quota import QuotaGovernor
from releve.store import Store

PDL = "01234567890123"
OTHER_PDL = "01234567890124"
# 12:00 in Paris on Saturday 2026-09-12: "today" is 2026-09-12, data exists up to the 11th.
NOW = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("RELEVE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "releve.db"


@pytest.fixture
def store(database: Path, clock: FrozenClock) -> Store:
    return Store.open(database, clock)


@pytest.fixture
def governor(store: Store, clock: FrozenClock) -> QuotaGovernor:
    return QuotaGovernor(store, daily_budget=45, clock=clock)


def make_settings(database: Path, **overrides: Any) -> Settings:
    """Settings for one consumption-only usage point, with `overrides` merged per section."""
    data: dict[str, Any] = {
        "gateway": {"token": "test-token"},
        "usage_points": [{"id": PDL, "name": "Home"}],
        "storage": {"path": str(database)},
        "sync": {"rte_signals": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return Settings(**data)
