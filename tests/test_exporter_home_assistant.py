"""Home Assistant statistics: the arithmetic, and the WebSocket exchange with a fake server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, serve

from releve.clock import at_paris_hour, day_start
from releve.config import HomeAssistantSettings, UsagePointSettings
from releve.curve import hourly_energy, incomplete_days
from releve.domain import DailyEnergy, Direction, LoadCurvePoint
from releve.errors import ExportError
from releve.exporters.home_assistant import (
    HomeAssistantExporter,
    daily_total_hours,
    series_rows,
)
from releve.store import HaBoundary, Store
from tests.conftest import NOW, PDL
from tests.fakes import curve_of

C = Direction.CONSUMPTION
DAY = date(2026, 9, 10)
SERIES = f"releve:{PDL}_consumption"


# -- arithmetic -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("day", "hours"), [(DAY, 24), (date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_a_complete_curve_gives_one_value_per_hour_of_the_day(day: date, hours: int) -> None:
    points = curve_of(PDL, C, day)
    energy = hourly_energy(day, points)
    assert energy is not None
    assert len(energy) == hours
    assert energy[0][0] == day_start(day)
    assert sum(wh for _, wh in energy) == sum(p.watts * 0.5 for p in points)


def test_an_incomplete_or_irregular_curve_is_not_guessed() -> None:
    points = curve_of(PDL, C, DAY)
    assert hourly_energy(DAY, []) is None
    assert hourly_energy(DAY, points[:-1]) is None  # a point missing
    shifted = [*points[:-1], LoadCurvePoint(PDL, C, points[-1].end - timedelta(minutes=10), 1)]
    assert hourly_energy(DAY, shifted) is None  # right count, wrong grid
    hourly = [
        LoadCurvePoint(PDL, C, day_start(DAY) + timedelta(hours=h), 1000) for h in range(1, 25)
    ]
    assert hourly_energy(DAY, hourly) == [
        (day_start(DAY) + timedelta(hours=h), 1000.0) for h in range(24)
    ]
    two = [LoadCurvePoint(PDL, C, day_start(DAY) + timedelta(hours=12 * k), 1) for k in (1, 2)]
    assert hourly_energy(DAY, two) is None  # a 12-hour step does not divide an hour


def test_incomplete_days_names_only_the_days_whose_curve_is_not_a_grid() -> None:
    complete = date(2026, 9, 10)
    partial = date(2026, 9, 11)
    points = curve_of(PDL, C, complete) + curve_of(PDL, C, partial)[:24]
    assert incomplete_days(points) == {partial}
    assert incomplete_days(curve_of(PDL, C, complete)) == set()
    assert incomplete_days([]) == set()


def test_a_day_without_a_complete_curve_lands_at_23_00() -> None:
    hours = daily_total_hours(DAY, 9100)
    assert len(hours) == 24
    assert [(h, wh) for h, wh in hours if wh] == [(at_paris_hour(DAY, 23), 9100.0)]
    assert len(daily_total_hours(date(2026, 3, 29), 1)) == 23


def test_rows_continue_the_boundary_and_keep_the_curve_total_on_curve_days() -> None:
    boundary = HaBoundary(SERIES, DAY - timedelta(days=1), 8481.922000000004)
    before = DailyEnergy(PDL, C, DAY - timedelta(days=1), 99_999)  # at the boundary: not ours
    daily = [
        before,
        DailyEnergy(PDL, C, DAY, 9100),
        DailyEnergy(PDL, C, DAY + timedelta(days=1), 7000),
    ]
    curve = curve_of(PDL, C, DAY)  # DAY has a complete curve, DAY+1 does not
    curve_total = sum(p.watts * 0.5 for p in curve)

    rows = series_rows(daily, curve, boundary)

    assert len(rows) == 48
    assert rows[0].start == day_start(DAY)
    assert rows[23].sum_kwh == pytest.approx(8481.922000000004 + curve_total / 1000)
    assert rows[46].sum_kwh == rows[23].sum_kwh  # flat until 23:00 on the daily-only day
    assert rows[47].start == at_paris_hour(DAY + timedelta(days=1), 23)
    assert rows[47].sum_kwh == pytest.approx(rows[23].sum_kwh + 7.0)
    assert rows[47].as_json() == {
        "start": "2026-09-11T21:00:00+00:00",
        "state": round(rows[47].sum_kwh, 3),
        "sum": round(rows[47].sum_kwh, 3),
    }


# -- WebSocket exchange -------------------------------------------------------------------
def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@dataclass
class FakeHomeAssistant:
    """Speaks just enough of Home Assistant's WebSocket API."""

    token: str = "ha-token"
    version: str = "2026.9.1"
    # Hourly rows per statistic id: {"start": epoch ms, "sum": kWh}.
    existing: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refuse_statistics: bool = False
    odd_statistics_answer: bool = False
    received: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""

    def handle(self, connection: ServerConnection) -> None:
        connection.send(json.dumps({"type": "auth_required", "ha_version": self.version}))
        auth = json.loads(connection.recv())
        if auth.get("access_token") != self.token:
            connection.send(json.dumps({"type": "auth_invalid"}))
            return
        connection.send(json.dumps({"type": "auth_ok", "ha_version": self.version}))
        for raw in connection:
            message = json.loads(raw)
            self.received.append(message)
            connection.send(json.dumps({"type": "event", "event": {}}))  # unsolicited noise
            connection.send(json.dumps(self.answer(message)))

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        reply: dict[str, Any] = {"id": message["id"], "type": "result", "success": True}
        if message["type"] != "recorder/statistics_during_period":
            return {**reply, "result": None}
        if self.refuse_statistics:
            return {**reply, "success": False, "error": {"code": "unknown_error"}}
        if self.odd_statistics_answer:
            return {**reply, "result": []}
        wanted = message["statistic_ids"][0]
        since = ms(datetime.fromisoformat(message["start_time"]))
        rows = [row for row in self.existing.get(wanted, []) if row["start"] >= since]
        if message["period"] == "month" and rows:
            last = datetime.fromtimestamp(rows[-1]["start"] / 1000, tz=UTC)
            rows = [{"start": ms(last.replace(day=1, hour=0)), "sum": rows[-1]["sum"]}]
        return {**reply, "result": {wanted: rows} if rows else {}}

    def imports(self) -> list[dict[str, Any]]:
        return [m for m in self.received if m["type"] == "recorder/import_statistics"]


@pytest.fixture
def home_assistant() -> Iterator[FakeHomeAssistant]:
    fake = FakeHomeAssistant()
    with serve(fake.handle, "127.0.0.1", 0) as server:
        host, port = server.socket.getsockname()[:2]
        fake.url = f"ws://{host}:{port}/api/websocket"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield fake
        server.shutdown()
        thread.join()


def exporter_for(fake: FakeHomeAssistant, pdl: str = PDL, **settings: Any) -> HomeAssistantExporter:
    configured = HomeAssistantSettings(enabled=True, url=fake.url, token=fake.token, **settings)
    usage_point = UsagePointSettings(id=pdl, consumption=True, consumption_detail=True)
    return HomeAssistantExporter(configured, [usage_point])


def cache_days(store: Store, *days: date, pdl: str = PDL) -> int:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(pdl, C, day, 5000) for day in days])
    return run


def test_a_new_series_is_pinned_at_its_start_and_fully_imported(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    run = cache_days(store, DAY, DAY + timedelta(days=1))

    summary = exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)

    assert store.ha_boundary(SERIES) == HaBoundary(SERIES, None, 0.0, PDL)
    (imported,) = home_assistant.imports()
    assert imported["metadata"] == {
        "has_sum": True,
        "mean_type": 0,
        "unit_class": "energy",
        "name": f"Electricity consumption {PDL}",
        "source": "releve",
        "statistic_id": SERIES,
        "unit_of_measurement": "kWh",
    }
    assert len(imported["stats"]) == 48
    assert imported["stats"][-1]["sum"] == 10.0
    assert "48 hourly rows" in summary


def test_home_assistant_before_2025_11_gets_the_old_metadata(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    home_assistant.version = "2025.10.4"
    run = cache_days(store, DAY)
    exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)
    metadata = home_assistant.imports()[0]["metadata"]
    assert metadata["has_mean"] is False
    assert "mean_type" not in metadata
    assert "unit_class" not in metadata


def test_an_existing_series_is_continued_after_its_last_hour(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    last_hour = at_paris_hour(DAY, 23)
    home_assistant.existing[SERIES] = [
        {"start": ms(last_hour - timedelta(days=40)), "sum": 1.0},
        {"start": ms(last_hour), "sum": 8481.922},
    ]
    run = cache_days(store, DAY - timedelta(days=1), DAY, DAY + timedelta(days=1))

    exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)

    assert store.ha_boundary(SERIES) == HaBoundary(SERIES, DAY, 8481.922, PDL)
    (imported,) = home_assistant.imports()
    assert imported["stats"][0]["start"] == day_start(DAY + timedelta(days=1)).isoformat()
    assert imported["stats"][-1]["sum"] == 8486.922
    periods = [m["period"] for m in home_assistant.received if "period" in m]
    assert periods == ["month", "hour"]


def test_a_pinned_boundary_is_never_rediscovered_and_is_adopted(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    store.set_ha_boundary(HaBoundary(SERIES, DAY - timedelta(days=1), 100.0), restart_sinks=None)
    home_assistant.refuse_statistics = True  # asking would fail the export
    run = cache_days(store, DAY)

    exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)

    (imported,) = home_assistant.imports()
    assert imported["stats"][-1]["sum"] == 105.0
    boundary = store.ha_boundary(SERIES)
    assert boundary is not None
    assert boundary.usage_point == PDL  # adopted by its first exporter


def test_a_series_pinned_for_another_usage_point_is_refused(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    fixed = "archive:consumption"
    store.set_ha_boundary(HaBoundary(fixed, DAY, 1.0, "09876543210987"), restart_sinks=None)
    run = cache_days(store, DAY + timedelta(days=1))
    with pytest.raises(ExportError, match="continues usage point 09876543210987"):
        exporter_for(home_assistant, statistic_id=fixed).export(store, after_run=0, up_to_run=run)
    assert home_assistant.imports() == []


def test_only_the_changed_tail_is_rewritten(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    first = cache_days(store, DAY, DAY + timedelta(days=1))
    exporter = exporter_for(home_assistant)
    exporter.export(store, after_run=0, up_to_run=first)
    second = cache_days(store, DAY + timedelta(days=2))

    exporter.export(store, after_run=first, up_to_run=second)
    assert exporter.export(store, after_run=second, up_to_run=second) == "no change"

    latest = home_assistant.imports()[-1]["stats"]
    assert len(latest) == 24
    assert latest[0]["start"] == day_start(DAY + timedelta(days=2)).isoformat()
    assert latest[-1]["sum"] == 15.0


def test_days_without_data_inside_the_owned_span_stay_flat(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    run = cache_days(store, DAY, DAY + timedelta(days=2))
    exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)
    stats = home_assistant.imports()[0]["stats"]
    assert len(stats) == 72
    assert {row["sum"] for row in stats[24:47]} == {5.0}  # the empty day overwrites stale rows
    assert stats[-1]["sum"] == 10.0


def test_a_failed_or_odd_statistics_query_imports_nothing_and_pins_nothing(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    run = cache_days(store, DAY)
    home_assistant.refuse_statistics = True
    with pytest.raises(ExportError, match="statistics_during_period refused"):
        exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)
    home_assistant.refuse_statistics = False
    home_assistant.odd_statistics_answer = True
    with pytest.raises(ExportError, match="unexpected statistics answer"):
        exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)
    assert store.ha_boundary(SERIES) is None
    assert home_assistant.imports() == []


def test_an_unreadable_last_point_is_refused(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    home_assistant.existing[SERIES] = [{"start": ms(NOW), "sum": "lots"}]
    run = cache_days(store, DAY)
    with pytest.raises(ExportError, match="cannot read the last point"):
        exporter_for(home_assistant).export(store, after_run=0, up_to_run=run)


def test_a_rejected_token_an_unknown_version_or_an_absent_server_is_an_export_error(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    run = cache_days(store, DAY)
    exporter = exporter_for(home_assistant)
    home_assistant.version = "unknown"
    with pytest.raises(ExportError, match="cannot tell the Home Assistant version"):
        exporter.export(store, after_run=0, up_to_run=run)
    home_assistant.version = "2026.9.1"
    home_assistant.token = "another"
    with pytest.raises(ExportError, match="rejected the access token"):
        exporter.export(store, after_run=0, up_to_run=run)
    offline = HomeAssistantSettings(enabled=True, url="ws://127.0.0.1:9/api/websocket", token="t")
    with pytest.raises(ExportError, match="exchange failed"):
        HomeAssistantExporter(offline, [UsagePointSettings(id=PDL)]).export(
            store, after_run=0, up_to_run=run
        )


def test_production_series_follow_the_configuration(
    home_assistant: FakeHomeAssistant, store: Store
) -> None:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(PDL, Direction.PRODUCTION, DAY, 2000)])
    settings = HomeAssistantSettings(
        enabled=True, url=home_assistant.url, token=home_assistant.token
    )

    consumption_only = HomeAssistantExporter(settings, [UsagePointSettings(id=PDL)])
    assert consumption_only.export(store, after_run=0, up_to_run=run) == "no change"

    with_production = HomeAssistantExporter(settings, [UsagePointSettings(id=PDL, production=True)])
    with_production.export(store, after_run=0, up_to_run=run)
    (imported,) = home_assistant.imports()
    assert imported["metadata"]["statistic_id"] == f"releve:{PDL}_production"
