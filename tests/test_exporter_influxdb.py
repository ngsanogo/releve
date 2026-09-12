"""InfluxDB / VictoriaMetrics: line protocol and incremental delivery."""

from __future__ import annotations

from datetime import date, timedelta

import httpx2
import pytest

from releve.clock import at_paris_hour, day_start
from releve.config import InfluxSettings
from releve.domain import DailyEnergy, Direction, LoadCurvePoint, PowerPeak
from releve.errors import ExportError
from releve.exporters.influxdb import LINES_PER_REQUEST, InfluxExporter, line_protocol
from releve.store import Store
from tests.conftest import NOW, PDL

DAY = date(2026, 9, 10)
C = Direction.CONSUMPTION


def test_line_protocol_series_and_escaping() -> None:
    end = day_start(DAY) + timedelta(minutes=30)
    lines = line_protocol(
        "my energy,v2",
        [DailyEnergy(PDL, C, DAY, 5715)],
        [LoadCurvePoint(PDL, Direction.PRODUCTION, end, 752)],
        [PowerPeak(PDL, DAY, 6100, at_paris_hour(DAY, 19))],
    )
    assert lines == [
        f"my\\ energy\\,v2,usage_point={PDL},direction=consumption,kind=daily wh=5715i 1788991200",
        (
            f"my\\ energy\\,v2,usage_point={PDL},direction=production,kind=load_curve"
            " watts=752i 1788993000"
        ),
        f"my\\ energy\\,v2,usage_point={PDL},kind=max_power va=6100i 1788991200",
    ]


class Endpoint:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, text="" if self.status < 300 else "bucket not found")


def exporter(endpoint: Endpoint, **settings: str) -> InfluxExporter:
    configured = InfluxSettings(enabled=True, url="http://influx.test/api/v2/write", **settings)
    return InfluxExporter(configured, transport=httpx2.MockTransport(endpoint))


def test_changes_are_written_in_batches_with_credentials(store: Store) -> None:
    run = store.start_run(NOW)
    curve = [
        LoadCurvePoint(PDL, C, day_start(DAY) + timedelta(minutes=30 * k), k)
        for k in range(1, LINES_PER_REQUEST + 2)
    ]
    store.upsert_curve(run, curve)
    endpoint = Endpoint()

    summary = exporter(endpoint, token="tok", org="home", bucket="energy").export(
        store, after_run=0, up_to_run=run
    )

    assert summary == f"{LINES_PER_REQUEST + 1} points written"
    assert [len(r.content.splitlines()) for r in endpoint.requests] == [LINES_PER_REQUEST, 1]
    first = endpoint.requests[0]
    assert first.headers["Authorization"] == "Token tok"
    assert dict(first.url.params) == {"precision": "s", "org": "home", "bucket": "energy"}


def test_nothing_changed_means_no_request(store: Store) -> None:
    endpoint = Endpoint()
    assert exporter(endpoint).export(store, after_run=0, up_to_run=5) == "no change"
    assert endpoint.requests == []


def test_a_refusal_or_an_unreachable_endpoint_is_an_export_error(store: Store) -> None:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(PDL, C, DAY, 1)])
    with pytest.raises(ExportError, match="HTTP 404"):
        exporter(Endpoint(404)).export(store, after_run=0, up_to_run=run)

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    offline = InfluxExporter(
        InfluxSettings(enabled=True), transport=httpx2.MockTransport(unreachable)
    )
    with pytest.raises(ExportError, match="unreachable"):
        offline.export(store, after_run=0, up_to_run=run)
