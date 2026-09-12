"""InfluxDB v2 / VictoriaMetrics exporter: Influx line protocol over HTTP.

Both accept line protocol on `/api/v2/write`, so there is no client library:
the exporter builds lines and POSTs them, in batches. It sends what changed
since its last successful delivery, so a write that fails is sent again.

Series (tags `usage_point`, `direction`, `kind`):
* `kind=daily`      field `wh`,    at the start of the Paris day;
* `kind=load_curve` field `watts`, at the END of the metering interval;
* `kind=max_power`  field `va`,    at the start of the Paris day.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import batched

import httpx2

from releve.clock import day_start, to_unix
from releve.config import InfluxSettings
from releve.domain import DailyEnergy, LoadCurvePoint, PowerPeak
from releve.errors import ExportError
from releve.store import Store

LINES_PER_REQUEST = 5000
TIMEOUT_SECONDS = 30.0


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
        lines = line_protocol(
            self._settings.measurement,
            store.changed_daily(after_run=after_run, up_to_run=up_to_run),
            store.changed_curve(after_run=after_run, up_to_run=up_to_run),
            store.changed_peaks(after_run=after_run, up_to_run=up_to_run),
        )
        if not lines:
            return "no change"
        with httpx2.Client(timeout=TIMEOUT_SECONDS, transport=self._transport) as http:
            for batch in batched(lines, LINES_PER_REQUEST):
                self._write(http, batch)
        return f"{len(lines)} points written"

    def _write(self, http: httpx2.Client, lines: Sequence[str]) -> None:
        settings = self._settings
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if token := settings.token.get_secret_value():
            headers["Authorization"] = f"Token {token}"
        params = {"precision": "s"}
        if settings.org:
            params["org"] = settings.org
        if settings.bucket:
            params["bucket"] = settings.bucket
        try:
            response = http.post(
                settings.url, params=params, headers=headers, content="\n".join(lines)
            )
        except httpx2.HTTPError as exc:
            raise ExportError(f"write endpoint {settings.url} unreachable: {exc}") from exc
        if not response.is_success:
            raise ExportError(f"write refused (HTTP {response.status_code}): {response.text[:200]}")


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


def _tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")
