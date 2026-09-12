"""Upgrading a database in the legacy layout in place.

The fixture below is the exact schema SQLAlchemy created in that layout, with
rows in the exact text formats it wrote.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from releve.domain import Direction
from releve.errors import QuotaExhaustedError, StoreError
from releve.quota import QuotaGovernor
from releve.store import HaBoundary, Store, migrations
from tests.conftest import NOW, FrozenClock

LEGACY_PDL = "09876543210987"
ARCHIVE = "myelectricaldata:09876543210987_consumption_full"

LEGACY_SCHEMA = """
CREATE TABLE schema_info (version INTEGER NOT NULL, PRIMARY KEY (version));
CREATE TABLE daily_reading (
    usage_point_id VARCHAR(14) NOT NULL, day DATE NOT NULL, direction VARCHAR(12) NOT NULL,
    wh INTEGER NOT NULL, PRIMARY KEY (usage_point_id, day, direction));
CREATE TABLE load_curve_point (
    usage_point_id VARCHAR(14) NOT NULL, ts DATETIME NOT NULL, direction VARCHAR(12) NOT NULL,
    watts INTEGER NOT NULL, PRIMARY KEY (usage_point_id, ts, direction));
CREATE TABLE power_peak (
    usage_point_id VARCHAR(14) NOT NULL, day DATE NOT NULL, va INTEGER NOT NULL,
    at DATETIME NOT NULL, PRIMARY KEY (usage_point_id, day));
CREATE TABLE tempo_day (day DATE NOT NULL, color VARCHAR(8) NOT NULL, PRIMARY KEY (day));
CREATE TABLE ecowatt_day (
    day DATE NOT NULL, value INTEGER NOT NULL, message VARCHAR(255) NOT NULL, PRIMARY KEY (day));
CREATE TABLE gateway_call (
    id INTEGER NOT NULL, ts_utc DATETIME NOT NULL, usage_point_id VARCHAR(14) NOT NULL,
    endpoint VARCHAR(64) NOT NULL, status INTEGER NOT NULL, PRIMARY KEY (id));
CREATE INDEX ix_gateway_call_ts_utc ON gateway_call (ts_utc);
CREATE INDEX ix_gateway_call_usage_point_id ON gateway_call (usage_point_id);
CREATE TABLE quota_block (
    usage_point_id VARCHAR(14) NOT NULL, until_utc DATETIME NOT NULL, cause VARCHAR(255) NOT NULL,
    PRIMARY KEY (usage_point_id));
CREATE TABLE sync_run (
    id INTEGER NOT NULL, usage_point_id VARCHAR(14) NOT NULL, started_utc DATETIME NOT NULL,
    finished_utc DATETIME, ok INTEGER NOT NULL, detail VARCHAR(1000) NOT NULL, PRIMARY KEY (id));
CREATE INDEX ix_sync_run_usage_point_id ON sync_run (usage_point_id);
CREATE TABLE ha_export_boundary (
    statistic_id VARCHAR(255) NOT NULL, base_day DATE, base_sum FLOAT NOT NULL,
    PRIMARY KEY (statistic_id));
"""

LEGACY_ROWS = """
INSERT INTO schema_info VALUES (2);
INSERT INTO daily_reading VALUES ('09876543210987', '2026-08-28', 'consumption', 9100);
INSERT INTO load_curve_point VALUES
    ('09876543210987', '2026-08-29 00:30:00.000000', 'consumption', 500),
    ('09876543210987', '2026-08-30 00:00:00.000000', 'consumption', 450);
INSERT INTO power_peak VALUES ('09876543210987', '2026-08-28', 6000, '2026-08-28 19:12:00.000000');
INSERT INTO tempo_day VALUES ('2026-09-12', 'BLUE');
INSERT INTO ecowatt_day VALUES ('2026-09-12', 1, 'ok');
INSERT INTO gateway_call VALUES
    (1, '2026-09-12 08:00:00.000000', '09876543210987', 'daily_consumption', 200),
    (2, '2026-09-12 08:01:00.000000', '_reference_', 'rte', 0),
    (3, '2026-09-11 23:00:00.000000', '09876543210987', 'daily_consumption', 200);
INSERT INTO quota_block VALUES
    ('09876543210987', '2026-09-12 20:00:00.000000', 'upstream throttle'),
    ('_reference_', '2026-09-12 09:00:00.000000', 'expired before the upgrade');
INSERT INTO sync_run VALUES
    (1, '09876543210987', '2026-09-12 08:00:00.000000', '2026-09-12 08:01:00.000000', 1, 'x');
INSERT INTO ha_export_boundary VALUES
    ('myelectricaldata:09876543210987_consumption_full', '2026-08-28', 8481.922000000004);
"""


@pytest.fixture
def legacy_database(database: Path) -> Path:
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.executescript(LEGACY_SCHEMA + LEGACY_ROWS)
    return database


def test_a_legacy_database_is_migrated_in_place_with_a_backup(
    legacy_database: Path, clock: FrozenClock
) -> None:
    store = Store.open(legacy_database, clock)

    backups = list(legacy_database.parent.glob("releve.db.pre-migration-*.bak"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup, backup:
        assert backup.execute("SELECT version FROM schema_info").fetchone() == (2,)
    with closing(sqlite3.connect(legacy_database)) as conn, conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(migrations())
    assert not {"schema_info", "daily_reading", "sync_run"} & tables
    assert not any(name.startswith("legacy_") for name in tables)

    assert store.ha_boundary(ARCHIVE) == HaBoundary(ARCHIVE, date(2026, 8, 28), 8481.922000000004)
    assert [
        (r.day, r.wh) for r in store.daily(LEGACY_PDL, Direction.CONSUMPTION, date.min, date.max)
    ] == [(date(2026, 8, 28), 9100)]
    curve = store.curve(LEGACY_PDL, Direction.CONSUMPTION, date.min, date.max)
    assert [(p.end, p.day, p.watts) for p in curve] == [
        (datetime(2026, 8, 28, 22, 30, tzinfo=UTC), date(2026, 8, 29), 500),
        (datetime(2026, 8, 29, 22, 0, tzinfo=UTC), date(2026, 8, 29), 450),
    ]
    (peak,) = store.peaks(LEGACY_PDL, date.min, date.max)
    assert peak.at == datetime(2026, 8, 28, 17, 12, tzinfo=UTC)
    assert store.tempo(date.min, date.max)[0].color == "BLUE"
    assert store.ecowatt(date.min, date.max)[0].level == 1
    (event,) = store.recent_events(5)
    assert event.subject == "migration"
    assert "ha_boundary 1" in event.detail

    Store.open(legacy_database, clock)  # a second open changes nothing
    assert len(list(legacy_database.parent.glob("*.bak"))) == 1


def test_the_budget_and_active_blocks_survive_the_upgrade(
    legacy_database: Path, clock: FrozenClock
) -> None:
    store = Store.open(legacy_database, clock)
    governor = QuotaGovernor(store, daily_budget=45, clock=clock)

    usage = governor.usage(LEGACY_PDL)
    assert usage.used == 1  # yesterday's call is not today's
    assert usage.blocked_until == datetime(2026, 9, 12, 20, 0, tzinfo=UTC)
    assert governor.usage("rte").used == 1
    assert governor.usage("rte").blocked_until is None  # it had already expired
    with pytest.raises(QuotaExhaustedError, match="upstream throttle"):
        governor.reserve(LEGACY_PDL, "daily_consumption")
    clock.advance(timedelta(hours=11))
    assert NOW + timedelta(hours=11) > usage.blocked_until
    governor.reserve(LEGACY_PDL, "daily_consumption")


def test_a_failed_migration_leaves_the_legacy_database_untouched(
    legacy_database: Path, clock: FrozenClock
) -> None:
    with closing(sqlite3.connect(legacy_database)) as conn, conn:
        conn.execute("INSERT INTO daily_reading VALUES ('x', 'not a date', 'consumption', 1)")

    with pytest.raises(StoreError, match="left it untouched"):
        Store.open(legacy_database, clock)

    with closing(sqlite3.connect(legacy_database)) as conn, conn:
        assert conn.execute("SELECT count(*) FROM daily_reading").fetchone() == (2,)
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)


def test_a_retried_migration_keeps_the_first_backup(
    legacy_database: Path, clock: FrozenClock
) -> None:
    with closing(sqlite3.connect(legacy_database)) as conn, conn:
        conn.execute("INSERT INTO daily_reading VALUES ('x', 'not a date', 'consumption', 1)")
    for _ in range(3):
        with pytest.raises(StoreError):
            Store.open(legacy_database, clock)
        clock.advance(timedelta(seconds=5))
    assert len(list(legacy_database.parent.glob("*.bak"))) == 1


def test_concurrent_opens_migrate_once(legacy_database: Path, clock: FrozenClock) -> None:
    errors: list[BaseException] = []

    def open_store() -> None:
        try:
            Store.open(legacy_database, clock)
        except BaseException as exc:  # noqa: BLE001 — collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert Store.open(legacy_database, clock).ha_boundary(ARCHIVE) is not None
