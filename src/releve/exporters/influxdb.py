"""InfluxDB v2 / VictoriaMetrics exporter: Influx line protocol over HTTP.

Both accept line protocol on `/api/v2/write`, so there is no client library:
the exporter builds lines and POSTs them, in batches. It sends what changed
since its last successful delivery, so a write that fails is sent again.

Series (tags `usage_point`, `direction`, `kind`):
* `kind=daily`      field `wh`,    at the start of the Paris day;
* `kind=load_curve` field `watts`, at the END of the metering interval;
* `kind=max_power`  field `va`,    at the start of the Paris day.

A load-curve point the cache dropped (see `Store.upsert_curve`) is deleted
first, through `/api/v2/delete` next to the write endpoint. VictoriaMetrics
has no such endpoint: a destination that refuses a deletion is not asked
again, and the delivery's summary says which points it still holds.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import Any

import httpx2

from releve.clock import day_start, to_unix
from releve.config import InfluxSettings
from releve.domain import DailyEnergy, LoadCurvePoint, PowerPeak
from releve.errors import ExportError
from releve.store import Store

log = logging.getLogger(__name__)

LINES_PER_REQUEST = 5000
TIMEOUT_SECONDS = 30.0
# Points sit on whole minutes: [end, end + 1 s] holds the dropped point and no other.
_DELETE_SPAN = timedelta(seconds=1)


class InfluxExporter:
    name = "influxdb"

    def __init__(
        self, settings: InfluxSettings, *, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self.sink = (
            f"influxdb {settings.url} {settings.org} {settings.bucket} {settings.measurement}"
        )

    def export(self, store: Store, *, after_run: int, up_to_run: int) -> str:
        dropped = store.removed_curve(after_run=after_run, up_to_run=up_to_run)
        lines = line_protocol(
            self._settings.measurement,
            store.changed_daily(after_run=after_run, up_to_run=up_to_run),
            store.changed_curve(after_run=after_run, up_to_run=up_to_run),
            store.changed_peaks(after_run=after_run, up_to_run=up_to_run),
        )
        if not dropped and not lines:
            return "no change"
        with httpx2.Client(timeout=TIMEOUT_SECONDS, transport=self._transport) as http:
            refusal = self._delete(http, dropped)
            for batch in batched(lines, LINES_PER_REQUEST):
                self._write(http, batch)
        summary = f"{len(lines)} points written"
        if refusal is not None:
            return f"{summary}; {len(dropped)} dropped load-curve points NOT deleted: {refusal}"
        if dropped:
            return f"{summary}, {len(dropped)} dropped load-curve points deleted"
        return summary

    def _write(self, http: httpx2.Client, lines: Sequence[str]) -> None:
        response = self._post(
            http,
            self._settings.url,
            {"precision": "s"},
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content="\n".join(lines),
        )
        if not response.is_success:
            raise ExportError(f"write refused (HTTP {response.status_code}): {response.text[:200]}")

    def _delete(self, http: httpx2.Client, points: Sequence[LoadCurvePoint]) -> str | None:
        """Delete `points`; returns why the destination refused to, or None.

        A refusal (HTTP 4xx) is final — the destination cannot or will not
        delete — and logged; an unreachable or failing destination is an error.
        """
        if not points:
            return None
        url = httpx2.URL(self._settings.url)
        if not url.path.endswith("/write"):
            refusal = f"no delete endpoint next to {self._settings.url}"
            log.warning("%s: %d dropped load-curve points not deleted", refusal, len(points))
            return refusal
        endpoint = str(url.copy_with(path=url.path.removesuffix("/write") + "/delete"))
        for point in points:
            body = {
                "start": _rfc3339(point.end),
                "stop": _rfc3339(point.end + _DELETE_SPAN),
                "predicate": delete_predicate(self._settings.measurement, point),
            }
            response = self._post(http, endpoint, {}, json=body)
            if response.is_client_error:
                refusal = f"{endpoint} refused (HTTP {response.status_code})"
                log.warning("%s: %d dropped load-curve points not deleted", refusal, len(points))
                return refusal
            if not response.is_success:
                raise ExportError(
                    f"delete failed (HTTP {response.status_code}): {response.text[:200]}"
                )
        return None

    def _post(
        self,
        http: httpx2.Client,
        url: str,
        params: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        content: str | None = None,
        json: Any = None,
    ) -> httpx2.Response:
        settings = self._settings
        headers = dict(headers or {})
        if token := settings.token.get_secret_value():
            headers["Authorization"] = f"Token {token}"
        params = dict(params)
        if settings.org:
            params["org"] = settings.org
        if settings.bucket:
            params["bucket"] = settings.bucket
        try:
            return http.post(url, params=params, headers=headers, content=content, json=json)
        except httpx2.HTTPError as exc:
            raise ExportError(f"{url} unreachable: {exc}") from exc


def line_protocol(
    measurement: str,
    daily: Sequence[DailyEnergy],
    curve: Sequence[LoadCurvePoint],
    peaks: Sequence[PowerPeak],
) -> list[str]:
    name = measurement.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")
    lines = [
        f"{name},usage_point={_tag(r.usage_point)},direction={r.direction},kind=daily"
        f" wh={r.wh}i {to_unix(day_start(r.day))}"
        for r in daily
    ]
    lines += [
        f"{name},usage_point={_tag(p.usage_point)},direction={p.direction},kind=load_curve"
        f" watts={p.watts}i {to_unix(p.end)}"
        for p in curve
    ]
    lines += [
        f"{name},usage_point={_tag(p.usage_point)},kind=max_power"
        f" va={p.va}i {to_unix(day_start(p.day))}"
        for p in peaks
    ]
    return lines


def delete_predicate(measurement: str, point: LoadCurvePoint) -> str:
    """The `/api/v2/delete` predicate matching the load-curve series of `point`."""
    terms = {
        "_measurement": measurement,
        "usage_point": point.usage_point,
        "direction": point.direction,
        "kind": "load_curve",
    }
    return " AND ".join(f'{key}="{_quoted(value)}"' for key, value in terms.items())


def _tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
