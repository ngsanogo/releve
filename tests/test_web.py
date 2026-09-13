"""The web interface: pages, JSON API, metrics, health and authentication."""

from __future__ import annotations

import base64
from datetime import date, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from releve.clock import at_paris_hour
from releve.domain import (
    Contact,
    CustomerResource,
    DailyEnergy,
    Direction,
    EcowattDay,
    EcowattHour,
    PowerPeak,
    TempoDay,
)
from releve.quota import QuotaGovernor
from releve.scheduler import Scheduler
from releve.store import Store
from releve.web import create_app
from tests.conftest import NOW, PDL, FrozenClock, make_settings

TODAY = date(2026, 9, 12)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture
def client(database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock) -> TestClient:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(PDL, Direction.CONSUMPTION, YESTERDAY, 6028)])
    store.upsert_peaks(run, [PowerPeak(PDL, YESTERDAY, 6100, at_paris_hour(YESTERDAY, 19))])
    store.upsert_tempo([TempoDay(TODAY, "WHITE")])
    store.upsert_ecowatt([EcowattDay(TODAY, 2, "tendu")])
    store.record_event(run, NOW, PDL, True, "daily_consumption +1")
    store.advance_export_cursor("mqtt 127.0.0.1:1883", run, NOW)
    store.block(PDL, NOW + timedelta(hours=2), "throttled upstream on daily_consumption")
    settings = make_settings(
        database,
        usage_points=[{"id": PDL, "name": "Home", "max_power": True}],
        sync={"rte_signals": True},
    )
    return TestClient(create_app(settings, store, governor, clock=clock))


def test_the_dashboard_tells_the_truth(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    for fragment in (
        "Home",
        PDL,
        "WHITE",
        "tendu",
        "blocked until 2026-09-12 14:00",
        "daily_consumption +1",
        "mqtt 127.0.0.1:1883",
        "364",
    ):
        assert fragment in page.text


def test_the_usage_point_page_and_unknown_usage_points(client: TestClient) -> None:
    page = client.get(f"/usage-points/{PDL}")
    assert page.status_code == 200
    assert "6.028" in page.text
    assert "6100" in page.text
    assert client.get("/usage-points/99999999999999").status_code == 404


def test_the_json_api(client: TestClient) -> None:
    daily = client.get(
        f"/api/v1/usage-points/{PDL}/daily", params={"start": "2026-09-01", "end": "2026-09-12"}
    )
    assert daily.json() == [{"day": "2026-09-11", "wh": 6028, "direction": "consumption"}]
    production = client.get(
        f"/api/v1/usage-points/{PDL}/daily",
        params={"start": "2026-09-01", "end": "2026-09-12", "direction": "production"},
    )
    assert production.json() == []
    rte = {"start": "2026-09-12", "end": "2026-09-13"}
    assert client.get("/api/v1/rte/tempo", params=rte).json() == [
        {"day": "2026-09-12", "color": "WHITE"}
    ]
    assert client.get("/api/v1/rte/ecowatt", params=rte).json() == [
        {"day": "2026-09-12", "value": 2, "message": "tendu"}
    ]


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"end": "2026-09-12"}, "missing query parameter 'start'"),
        ({"start": "2026-13-01", "end": "2026-09-12"}, "must be dates"),
        ({"start": "2026-09-12", "end": "2026-09-12"}, "start must be before end"),
        ({"start": "2020-01-01", "end": "2026-09-12"}, "limited to 1096 days"),
        (
            {"start": "2026-09-01", "end": "2026-09-12", "direction": "sideways"},
            "direction must be",
        ),
    ],
)
def test_bad_api_requests_get_a_json_400(
    client: TestClient, params: dict[str, str], error: str
) -> None:
    response = client.get(f"/api/v1/usage-points/{PDL}/daily", params=params)
    assert response.status_code == 400
    assert error in response.json()["error"]


def test_metrics(client: TestClient) -> None:
    body = client.get("/metrics").text
    assert 'releve_gateway_daily_budget{bucket="rte"} 45' in body
    assert f'releve_gateway_blocked_until_timestamp_seconds{{bucket="{PDL}"}} 1789214400' in body
    assert f'releve_missing_days{{usage_point="{PDL}",dataset="daily_consumption"}} 364' in body
    assert f'releve_last_success_timestamp_seconds{{subject="{PDL}"}} 1789207200' in body
    assert "e+" not in body  # timestamps are never rounded into scientific notation


def test_health_reflects_the_scheduler(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database)
    scheduler = Scheduler(lambda: None, timedelta(hours=4), clock=clock)
    client = TestClient(create_app(settings, store, governor, scheduler, clock))

    assert client.get("/healthz").status_code == 503  # not started
    scheduler.start()
    try:
        assert client.get("/healthz").json() == {
            "ok": True,
            "version": client.get("/healthz").json()["version"],
        }
        clock.advance(timedelta(hours=9))
        assert client.get("/healthz").json() == {"ok": False, "reason": "scheduler stalled"}
    finally:
        scheduler.stop()


def test_the_auth_token_guards_everything_but_health(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, web={"auth_token": "s3cret"})
    client = TestClient(create_app(settings, store, governor, clock=clock))

    denied = client.get("/metrics")
    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    basic = base64.b64encode(b"anyone:s3cret").decode()
    assert client.get("/", headers={"Authorization": f"Basic {basic}"}).status_code == 200
    for header in (
        "Bearer wrong",
        "Basic !!!",
        f"Basic {base64.b64encode(b'no-colon').decode()}",
        "Digest x",
    ):
        assert client.get("/", headers={"Authorization": header}).status_code == 401
    assert (
        client.get("/static/style.css", headers={"Authorization": "Bearer s3cret"}).status_code
        == 200
    )


def test_hourly_ecowatt_is_served_per_paris_day(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    store.upsert_ecowatt_hours(
        [
            EcowattHour(at_paris_hour(day, hour), day.day)
            for day in (YESTERDAY, TODAY)
            for hour in (0, 23)
        ]
    )
    api = TestClient(create_app(make_settings(database), store, governor, clock=clock))

    tomorrow = TODAY + timedelta(days=1)
    answer = api.get(
        "/api/v1/rte/ecowatt/hours", params={"start": str(TODAY), "end": str(tomorrow)}
    )

    assert answer.json() == [
        {"at": "2026-09-11T22:00:00+00:00", "value": 12},
        {"at": "2026-09-12T21:00:00+00:00", "value": 12},
    ]


def test_the_usage_point_page_shows_customer_data_that_could_not_be_fetched(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    detail = "identity: the gateway refused the window (HTTP 404)"
    store.record_customer_failure(PDL, CustomerResource.IDENTITY, at=NOW, detail=detail)
    for enabled in (True, False):
        settings = make_settings(database, usage_points=[{"id": PDL, "identity": enabled}])
        page = TestClient(create_app(settings, store, governor, clock=clock)).get(
            f"/usage-points/{PDL}"
        )
        assert (detail in page.text) is enabled


def test_customer_data_is_served_only_while_enabled(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    store.upsert_contact(Contact(PDL, phone="0102030405", email="a@b.c"), at=NOW)
    for enabled, status in ((True, 200), (False, 404)):
        settings = make_settings(database, usage_points=[{"id": PDL, "contact": enabled}])
        api = TestClient(create_app(settings, store, governor, clock=clock))
        assert api.get(f"/api/v1/usage-points/{PDL}/contact").status_code == status
