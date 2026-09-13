"""One-way import of a database in the earlier, SQLAlchemy-based layout.

That layout stored naive local timestamps for metering data, naive UTC for
bookkeeping, and a `schema_info` table instead of `user_version`.
`Store.open` backs the file up, then calls these steps inside ONE transaction:
set the old tables aside, create the current schema, copy the data over, drop
the old tables. Either everything is migrated or nothing is.

What is kept: every metering row, Tempo and Ecowatt, today's gateway calls and
active quota blocks (the budget must survive the upgrade), and the Home
Assistant export boundaries (they anchor a cumulative series). What is not:
the old sync journal, which does not match the run model.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

from releve.clock import PARIS, to_unix
from releve.domain import Direction, LoadCurvePoint

log = logging.getLogger(__name__)

_TABLES = (
    "schema_info",
    "daily_reading",
    "load_curve_point",
    "power_peak",
    "tempo_day",
    "ecowatt_day",
    "gateway_call",
    "quota_block",
    "sync_run",
    "ha_export_boundary",
)
_REFERENCE_BUCKET = "_reference_"  # the earlier quota bucket for Tempo/Ecowatt, now "rte"


def is_legacy_database(conn: sqlite3.Connection) -> bool:
    return _exists(conn, "schema_info")


def set_aside(conn: sqlite3.Connection) -> None:
    for table in _present(conn):
        conn.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_{table}"')  # names are ours


def drop(conn: sqlite3.Connection) -> None:
    for table in _TABLES:
        conn.execute(f'DROP TABLE IF EXISTS "legacy_{table}"')


def import_data(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """Copy the set-aside tables into the current schema; returns rows copied per table."""
    run_id = conn.execute(
        "INSERT INTO run (started_at, finished_at) VALUES (?, ?)", (to_unix(now), to_unix(now))
    ).lastrowid
    counts = {
        "daily_energy": _daily(conn, run_id),
        "load_curve": _curve(conn, run_id),
        "power_peak": _peaks(conn, run_id),
        "tempo_day": _tempo(conn),
        "ecowatt_day": _ecowatt(conn),
        "gateway_call": _calls(conn),
        "quota_block": _blocks(conn, now),
        "ha_boundary": _boundaries(conn),
    }
    detail = ", ".join(f"{table} {n}" for table, n in counts.items())
    conn.execute(
        "INSERT INTO run_event (run_id, at, subject, ok, detail) VALUES (?, ?, 'migration', 1, ?)",
        (run_id, to_unix(now), f"imported from the legacy layout: {detail}"),
    )
    return counts


def _daily(conn: sqlite3.Connection, run_id: int | None) -> int:
    if not _exists(conn, "legacy_daily_reading"):
        return 0
    rows = conn.execute(
        "SELECT usage_point_id, direction, day, wh FROM legacy_daily_reading"
    ).fetchall()
    conn.executemany(
        "INSERT INTO daily_energy (usage_point, direction, day, wh, run_id) VALUES (?, ?, ?, ?, ?)",
        [(up, direction, _day(day), wh, run_id) for up, direction, day, wh in rows],
    )
    return len(rows)


def _curve(conn: sqlite3.Connection, run_id: int | None) -> int:
    if not _exists(conn, "legacy_load_curve_point"):
        return 0
    rows = conn.execute(
        "SELECT usage_point_id, direction, ts, watts FROM legacy_load_curve_point ORDER BY ts"
    ).fetchall()
    converted = []
    for up, direction, ts, watts in rows:
        # The legacy layout stored interval ENDS as naive Paris wall-clock time, already
        # deduplicated at the October fall-back: read each as its first occurrence.
        point = LoadCurvePoint(up, Direction(direction), _paris(ts), watts)
        converted.append((up, direction, to_unix(point.end), point.day.isoformat(), watts, run_id))
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO load_curve (usage_point, direction, end_at, day, watts, run_id) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        converted,
    )
    inserted = conn.total_changes - before
    if inserted != len(rows):
        log.warning(
            "%d legacy load-curve points collapsed onto an existing instant", len(rows) - inserted
        )
    return inserted


def _peaks(conn: sqlite3.Connection, run_id: int | None) -> int:
    if not _exists(conn, "legacy_power_peak"):
        return 0
    rows = conn.execute("SELECT usage_point_id, day, va, at FROM legacy_power_peak").fetchall()
    conn.executemany(
        "INSERT INTO power_peak (usage_point, day, va, at, run_id) VALUES (?, ?, ?, ?, ?)",
        [(up, _day(day), va, to_unix(_paris(at)), run_id) for up, day, va, at in rows],
    )
    return len(rows)


def _calls(conn: sqlite3.Connection) -> int:
    if not _exists(conn, "legacy_gateway_call"):
        return 0
    rows = conn.execute(
        "SELECT usage_point_id, endpoint, ts_utc, status FROM legacy_gateway_call"
    ).fetchall()
    conn.executemany(
        "INSERT INTO gateway_call (bucket, endpoint, reserved_at, status) VALUES (?, ?, ?, ?)",
        [
            (_bucket(bucket), endpoint, _utc(ts), status or None)
            for bucket, endpoint, ts, status in rows
        ],
    )
    return len(rows)


def _blocks(conn: sqlite3.Connection, now: datetime) -> int:
    if not _exists(conn, "legacy_quota_block"):
        return 0
    rows = conn.execute(
        "SELECT usage_point_id, until_utc, cause FROM legacy_quota_block"
    ).fetchall()
    active = [
        (_bucket(b), _utc(until), cause) for b, until, cause in rows if _utc(until) > to_unix(now)
    ]
    conn.executemany("INSERT INTO quota_block (bucket, until, cause) VALUES (?, ?, ?)", active)
    return len(active)


def _boundaries(conn: sqlite3.Connection) -> int:
    if not _exists(conn, "legacy_ha_export_boundary"):
        return 0
    rows = conn.execute(
        "SELECT statistic_id, base_day, base_sum FROM legacy_ha_export_boundary"
    ).fetchall()
    conn.executemany(
        "INSERT INTO ha_boundary (statistic_id, usage_point, base_day, base_sum) "
        "VALUES (?, NULL, ?, ?)",
        [(sid, _day(day) if day is not None else None, float(total)) for sid, day, total in rows],
    )
    return len(rows)


def _tempo(conn: sqlite3.Connection) -> int:
    if not _exists(conn, "legacy_tempo_day"):
        return 0
    rows = conn.execute("SELECT day, color FROM legacy_tempo_day").fetchall()
    conn.executemany(
        "INSERT INTO tempo_day (day, color) VALUES (?, ?)",
        [(_day(day), color) for day, color in rows],
    )
    return len(rows)


def _ecowatt(conn: sqlite3.Connection) -> int:
    if not _exists(conn, "legacy_ecowatt_day"):
        return 0
    rows = conn.execute("SELECT day, value, message FROM legacy_ecowatt_day").fetchall()
    # The legacy layout dated each signal by the gateway's day key, one day early.
    conn.executemany(
        "INSERT INTO ecowatt_day (day, level, message) VALUES (?, ?, ?)",
        [(_next_day(day), value, message or "") for day, value, message in rows],
    )
    return len(rows)


def _day(value: str) -> str:
    return date.fromisoformat(value[:10]).isoformat()


def _next_day(value: str) -> str:
    return (date.fromisoformat(value[:10]) + timedelta(days=1)).isoformat()


def _paris(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=PARIS, fold=0)


def _utc(value: str) -> int:
    return to_unix(datetime.fromisoformat(value).replace(tzinfo=UTC))


def _bucket(value: str) -> str:
    return "rte" if value == _REFERENCE_BUCKET else value


def _present(conn: sqlite3.Connection) -> list[str]:
    return [table for table in _TABLES if _exists(conn, table)]


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None
