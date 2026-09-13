"""The store: schema lifecycle, quota bookkeeping, change feed, and never forgetting."""

from __future__ import annotations

import fcntl
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from releve.clock import day_start, next_utc_midnight, utc_midnight
from releve.domain import (
    CustomerResource,
    DailyEnergy,
    Dataset,
    Direction,
    EcowattDay,
    Identity,
    LoadCurvePoint,
    PowerPeak,
)
from releve.errors import StoreError
from releve.store import CustomerFailure, HaBoundary, Refusal, Store, migrations
from tests.conftest import NOW, PDL, FrozenClock

DAY = date(2026, 9, 10)
C = Direction.CONSUMPTION


def reserve(store: Store, at: datetime = NOW, budget: int = 3) -> int | Refusal:
    return store.reserve_call(
        PDL,
        "daily_consumption",
        at=at,
        day_start=utc_midnight(at),
        resets_at=next_utc_midnight(at),
        budget=budget,
    )


@pytest.mark.usefixtures("store")
def test_a_new_database_is_private_and_at_the_latest_schema(database: Path) -> None:
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.parent.stat().st_mode & 0o777 == 0o700
    with closing(sqlite3.connect(database)) as conn, conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(migrations())
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    Store.open(database)  # reopening is a no-op


def test_a_database_from_the_future_is_refused(database: Path, store: Store) -> None:
    del store
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(StoreError, match="newer than this releve"):
        Store.open(database)


def test_upgrading_from_version_1_dates_ecowatt_days_one_day_later(database: Path) -> None:
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.executescript(migrations()[0])
        conn.executemany(
            "INSERT INTO ecowatt_day (day, level, message) VALUES (?, ?, ?)",
            [("2026-09-11", 1, "a"), ("2026-09-12", 2, "b"), ("2026-09-13", 3, "c")],
        )
        conn.execute("PRAGMA user_version = 1")

    store = Store.open(database)

    assert store.ecowatt(date.min, date.max) == [
        EcowattDay(date(2026, 9, 12), 1, "a"),
        EcowattDay(date(2026, 9, 13), 2, "b"),
        EcowattDay(date(2026, 9, 14), 3, "c"),
    ]


def test_a_customer_failure_is_forgotten_once_the_resource_is_cached(store: Store) -> None:
    store.record_customer_failure(PDL, CustomerResource.IDENTITY, at=NOW, detail="HTTP 404")
    store.record_customer_failure(PDL, CustomerResource.CONTACT, at=NOW, detail="HTTP 500")
    assert store.customer_failures(PDL) == {
        CustomerResource.IDENTITY: CustomerFailure(NOW, "HTTP 404"),
        CustomerResource.CONTACT: CustomerFailure(NOW, "HTTP 500"),
    }
    assert store.customer_fetched_at(PDL, CustomerResource.IDENTITY) is None

    store.upsert_identity(Identity(PDL, firstname="Ada"), at=NOW)

    assert set(store.customer_failures(PDL)) == {CustomerResource.CONTACT}
    assert store.customer_fetched_at(PDL, CustomerResource.IDENTITY) == NOW


def test_an_unusable_path_is_a_store_error(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("not a directory")
    with pytest.raises(StoreError, match="cannot open database"):
        Store.open(tmp_path / "file" / "cache.db")


def test_reservations_stop_at_the_budget_and_resume_at_utc_midnight(store: Store) -> None:
    assert [isinstance(reserve(store), int) for _ in range(3)] == [True, True, True]
    refusal = reserve(store)
    assert refusal == Refusal(next_utc_midnight(NOW), "daily budget spent (3/3)")
    assert isinstance(reserve(store, at=next_utc_midnight(NOW)), int)


def test_calls_count_whatever_their_outcome(store: Store) -> None:
    for status in (200, 429, None):
        call_id = reserve(store)
        assert isinstance(call_id, int)
        store.settle_call(call_id, status)
    assert isinstance(reserve(store), Refusal)


def test_a_block_is_honored_and_the_later_block_wins(store: Store) -> None:
    store.block(PDL, NOW + timedelta(hours=2), "throttled")
    store.block(PDL, NOW + timedelta(hours=1), "shorter, ignored")
    assert reserve(store) == Refusal(NOW + timedelta(hours=2), "throttled")
    assert isinstance(reserve(store, at=NOW + timedelta(hours=2, seconds=1)), int)
    assert (
        store.quota_usage(
            PDL, at=NOW + timedelta(hours=3), day_start=utc_midnight(NOW)
        ).blocked_until
        is None
    )


def test_concurrent_reservations_never_overspend(database: Path, store: Store) -> None:
    del store
    results: list[int | Refusal] = []
    lock = threading.Lock()

    def worker() -> None:
        outcome = reserve(Store(database), budget=10)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(result, int) for result in results) == 10


def test_upserts_report_changes_and_stamp_only_changed_rows(store: Store) -> None:
    run_1, run_2 = store.start_run(NOW), store.start_run(NOW)
    readings = [DailyEnergy(PDL, C, DAY, 5000), DailyEnergy(PDL, C, DAY + timedelta(days=1), 6000)]
    assert store.upsert_daily(run_1, readings) == 2
    assert store.upsert_daily(run_2, readings) == 0
    assert store.upsert_daily(run_2, [DailyEnergy(PDL, C, DAY, 5100)]) == 1

    assert store.changed_daily(after_run=run_1, up_to_run=run_2) == [DailyEnergy(PDL, C, DAY, 5100)]
    assert store.earliest_changed_day(PDL, C, after_run=0, up_to_run=run_2) == DAY
    assert store.earliest_changed_day(PDL, C, after_run=run_2, up_to_run=run_2) is None


def test_an_answer_with_less_data_never_deletes_the_cache(store: Store) -> None:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(PDL, C, DAY, 5000)])
    store.upsert_daily(run, [])
    assert store.daily(PDL, C, date.min, date.max) == [DailyEnergy(PDL, C, DAY, 5000)]


def test_a_midnight_curve_point_closes_the_previous_day(store: Store) -> None:
    run = store.start_run(NOW)
    midnight = day_start(DAY + timedelta(days=1))
    points = [
        LoadCurvePoint(PDL, C, midnight - timedelta(minutes=30), 400),
        LoadCurvePoint(PDL, C, midnight, 300),
    ]
    assert store.upsert_curve(run, points) == 2
    assert store.days_with_data(PDL, Dataset.CURVE_CONSUMPTION, date.min, date.max) == {DAY}
    assert store.curve(PDL, C, DAY, DAY + timedelta(days=1)) == points
    assert store.changed_curve(after_run=0, up_to_run=run) == points


def test_days_and_latest_day_per_dataset(store: Store) -> None:
    run = store.start_run(NOW)
    store.upsert_daily(run, [DailyEnergy(PDL, Direction.PRODUCTION, DAY, 100)])
    store.upsert_peaks(run, [PowerPeak(PDL, DAY, 6000, datetime(2026, 9, 10, 17, tzinfo=UTC))])
    assert store.days_with_data(PDL, Dataset.DAILY_PRODUCTION, DAY, DAY + timedelta(days=1)) == {
        DAY
    }
    assert store.days_with_data(PDL, Dataset.DAILY_CONSUMPTION, date.min, date.max) == set()
    assert store.latest_day(PDL, Dataset.MAX_POWER) == DAY
    assert store.latest_day(PDL, Dataset.CURVE_PRODUCTION) is None
    assert store.changed_peaks(after_run=0, up_to_run=run)[0].va == 6000


def test_confirmed_gaps_are_remembered_once(store: Store) -> None:
    store.add_confirmed_gaps(PDL, Dataset.MAX_POWER, [DAY, DAY])
    store.add_confirmed_gaps(PDL, Dataset.MAX_POWER, [DAY])
    assert store.confirmed_gaps(PDL, Dataset.MAX_POWER, date.min, date.max) == {DAY}
    assert store.confirmed_gaps(PDL, Dataset.DAILY_CONSUMPTION, date.min, date.max) == set()


def test_export_cursors_and_boundaries(store: Store, clock: FrozenClock) -> None:
    assert store.export_cursor("sink") == 0
    store.advance_export_cursor("sink", 7, clock())
    assert store.export_cursor("sink") == 7
    assert [cursor.run_id for cursor in store.export_cursors()] == [7]

    boundary = HaBoundary("source:series", date(2026, 8, 28), 8481.922000000004)
    store.set_ha_boundary(boundary, restart_sinks=None)
    store.set_ha_boundary(HaBoundary("source:fresh", None, 0.0, PDL), restart_sinks=None)
    assert store.ha_boundary("source:series") == boundary
    assert [b.statistic_id for b in store.ha_boundaries()] == ["source:fresh", "source:series"]
    assert store.ha_boundary("source:missing") is None


def test_the_journal_forgets_old_runs_and_calls(store: Store) -> None:
    old = store.start_run(NOW - timedelta(days=100))
    store.record_event(old, NOW - timedelta(days=100), PDL, True, "old")
    recent = store.start_run(NOW)
    store.record_event(recent, NOW, PDL, False, "recent")
    reserve(store, at=NOW - timedelta(days=40))

    store.prune_journal(runs_before=NOW - timedelta(days=90), calls_before=NOW - timedelta(days=30))

    assert [event.detail for event in store.recent_events(10)] == ["recent"]
    assert store.last_success() == {}
    with closing(sqlite3.connect(store.path)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM gateway_call").fetchone()[0] == 0


def test_pinning_a_boundary_can_restart_deliveries(store: Store, clock: FrozenClock) -> None:
    store.advance_export_cursor("home_assistant ws://ha a:b c:d", 9, clock())
    store.advance_export_cursor("influxdb http://influx", 9, clock())
    store.set_ha_boundary(HaBoundary("a:b", None, 1.0), restart_sinks="home_assistant ")
    assert store.export_cursor("home_assistant ws://ha a:b c:d") == 0
    assert store.export_cursor("influxdb http://influx") == 9


def test_a_new_database_is_refused_next_to_an_existing_cache(tmp_path: Path) -> None:
    existing = tmp_path / "history.db"
    Store.open(existing)
    (tmp_path / "unrelated.db").write_bytes(b"not a database")
    with pytest.raises(StoreError, match=r"set storage\.path to"):
        Store.open(tmp_path / "releve.db")
    assert not (tmp_path / "releve.db").exists()
    Store.open(existing)  # pointing at it explicitly is fine


def test_opening_waits_for_another_opening(database: Path) -> None:
    """Switching to WAL and migrating fail at once, without waiting, when another
    connection does the same: opening is serialized by a lock file instead."""
    database.parent.mkdir(parents=True)
    opened = threading.Event()
    lock_path = database.with_name(f"{database.name}.open.lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)

        def open_store() -> None:
            Store.open(database)
            opened.set()

        opener = threading.Thread(target=open_store)
        opener.start()
        assert not opened.wait(0.3)
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert opened.wait(5)
    opener.join()
