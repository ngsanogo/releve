"""A synthetic meter behind the real quota governor.

`FakeGateway` answers like the gateway does — including its quirks — and charges
every call to the governor first, so tests of the sync pass also exercise the
budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx2

from releve.clock import Clock, at_paris_hour, day_end, day_start
from releve.domain import (
    Address,
    CacheResource,
    Consent,
    Contact,
    Contract,
    DailyEnergy,
    Direction,
    EcowattDay,
    EcowattForecast,
    EcowattHour,
    Identity,
    LoadCurvePoint,
    Period,
    PowerPeak,
    TempoColor,
    TempoDay,
    TempoPrice,
    TempoSeason,
)
from releve.errors import (
    AuthError,
    GatewayUnreachableError,
    ThrottledError,
    WindowRejectedError,
)
from releve.quota import RTE_BUCKET, QuotaGovernor

STEP = timedelta(minutes=30)
Call = tuple[str, str, date | None, date | None]


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
    partial_curves: set[date] = field(default_factory=set)  # served with half their points
    # The gateway's cache endpoints serve a curve day as they first served it.
    stale_curve_cache: bool = False
    throttled_curves: bool = False
    refused_usage_points: set[str] = field(default_factory=set)
    unreachable: bool = False
    refused_windows: set[date] = field(default_factory=set)
    consent_valid: bool = True
    consent_banned: bool = False
    calls: list[Call] = field(default_factory=list)
    uncached_calls: list[Call] = field(default_factory=list)
    _curve_cache: dict[tuple[str, Direction, date], list[LoadCurvePoint]] = field(
        default_factory=dict
    )
    deleted: list[tuple[str, CacheResource, date | None, date | None]] = field(default_factory=list)

    def _charge(
        self, bucket: str, endpoint: str, start: date | None = None, end: date | None = None
    ) -> None:
        self.governor.reserve(bucket, endpoint)
        self.calls.append((bucket, endpoint, start, end))
        if self.unreachable:
            raise GatewayUnreachableError(f"{endpoint}: no answer from the gateway (refused)")
        if start is not None and start in self.refused_windows:
            raise WindowRejectedError(f"{endpoint}: the gateway refused the window (HTTP 404)")

    def _available(self, start: date, end: date) -> list[date]:
        return [d for d in days(start, end) if d < self.published_until and d not in self.holes]

    def valid_access(self, usage_point: str) -> Consent:
        self._charge(usage_point, "valid_access")
        if usage_point in self.refused_usage_points:
            raise AuthError("valid_access: the gateway refused the token (HTTP 403)")
        return Consent(
            usage_point,
            valid=self.consent_valid,
            expires_at=datetime(2029, 1, 1, tzinfo=UTC),
            call_number=1,
            quota_limit=50,
            quota_reached=False,
            quota_reset_at=None,
            last_call_at=None,
            banned=self.consent_banned,
            information="" if self.consent_valid else "consent expired",
        )

    def contract(self, usage_point: str) -> Contract:
        self._charge(usage_point, "contracts")
        return Contract(
            usage_point,
            segment="C5",
            subscribed_power="9 kVA",
            distribution_tariff="BTINFCU4",
            offpeak_hours="HC (22H00-6H00)",
            contract_status="SERVC",
            meter_type="TCB",
            usage_point_status="COM",
        )

    def identity(self, usage_point: str) -> Identity:
        self._charge(usage_point, "identity")
        return Identity(
            usage_point, customer_id="cust", title="M", firstname="Ada", lastname="Lovelace"
        )

    def contact(self, usage_point: str) -> Contact:
        self._charge(usage_point, "contact")
        return Contact(usage_point, customer_id="cust", phone="0102030405", email="a@b.c")

    def addresses(self, usage_point: str) -> Address:
        self._charge(usage_point, "addresses")
        return Address(
            usage_point,
            customer_id="cust",
            street="1 rue de la Paix",
            postal_code="75002",
            city="Paris",
            country="France",
        )

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
        self,
        usage_point: str,
        direction: Direction,
        start: date,
        end: date,
        *,
        use_cache: bool = True,
    ) -> list[LoadCurvePoint]:
        self._charge(usage_point, f"{direction}_load_curve", start, end)
        if not use_cache:
            self.uncached_calls.append((usage_point, f"{direction}_load_curve", start, end))
        if self.throttled_curves:
            retry_at = self.clock() + timedelta(hours=2)
            self.governor.block(usage_point, retry_at, "throttled upstream")
            raise ThrottledError("load curve: throttled upstream", retry_at)
        # Quirk observed live: the answer also covers the `end` day.
        served = self._available(start, end + timedelta(days=1))
        points = []
        for day in served:
            key = (usage_point, direction, day)
            if self.stale_curve_cache and use_cache and key in self._curve_cache:
                points += self._curve_cache[key]
                continue
            day_points = curve_of(usage_point, direction, day)
            if day in self.partial_curves:
                day_points = day_points[: len(day_points) // 2]
            self._curve_cache[key] = day_points
            points += day_points
        return points

    def max_power(self, usage_point: str, start: date, end: date) -> list[PowerPeak]:
        self._charge(usage_point, "daily_consumption_max_power", start, end)
        return [
            PowerPeak(usage_point, d, 6000, at_paris_hour(d, 19))
            for d in self._available(start, end)
        ]

    def tempo(self, start: date, end: date) -> list[TempoDay]:
        self._charge(RTE_BUCKET, "rte_tempo", start, end)
        return [TempoDay(d, "BLUE") for d in days(start, end)]

    def ecowatt(self, start: date, end: date) -> EcowattForecast:
        self._charge(RTE_BUCKET, "rte_ecowatt", start, end)
        days_ = tuple(EcowattDay(d, 1, "") for d in days(start, end))
        hours = tuple(
            EcowattHour(at_paris_hour(d, hour), 1) for d in days(start, end) for hour in range(24)
        )
        return EcowattForecast(days_, hours)

    def tempo_prices(self) -> list[TempoPrice]:
        self._charge(RTE_BUCKET, "edf_tempo_price")
        return [
            TempoPrice(color, period, Decimal("0.1")) for color in TempoColor for period in Period
        ]

    def tempo_season(self) -> TempoSeason:
        self._charge(RTE_BUCKET, "edf_tempo_days")
        return TempoSeason({TempoColor.BLUE: 200, TempoColor.WHITE: 40, TempoColor.RED: 22})

    def delete_cache(
        self,
        usage_point: str,
        resource: CacheResource,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        self._charge(usage_point, f"delete_{resource}", start, end)
        self.deleted.append((usage_point, resource, start, end))


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
        self._routes: dict[tuple[str, str], _Route] = {}
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
        method: str = "GET",
    ) -> None:
        self._routes[(method.upper(), path)] = _Route(status, json, text, headers or {}, error)

    def calls(self, path: str, method: str = "GET") -> int:
        return sum(
            1
            for request in self.requests
            if request.url.path == path and request.method == method.upper()
        )

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        route = self._routes.get((request.method, request.url.path))
        if route is None:
            return httpx2.Response(404, text=f"no route for {request.method} {request.url.path}")
        if route.error is not None:
            raise route.error
        if route.text is not None:
            return httpx2.Response(route.status, text=route.text, headers=route.headers)
        return httpx2.Response(route.status, json=route.json, headers=route.headers)
