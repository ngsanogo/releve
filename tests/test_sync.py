"""The sync pass: planning against the cache, quota isolation, gaps, journal, exporters."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from releve.config import Settings
from releve.domain import Contract, CustomerResource, Dataset, Direction, Identity
from releve.errors import ExportError, NotFoundError, SyncAlreadyRunningError
from releve.planning import SETTLE_DAYS
from releve.quota import QuotaGovernor
from releve.store import Store
from releve.sync import backlog, exclusive_pass, export, partial_days, run_pass
from tests.conftest import NOW, OTHER_PDL, PDL, FrozenClock, make_settings
from tests.fakes import FakeGateway, curve_of

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


def curve_settings(database: Path, **point: object) -> Settings:
    usage_point = {"id": PDL, "consumption": False, "consumption_detail": True, "contract": False}
    return make_settings(
        database, usage_points=[{**usage_point, **point}], sync={"history_days": 10}
    )


def next_day(clock: FrozenClock, gateway: FakeGateway) -> date:
    """Wind the clock a day on; the gateway publishes the day that just ended."""
    clock.advance(timedelta(days=1))
    gateway.published_until += timedelta(days=1)
    gateway.calls.clear()
    return gateway.published_until


def test_a_partial_curve_day_never_costs_a_call_of_its_own(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    recent = TODAY - timedelta(days=2)
    settings = curve_settings(database)
    gateway = gateway_for(governor, clock, partial_curves={recent})

    run_pass(settings, gateway, store, [], clock)
    gateway.partial_curves.clear()  # Enedis completes the day
    gateway.calls.clear()
    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert outcome.detail == "nothing to fetch"
    assert gateway.calls == []
    assert backlog(store, PDL, Dataset.CURVE_CONSUMPTION, 10, TODAY) == []
    assert partial_days(store, PDL, Dataset.CURVE_CONSUMPTION, 10, TODAY) == {recent}


def test_a_recent_partial_curve_rides_along_with_yesterday_but_an_old_one_is_kept(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    recent = TODAY - timedelta(days=2)
    old = TODAY - timedelta(days=SETTLE_DAYS + 1)
    settings = curve_settings(database)
    gateway = gateway_for(governor, clock, partial_curves={recent, old})
    first = run_pass(settings, gateway, store, [], clock)

    gateway.partial_curves.clear()
    today = next_day(clock, gateway)
    second = run_pass(settings, gateway, store, [], clock)

    yesterday = today - timedelta(days=1)
    assert gateway.calls == [
        (PDL, "valid_access", None, None),
        (PDL, "consumption_load_curve", recent, today),  # stretched from [yesterday, today)
    ]
    assert gateway.uncached_calls == [(PDL, "consumption_load_curve", recent, today)]
    assert len(store.curve(PDL, Direction.CONSUMPTION, yesterday, today)) == 48
    assert len(store.curve(PDL, Direction.CONSUMPTION, recent, recent + timedelta(days=1))) == 48
    assert len(store.curve(PDL, Direction.CONSUMPTION, old, old + timedelta(days=1))) == 24
    assert partial_days(store, PDL, Dataset.CURVE_CONSUMPTION, 10, today) == set()
    # Completing the curve marks that day as changed so exporters rewrite it hour by hour.
    assert (
        store.earliest_changed_day(
            PDL, Direction.CONSUMPTION, after_run=first.run_id, up_to_run=second.run_id
        )
        == recent
    )


def test_a_partial_curve_day_is_asked_past_the_gateway_cache(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    recent = TODAY - timedelta(days=2)
    settings = curve_settings(database)
    gateway = gateway_for(governor, clock, partial_curves={recent}, stale_curve_cache=True)
    run_pass(settings, gateway, store, [], clock)
    assert gateway.uncached_calls == []  # nothing was known to be partial yet

    gateway.partial_curves.clear()
    next_day(clock, gateway)
    run_pass(settings, gateway, store, [], clock)

    assert len(store.curve(PDL, Direction.CONSUMPTION, recent, recent + timedelta(days=1))) == 48


def test_refetching_unchanged_curve_days_changes_nothing(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    """Yesterday unpublished and an older day partial: every pass asks for both."""
    partial = TODAY - timedelta(days=4)
    settings = curve_settings(database)
    gateway = FakeGateway(
        governor, clock, published_until=TODAY - timedelta(days=1), partial_curves={partial}
    )
    run_pass(settings, gateway, store, [], clock)
    gateway.calls.clear()

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert (PDL, "consumption_load_curve", partial, TODAY) in gateway.calls
    assert "curve_consumption +0" in outcome.detail


def test_a_partial_curve_day_settles_after_settle_days(store: Store) -> None:
    """Age SETTLE_DAYS-1 is still asked again; age SETTLE_DAYS is kept as it is."""
    unsettled = TODAY - timedelta(days=SETTLE_DAYS - 1)
    settled = TODAY - timedelta(days=SETTLE_DAYS)
    run = store.start_run(NOW)
    store.upsert_curve(run, curve_of(PDL, Direction.CONSUMPTION, unsettled)[:24])
    store.upsert_curve(run, curve_of(PDL, Direction.CONSUMPTION, settled)[:24])

    assert partial_days(store, PDL, Dataset.CURVE_CONSUMPTION, 30, TODAY) == {unsettled}
    assert partial_days(store, PDL, Dataset.DAILY_CONSUMPTION, 30, TODAY) == set()
    assert partial_days(store, PDL, Dataset.CURVE_PRODUCTION, 30, TODAY) == set()


def test_a_recent_partial_production_curve_rides_along(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    recent = TODAY - timedelta(days=1)
    settings = curve_settings(database, production_detail=True, consumption_detail=False)
    gateway = gateway_for(governor, clock, partial_curves={recent})
    run_pass(settings, gateway, store, [], clock)
    assert partial_days(store, PDL, Dataset.CURVE_PRODUCTION, 10, TODAY) == {recent}

    gateway.partial_curves.clear()
    today = next_day(clock, gateway)
    run_pass(settings, gateway, store, [], clock)

    assert (PDL, "production_load_curve", recent, today) in gateway.uncached_calls
    assert partial_days(store, PDL, Dataset.CURVE_PRODUCTION, 10, today) == set()
    assert len(store.curve(PDL, Direction.PRODUCTION, recent, recent + timedelta(days=1))) == 48


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
        raise NotFoundError("contracts: the gateway holds nothing for it (HTTP 404)")

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
        raise NotFoundError("identity: the gateway holds nothing for it (HTTP 404)")

    gateway.identity = refused  # type: ignore[method-assign]
    (first,) = run_pass(settings, gateway, store, [], clock).outcomes
    assert not first.ok
    assert "holds nothing" in store.customer_failures(PDL)[CustomerResource.IDENTITY].detail

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


def test_recent_days_not_published_yet_are_awaited_not_failed(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    """Observed live: before Enedis publishes yesterday, the gateway answers 404 every night."""
    settings = make_settings(
        database,
        usage_points=[
            {"id": PDL, "consumption": False, "consumption_detail": True, "contract": False}
        ],
        sync={"history_days": 14},
    )
    recent = TODAY - timedelta(days=7)
    gateway = gateway_for(governor, clock, unpublished_windows={recent})

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert outcome.ok
    assert "curve_consumption +" in outcome.detail
    assert outcome.detail.endswith("(not published yet)")
    assert store.confirmed_gaps(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max) == set()
    remaining = backlog(store, PDL, Dataset.CURVE_CONSUMPTION, 14, TODAY)
    assert remaining == [TODAY - timedelta(days=n) for n in range(1, 8)]  # asked again next pass


def test_settled_days_the_gateway_holds_nothing_for_become_gaps(
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
    gateway = gateway_for(governor, clock, unpublished_windows={old_window_start})

    (outcome,) = run_pass(settings, gateway, store, [], clock).outcomes

    assert outcome.ok
    assert "not published yet" not in outcome.detail
    gaps = store.confirmed_gaps(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max)
    assert gaps == {old_window_start + timedelta(days=n) for n in range(7)}


def test_a_refused_window_stretched_to_a_partial_day_still_settles_its_missing_days(
    database: Path, store: Store, governor: QuotaGovernor, clock: FrozenClock
) -> None:
    partial = TODAY - timedelta(days=4)
    run = store.start_run(NOW)
    for age in range(1, 9):
        day = TODAY - timedelta(days=age)
        points = curve_of(PDL, Direction.CONSUMPTION, day)
        store.upsert_curve(run, points[:24] if day == partial else points)
    oldest = TODAY - timedelta(days=10)
    gateway = gateway_for(governor, clock, refused_windows={oldest})

    (outcome,) = run_pass(curve_settings(database), gateway, store, [], clock).outcomes

    assert (PDL, "consumption_load_curve", oldest, partial + timedelta(days=1)) in gateway.calls
    assert outcome.ok
    gaps = store.confirmed_gaps(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max)
    assert gaps == {oldest, oldest + timedelta(days=1)}
