"""Home Assistant long-term statistics exporter (WebSocket API).

Imports cumulative energy, in kWh, as external statistics through
`recorder/import_statistics`: one series per usage point and direction.

Ownership
    The first time a series is exported, the exporter asks Home Assistant for
    the last point it already holds — written by an earlier integration, for
    instance — and PINS that (day, sum) locally as the series' boundary, for
    this usage point. From then on it owns every day after the boundary, never
    touches anything before it, and continues the sum from the boundary's. A
    series pinned for another usage point is refused.

Granularity
    Every owned day, from the first to the last day with data, is written hour
    by hour, so a day can change granularity without leaving stale rows behind:
    * a day whose load curve is a complete, regular grid from midnight to
      midnight gets its measured hourly energy, from the curve;
    * any other day with a daily total stays flat until 23:00, where the whole
      day's energy lands: a day's total is only known once the day is over;
    * a day with neither stays flat.

Delivery
    Only series that changed since the last delivery are rewritten, from their
    earliest changed day on. Home Assistant upserts rows on their start, so a
    rewrite is idempotent. A failed exchange is an `ExportError`: nothing is
    ever imported from a guessed starting sum.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import batched
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from releve.clock import PARIS, at_paris_hour, day_start
from releve.config import HomeAssistantSettings, UsagePointSettings
from releve.curve import day_hours, hourly_energy
from releve.domain import DailyEnergy, Direction, LoadCurvePoint
from releve.errors import ExportError
from releve.store import HaBoundary, Store

log = logging.getLogger(__name__)

SINK_PREFIX = "home_assistant "
DAILY_STAMP_HOUR = 23
ROWS_PER_IMPORT = 10_000
TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# From this version on, statistic metadata carries `mean_type` and `unit_class`
# (older versions reject them; 2026.11 stops accepting their absence).
MEAN_TYPE_SINCE = (2025, 11)
_BEGINNING = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StatisticRow:
    start: datetime
    sum_kwh: float

    def as_json(self) -> dict[str, Any]:
        value = round(self.sum_kwh, 3)
        return {"start": self.start.isoformat(), "state": value, "sum": value}


@dataclass(frozen=True, slots=True)
class _Series:
    statistic_id: str
    name: str
    usage_point: str
    direction: Direction
    changed_from: date


class HomeAssistantExporter:
    name = "home_assistant"

    def __init__(
        self, settings: HomeAssistantSettings, usage_points: Sequence[UsagePointSettings]
    ) -> None:
        self._settings = settings
        self._usage_points = usage_points
        statistic_ids = f"{settings.statistic_id} {settings.production_statistic_id}"
        self.sink = f"{SINK_PREFIX}{settings.url} {statistic_ids}"

    def export(self, store: Store, *, after_run: int, up_to_run: int) -> str:
        pending = self._pending(store, after_run, up_to_run)
        if not pending:
            return "no change"
        try:
            with connect(
                self._settings.url,
                open_timeout=TIMEOUT_SECONDS,
                close_timeout=TIMEOUT_SECONDS,
                max_size=MAX_MESSAGE_BYTES,
            ) as connection:
                session = _Session(connection)
                session.authenticate(self._settings.token.get_secret_value())
                summaries = [self._write(session, store, series) for series in pending]
        except (OSError, ValueError, WebSocketException) as exc:
            raise ExportError(f"Home Assistant exchange failed: {exc}") from exc
        return "; ".join(summaries)

    def _pending(self, store: Store, after_run: int, up_to_run: int) -> list[_Series]:
        settings = self._settings
        pending = []
        for up in self._usage_points:
            candidates = (
                (
                    Direction.CONSUMPTION,
                    up.consumption or up.consumption_detail,
                    settings.statistic_id,
                    settings.statistic_name,
                ),
                (
                    Direction.PRODUCTION,
                    up.production or up.production_detail,
                    settings.production_statistic_id,
                    settings.production_statistic_name,
                ),
            )
            for direction, wanted, id_template, name_template in candidates:
                if not wanted:
                    continue
                changed = store.earliest_changed_day(
                    up.id, direction, after_run=after_run, up_to_run=up_to_run
                )
                if changed is not None:
                    pending.append(
                        _Series(
                            id_template.format(pdl=up.id),
                            name_template.format(pdl=up.id),
                            up.id,
                            direction,
                            changed,
                        )
                    )
        return pending

    def _write(self, session: _Session, store: Store, series: _Series) -> str:
        boundary, rewrite_from = self._boundary(session, store, series)
        first = boundary.base_day + timedelta(days=1) if boundary.base_day else date.min
        rows = series_rows(
            store.daily(series.usage_point, series.direction, first, date.max),
            store.curve(series.usage_point, series.direction, first, date.max),
            boundary,
        )
        if rewrite_from is not None:
            rows = [row for row in rows if row.start >= day_start(rewrite_from)]
        if not rows:
            return f"{series.statistic_id}: nothing after the boundary"
        metadata: dict[str, Any] = {
            "has_sum": True,
            "name": series.name,
            "source": series.statistic_id.split(":", 1)[0],
            "statistic_id": series.statistic_id,
            "unit_of_measurement": "kWh",
        }
        if session.version >= MEAN_TYPE_SINCE:
            metadata |= {"mean_type": 0, "unit_class": "energy"}
        else:
            metadata["has_mean"] = False
        for chunk in batched(rows, ROWS_PER_IMPORT):
            session.request(
                {
                    "type": "recorder/import_statistics",
                    "metadata": metadata,
                    "stats": [row.as_json() for row in chunk],
                }
            )
        return f"{series.statistic_id}: {len(rows)} hourly rows from {rows[0].start.date()}"

    def _boundary(
        self, session: _Session, store: Store, series: _Series
    ) -> tuple[HaBoundary, date | None]:
        """The series' boundary, pinning it if needed, and the first day to rewrite (None: all)."""
        sid = series.statistic_id
        boundary = store.ha_boundary(sid)
        if boundary is None:
            boundary = discover_boundary(session, sid, series.usage_point)
            store.set_ha_boundary(boundary, restart_sinks=None)
            log.warning(
                "%s: boundary pinned after %s at %.3f kWh",
                sid,
                boundary.base_day or "nothing (new series)",
                boundary.base_sum_kwh,
            )
            return boundary, None  # a new boundary: write everything it owns
        if boundary.usage_point is None:
            boundary = replace(boundary, usage_point=series.usage_point)
            store.set_ha_boundary(boundary, restart_sinks=None)
        elif boundary.usage_point != series.usage_point:
            raise ExportError(
                f"{sid} continues usage point {boundary.usage_point}, not {series.usage_point}: "
                "use another statistic_id, or pin a new boundary with `releve ha-boundary`"
            )
        return boundary, series.changed_from


def discover_boundary(session: _Session, statistic_id: str, usage_point: str) -> HaBoundary:
    """The last hourly (day, sum) Home Assistant holds for `statistic_id`, in kWh.

    Two requests keep the answer small whatever the series' age: the last month
    holding data, then the last hour within it. An empty series starts at 0.
    """
    last_month = _last_statistic(session, statistic_id, "month", _BEGINNING)
    if last_month is None:
        return HaBoundary(statistic_id, None, 0.0, usage_point)
    last_hour = _last_statistic(session, statistic_id, "hour", _instant(last_month, statistic_id))
    if last_hour is None:
        raise ExportError(f"{statistic_id}: a month holds data but none of its hours do")
    total = last_hour.get("sum")
    if not isinstance(total, int | float) or isinstance(total, bool):
        raise ExportError(f"cannot read the last point of {statistic_id}: {last_hour!r}")
    day = _instant(last_hour, statistic_id).astimezone(PARIS).date()
    return HaBoundary(statistic_id, day, float(total), usage_point)


def _last_statistic(
    session: _Session, statistic_id: str, period: str, start: datetime
) -> dict[str, Any] | None:
    result = session.request(
        {
            "type": "recorder/statistics_during_period",
            "start_time": start.isoformat(),
            "statistic_ids": [statistic_id],
            "period": period,
            "types": ["sum"],
            "units": {"energy": "kWh"},
        }
    )
    if not isinstance(result, dict):
        raise ExportError(f"unexpected statistics answer for {statistic_id}: {result!r}")
    rows = result.get(statistic_id, [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ExportError(f"unexpected statistics rows for {statistic_id}: {rows!r}")
    return rows[-1] if rows else None


def _instant(row: dict[str, Any], statistic_id: str) -> datetime:
    """Home Assistant returns `start` as epoch milliseconds (recent versions) or ISO text."""
    raw = row.get("start")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw / 1000, tz=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None:
                return parsed
    raise ExportError(f"cannot read the last point of {statistic_id}: {row!r}")


def series_rows(
    daily: Sequence[DailyEnergy], curve: Sequence[LoadCurvePoint], boundary: HaBoundary
) -> list[StatisticRow]:
    """Hourly cumulative rows for every owned day, continuing the boundary's sum."""
    totals = {reading.day: reading.wh for reading in daily}
    curve_by_day: dict[date, list[LoadCurvePoint]] = defaultdict(list)
    for point in curve:
        curve_by_day[point.day].append(point)
    owned = sorted(
        day
        for day in totals.keys() | curve_by_day.keys()
        if boundary.base_day is None or day > boundary.base_day
    )
    rows = []
    energy_wh = 0.0
    for offset in range((owned[-1] - owned[0]).days + 1 if owned else 0):
        day = owned[0] + timedelta(days=offset)
        hours = hourly_energy(day, curve_by_day.get(day, []))
        if hours is None:
            hours = daily_total_hours(day, totals.get(day, 0))
        for hour, wh in hours:
            energy_wh += wh
            rows.append(StatisticRow(hour, boundary.base_sum_kwh + energy_wh / 1000))
    return rows


def daily_total_hours(day: date, wh: int) -> list[tuple[datetime, float]]:
    """Every hour of `day` at zero, except 23:00 which carries the day's total."""
    stamp = at_paris_hour(day, DAILY_STAMP_HOUR)
    return [(hour, float(wh) if hour == stamp else 0.0) for hour in day_hours(day)]


class _Session:
    """Home Assistant's WebSocket protocol: authenticate, then numbered requests."""

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection
        self._last_id = 0
        self.version = (0, 0)

    def authenticate(self, token: str) -> None:
        greeting = self._receive()
        if greeting.get("type") != "auth_required":
            raise ExportError(f"unexpected greeting from Home Assistant: {greeting.get('type')!r}")
        self._send({"type": "auth", "access_token": token})
        verdict = self._receive()
        if verdict.get("type") != "auth_ok":
            raise ExportError("Home Assistant rejected the access token")
        match = re.match(r"(\d+)\.(\d+)", str(verdict.get("ha_version", "")))
        if match is None:
            raise ExportError(f"cannot tell the Home Assistant version: {verdict!r}")
        self.version = (int(match.group(1)), int(match.group(2)))

    def request(self, payload: dict[str, Any]) -> Any:
        self._last_id += 1
        request_id = self._last_id
        self._send({"id": request_id, **payload})
        while True:
            reply = self._receive()
            if reply.get("id") == request_id and reply.get("type") == "result":
                break
        if not reply.get("success"):
            raise ExportError(f"{payload['type']} refused: {reply.get('error')}")
        return reply.get("result")

    def _send(self, message: dict[str, Any]) -> None:
        self._connection.send(json.dumps(message))

    def _receive(self) -> dict[str, Any]:
        message = json.loads(self._connection.recv(timeout=TIMEOUT_SECONDS))
        if not isinstance(message, dict):
            raise ExportError(f"unexpected message from Home Assistant: {message!r}")
        return message
