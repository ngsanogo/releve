"""A synthetic meter behind the real quota governor.

`FakeGateway` answers like the gateway does — including its quirks — and charges
every call to the governor first, so tests of the sync pass also exercise the
budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx2

from releve.clock import Clock, at_paris_hour, day_end, day_start
from releve.domain import (
    DailyEnergy,
    Direction,
    EcowattDay,
    LoadCurvePoint,
    PowerPeak,
    TempoDay,
)
from releve.errors import (
    AuthError,
    GatewayUnreachableError,
    ThrottledError,
    WindowRejectedError,
)
from releve.quota import RTE_BUCKET, QuotaGovernor

STEP = timedelta(minutes=30)


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days)]


def wh_of(day: date) -> int:
    return 5000 + day.toordinal() % 1000


def curve_of(usage_point: str, direction: Direction, day: date) -> list[LoadCurvePoint]:
    start, end = day_start(day), day_end(day)
    count = (end - start) // STEP
    return [
        LoadCurvePoint(usage_point, direction, start + STEP * k, 200 + (k * 37) % 900)
        for k in range(1, count + 1)
    ]


@dataclass
class FakeGateway:
    governor: QuotaGovernor
    clock: Clock
    published_until: date
    holes: set[date] = field(default_factory=set)
    throttled_curves: bool = False
    refused_usage_points: set[str] = field(default_factory=set)
    unreachable: bool = False
    refused_windows: set[date] = field(default_factory=set)
    calls: list[tuple[str, str, date, date]] = field(default_factory=list)

    def _charge(self, bucket: str, endpoint: str, start: date, end: date) -> None:
        self.governor.reserve(bucket, endpoint)
        self.calls.append((bucket, endpoint, start, end))
        if self.unreachable:
            raise GatewayUnreachableError(f"{endpoint}: no answer from the gateway (refused)")
        if start in self.refused_windows:
            raise WindowRejectedError(f"{endpoint}: the gateway refused the window (HTTP 404)")

    def _available(self, start: date, end: date) -> list[date]:
        return [d for d in days(start, end) if d < self.published_until and d not in self.holes]

    def daily(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[DailyEnergy]:
        self._charge(usage_point, f"daily_{direction}", start, end)
        if usage_point in self.refused_usage_points:
            raise AuthError("daily: the gateway refused the token (HTTP 403)")
        return [
            DailyEnergy(usage_point, direction, d, wh_of(d)) for d in self._available(start, end)
        ]

    def load_curve(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[LoadCurvePoint]:
        self._charge(usage_point, f"{direction}_load_curve", start, end)
        if self.throttled_curves:
            retry_at = self.clock() + timedelta(hours=2)
            self.governor.block(usage_point, retry_at, "throttled upstream")
            raise ThrottledError("load curve: throttled upstream", retry_at)
        # Quirk observed live: the answer also covers the `end` day.
        served = self._available(start, end + timedelta(days=1))
        return [point for day in served for point in curve_of(usage_point, direction, day)]

    def max_power(self, usage_point: str, start: date, end: date) -> list[PowerPeak]:
        self._charge(usage_point, "daily_consumption_max_power", start, end)
        return [
            PowerPeak(usage_point, d, 6000, at_paris_hour(d, 19))
            for d in self._available(start, end)
        ]

    def tempo(self, start: date, end: date) -> list[TempoDay]:
        self._charge(RTE_BUCKET, "rte", start, end)
        return [TempoDay(d, "BLUE") for d in days(start, end)]

    def ecowatt(self, start: date, end: date) -> list[EcowattDay]:
        self._charge(RTE_BUCKET, "rte", start, end)
        return [EcowattDay(d, 1, "") for d in days(start, end)]


@dataclass
class _Route:
    status: int
    json: object
    text: str | None
    headers: dict[str, str]
    error: Exception | None


class HttpDouble:
    """An HTTP server double for `httpx2.MockTransport`: fixed answers per path."""

    def __init__(self) -> None:
        self._routes: dict[str, _Route] = {}
        self.requests: list[httpx2.Request] = []

    def on(
        self,
        path: str,
        *,
        status: int = 200,
        json: object = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._routes[path] = _Route(status, json, text, headers or {}, error)

    def calls(self, path: str) -> int:
        return sum(1 for request in self.requests if request.url.path == path)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        route = self._routes.get(request.url.path)
        if route is None:
            return httpx2.Response(404, text=f"no route for {request.url.path}")
        if route.error is not None:
            raise route.error
        if route.text is not None:
            return httpx2.Response(route.status, text=route.text, headers=route.headers)
        return httpx2.Response(route.status, json=route.json, headers=route.headers)
