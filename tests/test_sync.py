"""The sync pass: planning against the cache, quota isolation, gaps, journal, exporters."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from releve.domain import Contract, CustomerResource, Dataset, Direction, Identity
from releve.errors import ExportError, SyncAlreadyRunningError, WindowRejectedError
from releve.planning import SETTLE_DAYS
from releve.quota import QuotaGovernor
from releve.store import Store
from releve.sync import backlog, exclusive_pass, export, run_pass
from tests.conftest import OTHER_PDL, PDL, FrozenClock, make_settings
from tests.fakes import FakeGateway

TODAY = date(2026, 9, 12)


def gateway_for(governor: QuotaGovernor, clock: FrozenClock, **kwargs: object) -> FakeGateway:
    return FakeGateway(governor, clock, published_until=TODAY, **kwargs)  # type: ignore[arg-type]


class RecordingExporter:
    name = "recording"
    sink = "recording sink"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deliveries: list[tuple[int, int]] = []

    def export(self, store: Store, *, after_run: int, up_to_run: int) -> str:
        del store
        self.deliveries.append((after_run, up_to_run))
        if self.fail:
            raise ExportError("sink down")
        return "delivered"


def test_first_pass_fills_the_history_then_nothing_is_asked(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 30})
    gateway = gateway_for(governor, clock)

    report = run_pass(settings, gateway, store, [], clock)

    assert report.ok
    assert gateway.calls == [
        (PDL, "valid_access", None, None),
        (PDL, "daily_consumption", TODAY - timedelta(days=30), TODAY),
    ]
    assert len(store.daily(PDL, Direction.CONSUMPTION, date.min, date.max)) == 30
    gateway.calls.clear()
    assert run_pass(settings, gateway, store, [], clock).ok
    assert gateway.calls == []  # an idle pass does not even check consent


def test_load_curve_windows_go_newest_first_and_ignore_the_extra_end_day(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption": False, "consumption_detail": True, "contract": False}
        ],
        sync={"history_days": 10},
    )
    gateway = gateway_for(governor, clock)

    run_pass(settings, gateway, store, [], clock)

    metering = [
        (start, end) for _, endpoint, start, end in gateway.calls if "load_curve" in endpoint
    ]
    assert metering == [
        (TODAY - timedelta(days=7), TODAY),
        (TODAY - timedelta(days=10), TODAY - timedelta(days=7)),
    ]
    cached_days = store.days_with_data(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max)
    assert cached_days == {TODAY - timedelta(days=n) for n in range(1, 11)}


def test_an_unpublished_yesterday_is_asked_again_until_it_arrives(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 5})
    gateway = FakeGateway(governor, clock, published_until=TODAY - timedelta(days=1))

    run_pass(settings, gateway, store, [], clock)
    assert backlog(store, PDL, Dataset.DAILY_CONSUMPTION, 5, TODAY) == [TODAY - timedelta(days=1)]

    gateway.published_until = TODAY
    gateway.calls.clear()
    run_pass(settings, gateway, store, [], clock)
    assert gateway.calls == [
        (PDL, "valid_access", None, None),
        (PDL, "daily_consumption", TODAY - timedelta(days=1), TODAY),
    ]
    assert backlog(store, PDL, Dataset.DAILY_CONSUMPTION, 5, TODAY) == []


def test_an_old_hole_becomes_a_confirmed_gap_but_a_recent_one_is_retried(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    old_hole = TODAY - timedelta(days=SETTLE_DAYS + 3)
    recent_hole = TODAY - timedelta(days=2)
    settings = make_settings(database, sync={"history_days": 20})
    gateway = gateway_for(governor, clock, holes={old_hole, recent_hole})

    run_pass(settings, gateway, store, [], clock)

    assert store.confirmed_gaps(PDL, Dataset.DAILY_CONSUMPTION, date.min, date.max) == {old_hole}
    assert backlog(store, PDL, Dataset.DAILY_CONSUMPTION, 20, TODAY) == [recent_hole]


def test_a_spent_budget_stops_one_usage_point_and_never_the_others(
    database: Path, store: Store, clock: FrozenClock
) -> None:
    governor = QuotaGovernor(store, daily_budget=2, clock=clock)
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption_detail": True, "contract": False},
            {"id": OTHER_PDL, "contract": False},
        ],
        sync={"history_days": 60},
    )
    gateway = gateway_for(governor, clock)

    report = run_pass(settings, gateway, store, [], clock)

    first, second = report.outcomes
    assert not first.ok
    assert "daily budget spent" in first.detail
    assert second.ok
    assert len(store.daily(OTHER_PDL, Direction.CONSUMPTION, date.min, date.max)) == 60
    assert governor.usage(PDL).used == 2


def test_a_throttle_or_a_refused_token_stops_only_that_usage_point(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption_detail": True, "max_power": True, "contract": False},
            {"id": OTHER_PDL, "contract": False},
        ],
        sync={"history_days": 3},
    )
    gateway = gateway_for(governor, clock, throttled_curves=True, refused_usage_points={OTHER_PDL})

    first, second = run_pass(settings, gateway, store, [], clock).outcomes

    assert not first.ok
    assert "throttled" in first.detail
    assert "daily_consumption +3" in first.detail
    assert "max_power +3" in first.detail
    assert governor.usage(PDL).blocked_until is not None
    assert not second.ok
    assert "refused the token" in second.detail


def test_an_unreachable_gateway_costs_one_call_per_usage_point(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption_detail": True, "max_power": True, "contract": False}
        ],
    )
    gateway = gateway_for(governor, clock, unreachable=True)

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert not outcome.ok
    assert "no answer" in outcome.detail
    assert governor.usage(PDL).used == 1


def test_invalid_consent_stops_metering_without_further_calls(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 5})
    gateway = gateway_for(governor, clock, consent_valid=False)

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert not outcome.ok
    assert "consent refused" in outcome.detail
    assert gateway.calls == [(PDL, "valid_access", None, None)]
    assert store.daily(PDL, Direction.CONSUMPTION, date.min, date.max) == []


def test_contract_is_refreshed_when_due(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[{"id": PDL, "contract": True}],
        sync={"history_days": 1, "customer_refresh_days": 7},
    )
    gateway = gateway_for(governor, clock)

    run_pass(settings, gateway, store, [], clock)
    assert any(endpoint == "contracts" for _, endpoint, _, _ in gateway.calls)
    assert store.contract(PDL) is not None
    gateway.calls.clear()
    run_pass(settings, gateway, store, [], clock)
    assert all(endpoint != "contracts" for _, endpoint, _, _ in gateway.calls)
    clock.advance(timedelta(days=7))
    gateway.calls.clear()
    run_pass(settings, gateway, store, [], clock)
    assert any(endpoint == "contracts" for _, endpoint, _, _ in gateway.calls)


def test_a_failing_customer_resource_does_not_block_the_others(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[{"id": PDL, "contract": True, "addresses": True}],
        sync={"history_days": 1},
    )
    gateway = gateway_for(governor, clock)

    def refused(usage_point: str) -> Contract:
        del usage_point
        raise WindowRejectedError("contracts: the gateway refused the window (HTTP 404)")

    gateway.contract = refused  # type: ignore[method-assign]
    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert not outcome.ok
    assert store.address(PDL) is not None
    assert store.daily(PDL, Direction.CONSUMPTION, date.min, date.max) != []


def test_a_failing_customer_resource_is_asked_again_a_day_later_not_every_pass(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[{"id": PDL, "contract": True, "identity": True}],
        sync={"history_days": 1, "interval_hours": 4},
    )
    gateway = gateway_for(governor, clock)

    def refused(usage_point: str) -> Identity:
        gateway.calls.append((usage_point, "identity", None, None))
        raise WindowRejectedError("identity: the gateway refused the window (HTTP 404)")

    gateway.identity = refused  # type: ignore[method-assign]
    (first,) = run_pass(settings, gateway, store, [], clock).outcomes
    assert not first.ok
    assert "refused" in store.customer_failures(PDL)[CustomerResource.IDENTITY].detail

    for _ in range(5):  # the rest of the day: nothing is due, nothing is asked
        clock.advance(timedelta(hours=4))
        gateway.calls.clear()
        (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes
        assert outcome.ok
        assert all(endpoint != "identity" for _, endpoint, _, _ in gateway.calls)

    clock.advance(timedelta(hours=4))
    gateway.calls.clear()
    del gateway.identity  # the gateway gives the identity again
    (retried,) = run_pass(settings, gateway, store, [], clock).outcomes
    assert retried.ok
    assert (PDL, "identity", None, None) in gateway.calls
    assert store.customer_failures(PDL) == {}
    assert store.identity(PDL) is not None


def test_a_quota_refusal_is_not_recorded_against_a_customer_resource(
    database: Path, store: Store, clock: FrozenClock
) -> None:
    governor = QuotaGovernor(store, daily_budget=1, clock=clock)
    settings = make_settings(database, usage_points=[{"id": PDL, "contract": True}])

    (outcome,) = run_pass(settings, gateway_for(governor, clock), store, [], clock).outcomes

    assert not outcome.ok
    assert "daily budget spent" in outcome.detail
    assert store.customer_failures(PDL) == {}


def test_tempo_season_and_prices_are_fetched_once_a_day(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 1, "rte_signals": True})
    gateway = gateway_for(governor, clock)
    extras = {"edf_tempo_days", "edf_tempo_price"}

    run_pass(settings, gateway, store, [], clock)
    assert extras <= {endpoint for _, endpoint, _, _ in gateway.calls}
    gateway.calls.clear()
    run_pass(settings, gateway, store, [], clock)
    assert not extras & {endpoint for _, endpoint, _, _ in gateway.calls}
    assert store.tempo_season() is not None


def test_rte_signals_are_fetched_only_when_enabled(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    gateway = gateway_for(governor, clock)
    run_pass(make_settings(database, sync={"history_days": 1}), gateway, store, [], clock)
    assert [call for call in gateway.calls if call[0] == "rte"] == []

    run_pass(
        make_settings(database, sync={"history_days": 1, "rte_signals": True}),
        gateway,
        store,
        [],
        clock,
    )
    assert store.tempo(TODAY, TODAY + timedelta(days=1))[0].color == "BLUE"
    ecowatt = store.ecowatt(date.min, date.max)
    assert [day.day for day in ecowatt] == [TODAY + timedelta(days=n) for n in range(-1, 4)]


def test_every_outcome_is_journaled(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 2, "rte_signals": True})
    report = run_pass(settings, gateway_for(governor, clock), store, [RecordingExporter()], clock)

    events = store.recent_events(10)
    assert {event.subject for event in events} == {"rte", PDL, "export:recording"}
    assert all(event.run_id == report.run_id for event in events)
    assert set(store.last_success()) == {"rte", PDL, "export:recording"}


def test_a_failed_export_is_delivered_again_with_the_next_changes(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database, sync={"history_days": 2})
    gateway = gateway_for(governor, clock)
    exporter = RecordingExporter(fail=True)

    first = run_pass(settings, gateway, store, [exporter], clock)
    assert not first.ok
    exporter.fail = False
    second = run_pass(settings, gateway, store, [exporter], clock)

    assert exporter.deliveries == [(0, first.run_id), (0, second.run_id)]
    assert store.export_cursor("recording sink") == second.run_id
    assert export(exporter, store, second.run_id + 1).ok
    assert exporter.deliveries[-1] == (second.run_id, second.run_id + 1)


def test_one_pass_at_a_time_per_database(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(database)
    with exclusive_pass(database), pytest.raises(SyncAlreadyRunningError):
        run_pass(settings, gateway_for(governor, clock), store, [], clock)
    with exclusive_pass(database):
        pass  # the lock was released


def test_a_refused_window_of_settled_days_becomes_gaps_and_others_go_on(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption": False, "consumption_detail": True, "contract": False}
        ],
        sync={"history_days": 30},
    )
    old_window_start = TODAY - timedelta(days=21)
    gateway = gateway_for(governor, clock, refused_windows={old_window_start})

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert outcome.ok
    gaps = store.confirmed_gaps(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max)
    assert gaps == {old_window_start + timedelta(days=n) for n in range(7)}
    assert backlog(store, PDL, Dataset.CURVE_CONSUMPTION, 30, TODAY) == []


def test_a_refused_recent_window_is_reported_after_the_others(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption": False, "consumption_detail": True, "contract": False}
        ],
        sync={"history_days": 14},
    )
    gateway = gateway_for(governor, clock, refused_windows={TODAY - timedelta(days=7)})

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert not outcome.ok
    assert "refused the window" in outcome.detail
    remaining = backlog(store, PDL, Dataset.CURVE_CONSUMPTION, 14, TODAY)
    assert remaining == [TODAY - timedelta(days=n) for n in range(1, 8)]
