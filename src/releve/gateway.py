"""The MyElectricalData gateway client.

The only module that speaks HTTP and JSON with the gateway. Every call passes
the quota governor first; every answer is parsed here into domain types or
rejected with a typed error. An upstream throttle becomes a persisted block,
never a retry.

Endpoints used (public API facts):
    GET /daily_{consumption,production}/{pdl}/start/{d}/end/{d}[/cache]
    GET /{consumption,production}_load_curve/{pdl}/start/{d}/end/{d}[/cache]
    GET /daily_consumption_max_power/{pdl}/start/{d}/end/{d}[/cache]
    GET /rte/tempo/{d}/{d}
    GET /rte/ecowatt/{d}/{d}
`end` is exclusive. Load-curve dates are interval ENDS in Paris wall-clock time.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import Any, Protocol, Self

import httpx2

from releve.clock import PARIS, Clock, next_utc_midnight, utc_now
from releve.config import GatewaySettings
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
    GatewayError,
    GatewayUnreachableError,
    ThrottledError,
    WindowRejectedError,
)
from releve.quota import RTE_BUCKET, QuotaGovernor

log = logging.getLogger(__name__)

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}
# Enedis' throttle body, e.g. "nextAccessTime":"2026-Sep-12 20:00:00+0000 UTC".
_NEXT_ACCESS = re.compile(
    r'nextAccessTime"?\s*:\s*"(\d{4})-([A-Z][a-z]{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\+0000'
)
_THROTTLE_FALLBACK = timedelta(hours=1)


class Gateway(Protocol):
    """What the sync pass needs from the gateway."""

    def daily(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[DailyEnergy]: ...

    def load_curve(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[LoadCurvePoint]: ...

    def max_power(self, usage_point: str, start: date, end: date) -> list[PowerPeak]: ...

    def tempo(self, start: date, end: date) -> list[TempoDay]: ...

    def ecowatt(self, start: date, end: date) -> list[EcowattDay]: ...


class GatewayClient:
    def __init__(
        self,
        settings: GatewaySettings,
        governor: QuotaGovernor,
        *,
        clock: Clock = utc_now,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._governor = governor
        self._clock = clock
        self._http = httpx2.Client(
            base_url=settings.base_url,
            headers={"Authorization": settings.token.get_secret_value()},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- endpoints ----------------------------------------------------------------------
    def daily(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[DailyEnergy]:
        endpoint = f"daily_{direction}"
        path = f"/{endpoint}/{usage_point}/start/{start}/end/{end}"
        payload = self._get(usage_point, endpoint, path, cacheable=True)
        return parse_daily(payload, usage_point, direction, path)

    def load_curve(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[LoadCurvePoint]:
        endpoint = f"{direction}_load_curve"
        path = f"/{endpoint}/{usage_point}/start/{start}/end/{end}"
        payload = self._get(usage_point, endpoint, path, cacheable=True)
        return parse_load_curve(payload, usage_point, direction, path)

    def max_power(self, usage_point: str, start: date, end: date) -> list[PowerPeak]:
        endpoint = "daily_consumption_max_power"
        path = f"/{endpoint}/{usage_point}/start/{start}/end/{end}"
        payload = self._get(usage_point, endpoint, path, cacheable=True)
        return parse_max_power(payload, usage_point, path)

    def tempo(self, start: date, end: date) -> list[TempoDay]:
        path = f"/rte/tempo/{start}/{end}"
        return parse_tempo(self._get(RTE_BUCKET, "rte_tempo", path, cacheable=False), path)

    def ecowatt(self, start: date, end: date) -> list[EcowattDay]:
        path = f"/rte/ecowatt/{start}/{end}"
        return parse_ecowatt(self._get(RTE_BUCKET, "rte_ecowatt", path, cacheable=False), path)

    # -- plumbing -----------------------------------------------------------------------
    def _get(self, bucket: str, endpoint: str, path: str, *, cacheable: bool) -> Any:
        if cacheable and self._settings.prefer_cache:
            path = f"{path}/cache"
        call_id = self._governor.reserve(bucket, endpoint)
        try:
            response = self._http.get(path)
        except httpx2.HTTPError as exc:
            self._governor.settle(call_id, None)
            raise GatewayUnreachableError(
                f"{endpoint}: no answer from the gateway ({exc})"
            ) from exc
        self._governor.settle(call_id, response.status_code)
        return self._interpret(bucket, endpoint, response)

    def _interpret(self, bucket: str, endpoint: str, response: httpx2.Response) -> Any:
        status = response.status_code
        if status == 200:
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise GatewayError(f"{endpoint}: HTTP 200 without a JSON body") from exc
        if status in (401, 403):
            raise AuthError(f"{endpoint}: the gateway refused the token (HTTP {status})")
        if status == 429:
            retry_at = (
                parse_next_access(response.text)
                or _retry_after(response, self._clock())
                or self._clock() + _THROTTLE_FALLBACK
            )
            self._governor.block(bucket, retry_at, f"throttled upstream on {endpoint}")
            raise ThrottledError(
                f"{endpoint}: throttled upstream until {retry_at:%Y-%m-%d %H:%M} UTC", retry_at
            )
        if status == 409:
            retry_at = next_utc_midnight(self._clock())
            self._governor.block(bucket, retry_at, "the gateway's daily quota is spent")
            raise ThrottledError(f"{endpoint}: the gateway's daily quota is spent", retry_at)
        # The body stays out of errors and the journal (it may echo personal data).
        log.debug("%s: HTTP %d body: %.500s", endpoint, status, response.text)
        if status in (400, 404):
            raise WindowRejectedError(f"{endpoint}: the gateway refused the window (HTTP {status})")
        raise GatewayError(f"{endpoint}: unexpected HTTP {status}")


def _retry_after(response: httpx2.Response, now: datetime) -> datetime | None:
    raw = response.headers.get("Retry-After", "")
    return now + timedelta(seconds=int(raw)) if raw.isdigit() else None


def parse_next_access(body: str) -> datetime | None:
    """Enedis' `nextAccessTime`, possibly nested as an escaped JSON string in `detail`."""
    match = _NEXT_ACCESS.search(body.replace('\\"', '"'))
    if match is None:
        return None
    year, month_name, day, hour, minute, second = match.groups()
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    return datetime(int(year), month, int(day), int(hour), int(minute), int(second), tzinfo=UTC)


def parse_daily(
    payload: Any, usage_point: str, direction: Direction, path: str
) -> list[DailyEnergy]:
    readings = []
    for raw_date, value in _interval_readings(payload, path):
        day = _parse(date.fromisoformat, raw_date[:10], path, "date")
        readings.append(DailyEnergy(usage_point, direction, day, value))
    return readings


def parse_load_curve(
    payload: Any, usage_point: str, direction: Direction, path: str
) -> list[LoadCurvePoint]:
    """Load-curve points with their interval ENDS made unambiguous.

    Timestamps are Paris wall-clock times, so the hour repeated at the October
    fall-back appears twice: the second occurrence of a wall-clock time is the
    later instant (`fold=1`). Should a payload still map two readings onto one
    instant, the last one wins, loudly.
    """
    points: dict[datetime, LoadCurvePoint] = {}
    seen: set[datetime] = set()
    for raw_date, value in _interval_readings(payload, path):
        wall = _parse(datetime.fromisoformat, raw_date, path, "timestamp")
        if wall.tzinfo is not None:
            raise GatewayError(f"unexpected payload on {path}: timestamp carries an offset")
        end = wall.replace(tzinfo=PARIS, fold=int(wall in seen)).astimezone(UTC)
        seen.add(wall)
        if end in points:
            log.warning("%s: two readings end at %s; keeping the last", path, end.isoformat())
        points[end] = LoadCurvePoint(usage_point, direction, end, value)
    return sorted(points.values(), key=lambda point: point.end)


def parse_max_power(payload: Any, usage_point: str, path: str) -> list[PowerPeak]:
    peaks = []
    for raw_date, value in _interval_readings(payload, path):
        wall = _parse(datetime.fromisoformat, raw_date, path, "timestamp")
        at = wall.replace(tzinfo=PARIS) if wall.tzinfo is None else wall
        peaks.append(PowerPeak(usage_point, at.astimezone(PARIS).date(), value, at.astimezone(UTC)))
    return peaks


def parse_tempo(payload: Any, path: str) -> list[TempoDay]:
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object of days")
    days = []
    for raw_day, color in sorted(payload.items()):
        if not isinstance(color, str):
            raise GatewayError(f"unexpected payload on {path}: color of {raw_day} is not text")
        days.append(TempoDay(_parse(date.fromisoformat, str(raw_day), path, "date"), color.upper()))
    return days


def parse_ecowatt(payload: Any, path: str) -> list[EcowattDay]:
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object of days")
    days = []
    for raw_day, info in sorted(payload.items()):
        day = _parse(date.fromisoformat, str(raw_day), path, "date")
        if isinstance(info, dict) and isinstance(info.get("value"), int):
            days.append(EcowattDay(day, info["value"], str(info.get("message", ""))))
        elif isinstance(info, int) and not isinstance(info, bool):
            days.append(EcowattDay(day, info, ""))
        else:
            raise GatewayError(f"unexpected payload on {path}: bad Ecowatt signal for {raw_day}")
    return days


def _interval_readings(payload: Any, path: str) -> list[tuple[str, int]]:
    """The (date, integer value) pairs of Enedis' `meter_reading.interval_reading`."""
    try:
        readings = payload["meter_reading"]["interval_reading"]
    except (KeyError, TypeError) as exc:
        raise GatewayError(
            f"unexpected payload on {path}: no meter_reading.interval_reading"
        ) from exc
    if not isinstance(readings, list):
        raise GatewayError(f"unexpected payload on {path}: interval_reading is not a list")
    pairs = []
    for entry in readings:
        if not isinstance(entry, dict):
            raise GatewayError(f"unexpected payload on {path}: a reading is not an object")
        raw_date, raw_value = entry.get("date"), entry.get("value")
        if not isinstance(raw_date, str):
            raise GatewayError(f"unexpected payload on {path}: reading without a text date")
        pairs.append((raw_date, _whole_number(raw_value, path)))
    return pairs


def _whole_number(raw: object, path: str) -> int:
    """Enedis sends integers, usually as text ("5715"); anything else is refused."""
    if isinstance(raw, bool) or not isinstance(raw, str | int | float):
        raise GatewayError(f"unexpected payload on {path}: bad value {raw!r}")
    try:
        number = float(raw)
    except ValueError as exc:
        raise GatewayError(f"unexpected payload on {path}: bad value {raw!r}") from exc
    if not number.is_integer():
        raise GatewayError(f"unexpected payload on {path}: value {raw!r} is not a whole number")
    return int(number)


def _parse[T](parser: Callable[[str], T], raw: str, path: str, what: str) -> T:
    try:
        return parser(raw)
    except ValueError as exc:
        raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}") from exc
