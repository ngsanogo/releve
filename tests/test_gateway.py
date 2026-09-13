"""The gateway client against a mocked HTTP layer. Payload shapes were observed live."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest

from releve.clock import next_utc_midnight
from releve.config import GatewaySettings
from releve.curve import hourly_energy
from releve.domain import DailyEnergy, Direction, TempoColor
from releve.errors import (
    AuthError,
    GatewayError,
    GatewayUnreachableError,
    QuotaExhaustedError,
    ThrottledError,
    WindowRejectedError,
)
from releve.gateway import (
    GatewayClient,
    parse_consent,
    parse_contact,
    parse_contract,
    parse_ecowatt,
    parse_identity,
    parse_load_curve,
    parse_next_access,
    parse_tempo,
)
from releve.quota import QuotaGovernor
from releve.store import Store
from tests.conftest import NOW, PDL, FrozenClock
from tests.fakes import HttpDouble

BASE = "https://gateway.test"
C = Direction.CONSUMPTION
START, END = date(2026, 9, 8), date(2026, 9, 11)
DAILY_PATH = f"/daily_consumption/{PDL}/start/2026-09-08/end/2026-09-11"


def readings(*pairs: tuple[str, object]) -> dict[str, Any]:
    return {
        "meter_reading": {
            "usage_point_id": PDL,
            "interval_reading": [{"value": value, "date": when} for when, value in pairs],
        }
    }


@pytest.fixture
def mock() -> HttpDouble:
    return HttpDouble()


def gateway_client(
    mock: HttpDouble, governor: QuotaGovernor, clock: FrozenClock, *, prefer_cache: bool = False
) -> GatewayClient:
    settings = GatewaySettings(base_url=BASE, token="secret", prefer_cache=prefer_cache)
    return GatewayClient(settings, governor, clock=clock, transport=mock.transport)


@pytest.fixture
def client(
    mock: HttpDouble, governor: QuotaGovernor, clock: FrozenClock
) -> Iterator[GatewayClient]:
    with gateway_client(mock, governor, clock) as gateway:
        yield gateway


def test_daily_energy_is_parsed_and_the_call_is_counted(
    mock: HttpDouble, client: GatewayClient, governor: QuotaGovernor
) -> None:
    mock.on(
        DAILY_PATH,
        json=readings(("2026-09-08", "5715"), ("2026-09-09", 6517), ("2026-09-10", "6028.0")),
    )

    energy = client.daily(PDL, C, START, END)

    assert [(e.day, e.wh) for e in energy] == [
        (date(2026, 9, 8), 5715),
        (date(2026, 9, 9), 6517),
        (date(2026, 9, 10), 6028),
    ]
    assert mock.requests[-1].headers["Authorization"] == "secret"
    assert governor.usage(PDL).used == 1


def test_cache_endpoints_are_used_when_preferred(
    mock: HttpDouble, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    mock.on(f"{DAILY_PATH}/cache", json=readings())
    with gateway_client(mock, governor, clock, prefer_cache=True) as gateway:
        assert gateway.daily(PDL, C, START, END) == []
    assert mock.calls(f"{DAILY_PATH}/cache") == 1


def test_the_october_fall_back_keeps_both_repeated_hours(
    mock: HttpDouble, client: GatewayClient
) -> None:
    # 2026-10-25: at 03:00 CEST, Paris clocks go back to 02:00 CET, so the
    # interval ends 02:00 and 02:30 are published twice.
    day = date(2026, 10, 25)
    half_hour = timedelta(minutes=30)
    wall = datetime(2026, 10, 25)  # noqa: DTZ001 — Enedis publishes naive wall-clock times
    before = [wall + timedelta(minutes=30) + half_hour * k for k in range(5)]  # 00:30..02:30
    repeated = [wall + timedelta(hours=2), wall + timedelta(hours=2, minutes=30)]
    after = [wall + timedelta(hours=3) + half_hour * k for k in range(43)]  # 03:00..24:00
    wall_times = [t.strftime("%Y-%m-%d %H:%M:%S") for t in before + repeated + after]
    path = f"/consumption_load_curve/{PDL}/start/2026-10-25/end/2026-10-26"
    mock.on(path, json=readings(*[(t, 400) for t in wall_times]))

    points = client.load_curve(PDL, C, day, day + timedelta(days=1))

    assert len(points) == 50
    assert len({p.end for p in points}) == 50
    hours = hourly_energy(day, points)
    assert hours is not None
    assert len(hours) == 25


def test_ambiguous_readings_on_one_instant_keep_the_last(caplog: pytest.LogCaptureFixture) -> None:
    payload = readings(
        ("2026-09-10 10:00:00", 1), ("2026-09-10 10:00:00", 2), ("2026-09-10 10:00:00", 3)
    )
    points = parse_load_curve(payload, PDL, C, "/curve")
    # outside the fall-back hour a repeated wall-clock time is one instant
    assert [p.watts for p in points] == [3]
    assert "keeping the last" in caplog.text


def test_a_local_refusal_sends_nothing(mock: HttpDouble, store: Store, clock: FrozenClock) -> None:
    mock.on(DAILY_PATH, json=readings())
    governor = QuotaGovernor(store, daily_budget=1, clock=clock)
    with gateway_client(mock, governor, clock) as gateway:
        gateway.daily(PDL, C, START, END)
        with pytest.raises(QuotaExhaustedError, match="daily budget spent"):
            gateway.daily(PDL, C, START, END)
    assert mock.calls(DAILY_PATH) == 1


def test_an_enedis_throttle_blocks_until_next_access_time(
    mock: HttpDouble, client: GatewayClient, governor: QuotaGovernor
) -> None:
    body = {
        "detail": '{"code":"900804","message":"Message throttled out",'
        '"nextAccessTime":"2026-Sep-12 20:00:00+0000 UTC"}'
    }
    mock.on(DAILY_PATH, status=429, json=body)

    with pytest.raises(ThrottledError) as caught:
        client.daily(PDL, C, START, END)

    until = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
    assert caught.value.retry_at == until
    assert governor.usage(PDL).blocked_until == until
    with pytest.raises(QuotaExhaustedError, match="throttled upstream"):
        client.daily(PDL, C, START, END)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"Retry-After": "120"}, NOW + timedelta(seconds=120)), ({}, NOW + timedelta(hours=1))],
)
def test_a_throttle_without_next_access_time_falls_back(
    mock: HttpDouble,
    client: GatewayClient,
    headers: dict[str, str],
    expected: datetime,
) -> None:
    mock.on(DAILY_PATH, status=429, headers=headers, text="slow down")
    with pytest.raises(ThrottledError) as caught:
        client.daily(PDL, C, START, END)
    assert caught.value.retry_at == expected


def test_the_gateway_daily_quota_blocks_until_utc_midnight(
    mock: HttpDouble, client: GatewayClient, governor: QuotaGovernor
) -> None:
    mock.on(DAILY_PATH, status=409, json={"detail": "quota journalier dépassé"})
    with pytest.raises(ThrottledError):
        client.daily(PDL, C, START, END)
    assert governor.usage(PDL).blocked_until == next_utc_midnight(NOW)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_an_auth_error(
    mock: HttpDouble, client: GatewayClient, status: int
) -> None:
    mock.on(DAILY_PATH, status=status, json={"detail": "bad token"})
    with pytest.raises(AuthError, match=f"HTTP {status}"):
        client.daily(PDL, C, START, END)


def test_transport_failures_and_odd_answers_are_typed_and_still_counted(
    mock: HttpDouble, client: GatewayClient, governor: QuotaGovernor
) -> None:
    mock.on(DAILY_PATH, error=httpx2.ConnectError("boom"))
    with pytest.raises(GatewayUnreachableError, match="no answer"):
        client.daily(PDL, C, START, END)
    mock.on(DAILY_PATH, text="<html>maintenance</html>")
    with pytest.raises(GatewayError, match="without a JSON body"):
        client.daily(PDL, C, START, END)
    mock.on(DAILY_PATH, status=404, text="no data")
    with pytest.raises(WindowRejectedError, match="refused the window"):
        client.daily(PDL, C, START, END)
    mock.on(DAILY_PATH, status=502, text="bad gateway")
    with pytest.raises(GatewayError, match=r"unexpected HTTP 502$"):
        client.daily(PDL, C, START, END)
    assert governor.usage(PDL).used == 4


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"nope": 1}, "no meter_reading"),
        ({"meter_reading": {"interval_reading": {}}}, "not a list"),
        ({"meter_reading": {"interval_reading": ["x"]}}, "not an object"),
        (readings(("2026-09-08", None)), "bad value"),
        (readings(("2026-09-08", "12.5")), "not a whole number"),
        (readings(("2026-09-08", "abc")), "bad value"),
        (readings(("2026-99-08", "1")), "bad date"),
        ({"meter_reading": {"interval_reading": [{"value": "1"}]}}, "without a text date"),
    ],
)
def test_malformed_payloads_are_gateway_errors(
    mock: HttpDouble, client: GatewayClient, payload: dict[str, Any], message: str
) -> None:
    mock.on(DAILY_PATH, json=payload)
    with pytest.raises(GatewayError, match=message):
        client.daily(PDL, C, START, END)


def test_max_power_and_rte_signals(mock: HttpDouble, client: GatewayClient) -> None:
    mock.on(
        f"/daily_consumption_max_power/{PDL}/start/2026-09-08/end/2026-09-11",
        json=readings(("2026-09-08 19:12:00", "6000")),
    )
    mock.on("/rte/tempo/2026-09-08/2026-09-11", json={"2026-09-09": "white", "2026-09-08": "BLUE"})
    # The gateway keys Ecowatt days one day early, and filters on that key.
    mock.on(
        "/rte/ecowatt/2026-09-07/2026-09-10",
        json={"2026-09-08": {"value": 2, "message": "tendu"}, "2026-09-09": 1},
    )

    (peak,) = client.max_power(PDL, START, END)
    assert (peak.day, peak.va, peak.at) == (
        date(2026, 9, 8),
        6000,
        datetime(2026, 9, 8, 17, 12, tzinfo=UTC),
    )
    assert [(t.day.day, t.color) for t in client.tempo(START, END)] == [(8, "BLUE"), (9, "WHITE")]
    forecast = client.ecowatt(START, END)
    assert [(e.day.day, e.level, e.message) for e in forecast.days] == [
        (9, 2, "tendu"),
        (10, 1, ""),
    ]
    assert forecast.hours == ()


@pytest.mark.parametrize("payload", [[], {"2026-09-08": 3}, {"not-a-date": "BLUE"}])
def test_bad_tempo_payloads(payload: object) -> None:
    with pytest.raises(GatewayError):
        parse_tempo(payload, "/rte/tempo")


@pytest.mark.parametrize("payload", [[], {"2026-09-08": "high"}, {"2026-09-08": True}])
def test_bad_ecowatt_payloads(payload: object) -> None:
    with pytest.raises(GatewayError):
        parse_ecowatt(payload, "/rte/ecowatt")


def test_next_access_time_parsing_does_not_depend_on_the_locale() -> None:
    assert parse_next_access('"nextAccessTime":"2026-Dec-01 07:05:09+0000 UTC"') == datetime(
        2026, 12, 1, 7, 5, 9, tzinfo=UTC
    )
    assert parse_next_access('"nextAccessTime":"2026-Foo-01 07:05:09+0000 UTC"') is None
    assert parse_next_access("no timestamp") is None


def test_daily_answers_are_domain_objects(mock: HttpDouble, client: GatewayClient) -> None:
    mock.on(DAILY_PATH, json=readings(("2026-09-08", "1")))
    assert client.daily(PDL, C, START, END) == [DailyEnergy(PDL, C, date(2026, 9, 8), 1)]


def test_consent_contract_identity_contact_address_and_tempo_extras(
    mock: HttpDouble, client: GatewayClient
) -> None:
    mock.on(
        f"/valid_access/{PDL}",
        json={
            "valid": True,
            "information": "",
            "consent_expiration_date": "2029-09-12T22:04:31",
            "call_number": 6,
            "quota_reached": False,
            "quota_limit": 50,
            "quota_reset_at": "2026-09-13T23:59:59.999999",
            "last_call": "2026-06-21T11:57:27.051689",
            "ban": False,
        },
    )
    mock.on(
        f"/contracts/{PDL}",
        json={
            "customer": {
                "customer_id": "cust",
                "usage_points": [
                    {
                        "usage_point": {
                            "usage_point_id": PDL,
                            "usage_point_status": "COM",
                            "meter_type": "TCB",
                        },
                        "contracts": {
                            "segment": "C5",
                            "subscribed_power": "9 kVA",
                            "last_activation_date": "2019-07-16+02:00",
                            "distribution_tariff": "BTINFCU4",
                            "offpeak_hours": "HC (0H50-6H50;14H20-16H20)",
                            "contract_status": "SERVC",
                            "last_distribution_tariff_change_date": "2024-03-17+01:00",
                        },
                    }
                ],
            }
        },
    )
    mock.on(
        f"/identity/{PDL}",
        json={
            "customer_id": "cust",
            "identity": {"natural_person": {"title": "M", "firstname": "Ada", "lastname": "L"}},
        },
    )
    mock.on(
        f"/contact/{PDL}",
        json={"customer_id": "cust", "contact_data": {"phone": "0102030405", "email": "a@b.c"}},
    )
    mock.on(
        f"/addresses/{PDL}",
        json={
            "customer": {
                "customer_id": "cust",
                "usage_points": [
                    {
                        "usage_point": {
                            "usage_point_id": PDL,
                            "usage_point_status": "COM",
                            "meter_type": "TCB",
                            "usage_point_addresses": {
                                "street": "1 rue",
                                "locality": None,
                                "postal_code": "75002",
                                "insee_code": "75102",
                                "city": "Paris",
                                "country": "France",
                                "geo_points": {},
                            },
                        }
                    }
                ],
            }
        },
    )
    mock.on(
        "/edf/tempo/price",
        json={
            "red_hc": "0.1615",
            "red_hp": "0.7295",
            "blue_hc": "0.1356",
            "blue_hp": "0.1654",
            "white_hc": "0.1536",
            "white_hp": "0.1921",
        },
    )
    mock.on("/edf/tempo/days", json={"red": 22, "blue": 287, "white": 43})

    consent = client.valid_access(PDL)
    assert consent.valid
    assert consent.call_number == 6
    contract = client.contract(PDL)
    assert contract.offpeak_hours is not None
    assert "0H50" in contract.offpeak_hours
    assert client.identity(PDL).firstname == "Ada"
    assert client.contact(PDL).email == "a@b.c"
    assert client.addresses(PDL).city == "Paris"
    prices = client.tempo_prices()
    assert len(prices) == 6
    season = client.tempo_season()
    assert season.days_left[TempoColor.BLUE] == 287


def test_ecowatt_days_are_dated_from_hourly_detail() -> None:
    payload = {
        "2026-09-12": {
            "value": 1,
            "message": "ok",
            "detail": {"2026-09-13 00:00:00": 1, "2026-09-13 01:00:00": 0},
        }
    }
    forecast = parse_ecowatt(payload, "/rte/ecowatt")
    assert forecast.days[0].day == date(2026, 9, 13)
    assert [hour.at for hour in forecast.hours] == [
        datetime(2026, 9, 12, 22, tzinfo=UTC),
        datetime(2026, 9, 12, 23, tzinfo=UTC),
    ]


def test_ecowatt_hours_across_the_clock_changes_never_share_an_instant() -> None:
    def day_of_hours(day: str) -> dict[str, int]:
        return {f"{day} {hour:02d}:00:00": hour for hour in range(24)}

    payload = {
        "2027-03-27": {"value": 1, "message": "", "detail": day_of_hours("2027-03-28")},
        "2027-10-30": {"value": 1, "message": "", "detail": day_of_hours("2027-10-31")},
    }
    forecast = parse_ecowatt(payload, "/rte/ecowatt")

    instants = [hour.at for hour in forecast.hours]
    assert len(instants) == len(set(instants)) == 23 + 24  # no 02:00 on 28 March
    assert 2 not in {hour.level for hour in forecast.hours if hour.at.month == 3}
    assert [day.day for day in forecast.days] == [date(2027, 3, 28), date(2027, 10, 31)]


def test_delete_cache_counts_and_hits_the_right_path(
    mock: HttpDouble, client: GatewayClient, governor: QuotaGovernor
) -> None:
    from releve.domain import CacheResource

    mock.on(f"/cache/{PDL}", method="DELETE", json={"status": "ok"})
    client.delete_cache(PDL, CacheResource.ALL)
    assert mock.calls(f"/cache/{PDL}", method="DELETE") == 1
    assert governor.usage(PDL).used == 1


def test_customer_answers_may_wrap_the_holder_and_never_mix_meters() -> None:
    wrapped = {
        "customer": {
            "customer_id": "cust",
            "identity": {"natural_person": {"title": "M", "firstname": "Ada", "lastname": "L"}},
            "contact_data": {"phone": "0102030405", "email": "a@b.c"},
        }
    }
    assert parse_identity(wrapped, PDL, "/identity").firstname == "Ada"
    assert parse_contact(wrapped, PDL, "/contact").email == "a@b.c"

    other_meter = {
        "customer": {
            "usage_points": [{"usage_point": {"usage_point_id": "99999999999999"}, "contracts": {}}]
        }
    }
    with pytest.raises(GatewayError, match="not in answer"):
        parse_contract(other_meter, PDL, "/contracts")


@pytest.mark.parametrize(("raw", "banned"), [(False, False), ("false", False), ("true", True)])
def test_consent_flags_sent_as_text_are_read_as_booleans(raw: object, banned: bool) -> None:
    assert parse_consent({"valid": "true", "ban": raw}, PDL, "/valid_access").banned is banned


@pytest.mark.parametrize(
    "payload", [{}, {"valid": None}, {"valid": "maybe"}, {"valid": True, "ban": "perhaps"}]
)
def test_consent_without_a_readable_decision_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(GatewayError):
        parse_consent(payload, PDL, "/valid_access")


def test_unreadable_informational_consent_fields_are_left_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    consent = parse_consent(
        {
            "valid": True,
            "ban": False,
            "call_number": "6",
            "quota_limit": 12.5,
            "quota_reached": "soon",
            "consent_expiration_date": "next year",
        },
        PDL,
        "/valid_access",
    )

    assert consent.granted
    assert consent.call_number == 6
    assert (consent.quota_limit, consent.quota_reached, consent.expires_at) == (None, False, None)
    assert caplog.text.count("ignoring an unreadable informational field") == 3


def test_delete_cache_accepts_no_content(mock: HttpDouble, client: GatewayClient) -> None:
    from releve.domain import CacheResource

    mock.on(f"/identity/{PDL}/cache", method="DELETE", status=204)
    client.delete_cache(PDL, CacheResource.IDENTITY)
    assert mock.calls(f"/identity/{PDL}/cache", method="DELETE") == 1
