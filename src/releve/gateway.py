"""The MyElectricalData gateway client.

The only module that speaks HTTP and JSON with the gateway. Every call passes
the quota governor first; every answer is parsed here into domain types or
rejected with a typed error. An upstream throttle becomes a persisted block,
never a retry.

Endpoints used (public API facts):
    GET  /valid_access/{pdl}
    GET  /contracts|identity|contact|addresses/{pdl}[/cache]
    GET  /daily_{consumption,production}/{pdl}/start/{d}/end/{d}[/cache]
    GET  /{consumption,production}_load_curve/{pdl}/start/{d}/end/{d}[/cache]
    GET  /daily_consumption_max_power/{pdl}/start/{d}/end/{d}[/cache]
    GET  /rte/tempo/{d}/{d}
    GET  /rte/ecowatt/{d}/{d}
    GET  /edf/tempo/price
    GET  /edf/tempo/days
    DELETE /cache/{pdl}
    DELETE /{resource}/{pdl}/cache
    DELETE /{resource}/{pdl}/start/{d}/end/{d}/cache
`end` is exclusive. Load-curve dates are interval ENDS in Paris wall-clock time.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Protocol, Self

import httpx2

from releve.clock import PARIS, Clock, next_utc_midnight, utc_now
from releve.config import GatewaySettings
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
ONE_DAY = timedelta(days=1)
_TEMPO_PRICE_KEYS = {
    ("blue", "hc"): (TempoColor.BLUE, Period.OFFPEAK),
    ("blue", "hp"): (TempoColor.BLUE, Period.PEAK),
    ("white", "hc"): (TempoColor.WHITE, Period.OFFPEAK),
    ("white", "hp"): (TempoColor.WHITE, Period.PEAK),
    ("red", "hc"): (TempoColor.RED, Period.OFFPEAK),
    ("red", "hp"): (TempoColor.RED, Period.PEAK),
}


class Gateway(Protocol):
    """What the sync pass needs from the gateway."""

    def valid_access(self, usage_point: str) -> Consent: ...

    def contract(self, usage_point: str) -> Contract: ...

    def identity(self, usage_point: str) -> Identity: ...

    def contact(self, usage_point: str) -> Contact: ...

    def addresses(self, usage_point: str) -> Address: ...

    def daily(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[DailyEnergy]: ...

    def load_curve(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[LoadCurvePoint]: ...

    def max_power(self, usage_point: str, start: date, end: date) -> list[PowerPeak]: ...

    def tempo(self, start: date, end: date) -> list[TempoDay]: ...

    def ecowatt(self, start: date, end: date) -> EcowattForecast: ...

    def tempo_prices(self) -> list[TempoPrice]: ...

    def tempo_season(self) -> TempoSeason: ...

    def delete_cache(
        self,
        usage_point: str,
        resource: CacheResource,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> None: ...


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
    def valid_access(self, usage_point: str) -> Consent:
        path = f"/valid_access/{usage_point}"
        return parse_consent(
            self._get(usage_point, "valid_access", path, cacheable=False), usage_point, path
        )

    def contract(self, usage_point: str) -> Contract:
        path = f"/contracts/{usage_point}"
        return parse_contract(
            self._get(usage_point, "contracts", path, cacheable=True), usage_point, path
        )

    def identity(self, usage_point: str) -> Identity:
        path = f"/identity/{usage_point}"
        return parse_identity(
            self._get(usage_point, "identity", path, cacheable=True), usage_point, path
        )

    def contact(self, usage_point: str) -> Contact:
        path = f"/contact/{usage_point}"
        return parse_contact(
            self._get(usage_point, "contact", path, cacheable=True), usage_point, path
        )

    def addresses(self, usage_point: str) -> Address:
        path = f"/addresses/{usage_point}"
        return parse_address(
            self._get(usage_point, "addresses", path, cacheable=True), usage_point, path
        )

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

    def ecowatt(self, start: date, end: date) -> EcowattForecast:
        """The Ecowatt signals of the Paris days [start, end).

        The gateway keys each day one day early and filters the range on that
        key, so the range asked is one day earlier than the days wanted.
        """
        path = f"/rte/ecowatt/{start - ONE_DAY}/{end - ONE_DAY}"
        return parse_ecowatt(self._get(RTE_BUCKET, "rte_ecowatt", path, cacheable=False), path)

    def tempo_prices(self) -> list[TempoPrice]:
        path = "/edf/tempo/price"
        return parse_tempo_prices(
            self._get(RTE_BUCKET, "edf_tempo_price", path, cacheable=False), path
        )

    def tempo_season(self) -> TempoSeason:
        path = "/edf/tempo/days"
        return parse_tempo_season(
            self._get(RTE_BUCKET, "edf_tempo_days", path, cacheable=False), path
        )

    def delete_cache(
        self,
        usage_point: str,
        resource: CacheResource,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> None:
        """Delete the gateway's remote cache for a resource (counts against the quota)."""
        if resource.needs_dates:
            if start is None or end is None or not start < end:
                raise GatewayError(f"{resource}: deleting a dated cache needs start < end")
            path = f"/{resource}/{usage_point}/start/{start}/end/{end}/cache"
        elif resource is CacheResource.ALL:
            path = f"/cache/{usage_point}"
        else:
            path = f"/{resource}/{usage_point}/cache"
        self._request("DELETE", usage_point, f"delete_{resource}", path)

    # -- plumbing -----------------------------------------------------------------------
    def _get(self, bucket: str, endpoint: str, path: str, *, cacheable: bool) -> Any:
        if cacheable and self._settings.prefer_cache:
            path = f"{path}/cache"
        return self._request("GET", bucket, endpoint, path)

    def _request(self, method: str, bucket: str, endpoint: str, path: str) -> Any:
        call_id = self._governor.reserve(bucket, endpoint)
        try:
            response = self._http.request(method, path)
        except httpx2.HTTPError as exc:
            self._governor.settle(call_id, None)
            raise GatewayUnreachableError(
                f"{endpoint}: no answer from the gateway ({exc})"
            ) from exc
        self._governor.settle(call_id, response.status_code)
        return self._interpret(bucket, endpoint, response)

    def _interpret(self, bucket: str, endpoint: str, response: httpx2.Response) -> Any:
        status = response.status_code
        if status == 204:
            return None  # e.g. a DELETE that succeeded without a body
        if status == 200:
            if not response.content:
                return None
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


def parse_consent(payload: Any, usage_point: str, path: str) -> Consent:
    """The consent, read strictly where it decides and leniently where it informs.

    `valid` and `ban` decide whether releve may read the usage point: an answer
    without `valid`, or with either flag unreadable, is refused. The other
    fields only inform; one that cannot be read is logged and left unknown, so
    it never keeps metering from running.
    """
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object")
    if payload.get("valid") is None:
        raise GatewayError(f"unexpected payload on {path}: no valid flag")

    def informational[T](read: Callable[[object, str, str], T], key: str, unknown: T) -> T:
        try:
            return read(payload.get(key), path, key)
        except GatewayError as exc:
            log.warning("%s: ignoring an unreadable informational field: %s", path, exc)
            return unknown

    return Consent(
        usage_point,
        valid=_flag(payload["valid"], path, "valid"),
        expires_at=informational(_optional_datetime, "consent_expiration_date", None),
        call_number=informational(_optional_int, "call_number", None),
        quota_limit=informational(_optional_int, "quota_limit", None),
        quota_reached=informational(_flag, "quota_reached", False),
        quota_reset_at=informational(_optional_datetime, "quota_reset_at", None),
        last_call_at=informational(_optional_datetime, "last_call", None),
        banned=_flag(payload.get("ban"), path, "ban"),
        information=str(payload.get("information") or ""),
    )


def parse_contract(payload: Any, usage_point: str, path: str) -> Contract:
    point, entry = _customer_usage_point(payload, usage_point, path)
    contracts = entry.get("contracts")
    if not isinstance(contracts, dict):
        raise GatewayError(f"unexpected payload on {path}: missing contracts object")
    return Contract(
        usage_point,
        segment=_optional_text(contracts.get("segment")),
        subscribed_power=_optional_text(contracts.get("subscribed_power")),
        distribution_tariff=_optional_text(contracts.get("distribution_tariff")),
        offpeak_hours=_optional_text(contracts.get("offpeak_hours")),
        contract_status=_optional_text(contracts.get("contract_status")),
        last_activation_date=_optional_text(contracts.get("last_activation_date")),
        last_tariff_change_date=_optional_text(
            contracts.get("last_distribution_tariff_change_date")
        ),
        meter_type=_optional_text(point.get("meter_type")),
        usage_point_status=_optional_text(point.get("usage_point_status")),
    )


def parse_identity(payload: Any, usage_point: str, path: str) -> Identity:
    holder = _customer(payload, path)
    identity = holder.get("identity")
    person = identity.get("natural_person") if isinstance(identity, dict) else None
    if not isinstance(person, dict):
        person = {}
    return Identity(
        usage_point,
        customer_id=_optional_text(holder.get("customer_id")),
        title=_optional_text(person.get("title")),
        firstname=_optional_text(person.get("firstname")),
        lastname=_optional_text(person.get("lastname")),
    )


def parse_contact(payload: Any, usage_point: str, path: str) -> Contact:
    holder = _customer(payload, path)
    data = holder.get("contact_data")
    if not isinstance(data, dict):
        data = {}
    return Contact(
        usage_point,
        customer_id=_optional_text(holder.get("customer_id")),
        phone=_optional_text(data.get("phone")),
        email=_optional_text(data.get("email")),
    )


def parse_address(payload: Any, usage_point: str, path: str) -> Address:
    point, _ = _customer_usage_point(payload, usage_point, path)
    address = point.get("usage_point_addresses")
    if not isinstance(address, dict):
        address = {}
    geo = address.get("geo_points")
    if not isinstance(geo, dict):
        geo = {}
    return Address(
        usage_point,
        customer_id=_optional_text(payload["customer"].get("customer_id")),
        street=_optional_text(address.get("street")),
        locality=_optional_text(address.get("locality")),
        postal_code=_optional_text(address.get("postal_code")),
        insee_code=_optional_text(address.get("insee_code")),
        city=_optional_text(address.get("city")),
        country=_optional_text(address.get("country")),
        latitude=_optional_text(geo.get("latitude")),
        longitude=_optional_text(geo.get("longitude")),
        altitude=_optional_text(geo.get("altitude")),
        meter_type=_optional_text(point.get("meter_type")),
        usage_point_status=_optional_text(point.get("usage_point_status")),
    )


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


def parse_ecowatt(payload: Any, path: str) -> EcowattForecast:
    """Daily and hourly signals, each day dated by its hourly detail.

    The gateway keys each day one day early (observed live: the key 2026-09-12
    holds the hours of 2026-09-13, in Paris wall-clock time). A day without
    detail is dated by its key plus one day.
    """
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object of days")
    days: list[EcowattDay] = []
    hours: list[EcowattHour] = []
    for raw_day, info in sorted(payload.items()):
        key_day = _parse(date.fromisoformat, str(raw_day), path, "date")
        if isinstance(info, dict) and isinstance(info.get("value"), int):
            level, message = info["value"], str(info.get("message", ""))
            detail = info.get("detail")
        elif isinstance(info, int) and not isinstance(info, bool):
            level, message, detail = info, "", None
        else:
            raise GatewayError(f"unexpected payload on {path}: bad Ecowatt signal for {raw_day}")
        day_hours = _parse_ecowatt_hours(detail, path)
        hours.extend(day_hours)
        first = min((hour.at for hour in day_hours), default=None)
        day = first.astimezone(PARIS).date() if first is not None else key_day + ONE_DAY
        days.append(EcowattDay(day, level, message))
    hours.sort(key=lambda hour: hour.at)
    return EcowattForecast(tuple(days), tuple(hours))


def parse_tempo_prices(payload: Any, path: str) -> list[TempoPrice]:
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object of prices")
    prices = []
    for key, raw in payload.items():
        if not isinstance(key, str) or "_" not in key:
            raise GatewayError(f"unexpected payload on {path}: bad price key {key!r}")
        color_name, period_name = key.lower().split("_", 1)
        mapped = _TEMPO_PRICE_KEYS.get((color_name, period_name))
        if mapped is None:
            raise GatewayError(f"unexpected payload on {path}: unknown price key {key!r}")
        color, period = mapped
        try:
            amount = Decimal(str(raw))
        except InvalidOperation as exc:
            raise GatewayError(f"unexpected payload on {path}: bad price {raw!r}") from exc
        prices.append(TempoPrice(color, period, amount))
    return prices


def parse_tempo_season(payload: Any, path: str) -> TempoSeason:
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object of colors")
    days_left: dict[TempoColor, int] = {}
    for color in TempoColor:
        raw = payload.get(color.value.lower())
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise GatewayError(f"unexpected payload on {path}: bad days left for {color}")
        days_left[color] = raw
    return TempoSeason(days_left)


def _parse_ecowatt_hours(detail: Any, path: str) -> list[EcowattHour]:
    """Hourly levels keyed by Paris wall-clock time.

    On the March switch the wall-clock hour 02:00 does not exist: a level stamped
    at it is dropped, as it would land on the instant of 03:00. On the October
    switch the object can hold 02:00 only once; it is read as the first 02:00.
    """
    if detail is None:
        return []
    if not isinstance(detail, dict):
        raise GatewayError(f"unexpected payload on {path}: Ecowatt detail is not an object")
    hours = []
    for raw_at, level in detail.items():
        if not isinstance(level, int) or isinstance(level, bool):
            raise GatewayError(f"unexpected payload on {path}: bad hourly Ecowatt level")
        wall = _parse(datetime.fromisoformat, str(raw_at), path, "timestamp")
        if wall.tzinfo is not None:
            hours.append(EcowattHour(wall.astimezone(UTC), level))
            continue
        at = wall.replace(tzinfo=PARIS).astimezone(UTC)
        if at.astimezone(PARIS).replace(tzinfo=None) != wall:
            log.debug("%s: skipping Ecowatt level at nonexistent Paris time %s", path, wall)
            continue
        hours.append(EcowattHour(at, level))
    return hours


def _customer(payload: Any, path: str) -> dict[str, Any]:
    """The customer object, whether or not the answer wraps it in `customer`."""
    if not isinstance(payload, dict):
        raise GatewayError(f"unexpected payload on {path}: expected an object")
    customer = payload.get("customer")
    return customer if isinstance(customer, dict) else payload


def _customer_usage_point(
    payload: Any, usage_point: str, path: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The usage-point object matching `usage_point` under `customer`, and its entry."""
    if not isinstance(payload, dict) or not isinstance(payload.get("customer"), dict):
        raise GatewayError(f"unexpected payload on {path}: no customer object")
    points = payload["customer"].get("usage_points")
    if not isinstance(points, list) or not points:
        raise GatewayError(f"unexpected payload on {path}: no usage_points")
    for entry in points:
        if not isinstance(entry, dict) or not isinstance(entry.get("usage_point"), dict):
            raise GatewayError(f"unexpected payload on {path}: a usage point is not an object")
        point = entry["usage_point"]
        answered = point.get("usage_point_id")
        if str(answered) == usage_point:
            return point, entry
        # Some answers hold a single point without repeating the id we asked for;
        # a point carrying ANOTHER id belongs to another meter and is never taken.
        if answered is None and len(points) == 1:
            return point, entry
    raise GatewayError(f"unexpected payload on {path}: usage point {usage_point} not in answer")


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


def _optional_text(raw: object) -> str | None:
    return None if raw is None else str(raw)


def _optional_int(raw: object, path: str, what: str) -> int | None:
    """An integer, as JSON or as decimal text ("6"), like the rest of Enedis' numbers."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().isdecimal():
        return int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}")
    return raw


def _optional_datetime(raw: object, path: str, what: str) -> datetime | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}")
    text = raw.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}") from exc
    if parsed.tzinfo is None:
        # Gateway timestamps without an offset are UTC civil times.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _flag(raw: object, path: str, what: str) -> bool:
    """A boolean the gateway may send as JSON, 0/1 or text; absent is False, "false" never True."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0"):
        return raw.strip().lower() in ("true", "1")
    raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}")


def _parse[T](parser: Callable[[str], T], raw: str, path: str, what: str) -> T:
    try:
        return parser(raw)
    except ValueError as exc:
        raise GatewayError(f"unexpected payload on {path}: bad {what} {raw!r}") from exc
