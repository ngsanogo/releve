"""The local cache: one SQLite file, through the standard library.

* Schema changes are numbered SQL scripts in `migrations/`, applied in order;
  `PRAGMA user_version` records the last one applied.
* Instants are stored as unix seconds (UTC), Paris civil days as ISO dates.
* A sync pass is a *run*. Every metering row carries the id of the run that
  last CHANGED it, which is how exporters find what is new since they last
  delivered.
* Metering data is upserted, never deleted by a sync: an answer that holds
  less than the cache cannot make the cache forget.
* Each operation opens its own short-lived connection, so web threads, the
  scheduler thread and a concurrent CLI process never share one.
"""

from __future__ import annotations

import fcntl
import logging
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path

from releve import legacy
from releve.clock import Clock, from_unix, to_unix, utc_now
from releve.domain import (
    DailyEnergy,
    Dataset,
    Direction,
    EcowattDay,
    LoadCurvePoint,
    PowerPeak,
    TempoDay,
)
from releve.errors import StoreError

log = logging.getLogger(__name__)

BUSY_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a gateway call may not be sent, and until when."""

    until: datetime
    cause: str


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    used: int
    blocked_until: datetime | None
    blocked_cause: str | None


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: int
    at: datetime
    subject: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ExportCursor:
    sink: str
    run_id: int
    exported_at: datetime


@dataclass(frozen=True, slots=True)
class HaBoundary:
    """The last point a Home Assistant series held before our first import."""

    statistic_id: str
    base_day: date | None
    base_sum_kwh: float
    usage_point: str | None = None


def migrations() -> list[str]:
    """The schema scripts, in order; script N brings the schema to version N."""
    scripts = sorted(
        (
            entry
            for entry in files("releve").joinpath("migrations").iterdir()
            if entry.name.endswith(".sql")
        ),
        key=lambda entry: entry.name,
    )
    for number, entry in enumerate(scripts, start=1):
        if not re.match(rf"^{number:04d}_\w+\.sql$", entry.name):
            raise StoreError(f"migration {entry.name!r} is out of sequence (expected {number:04d})")
    return [entry.read_text(encoding="utf-8") for entry in scripts]


def _caches_beside(path: Path) -> list[Path]:
    """Other SQLite files next to `path` that hold a cache, current or legacy layout."""
    found = []
    for candidate in sorted(path.parent.glob("*.db")) if path.parent.is_dir() else []:
        if candidate == path:
            continue
        try:
            with closing(sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema")}
        except sqlite3.DatabaseError:
            continue
        if tables & {"schema_info", "daily_energy"}:
            found.append(candidate)
    return found


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # IMMEDIATE takes the write lock up front: two processes can never
    # interleave a read-then-write, which the quota reservation relies on.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class Store:
    """The single door to the database. Build it with `Store.open`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def open(cls, path: Path, clock: Clock = utc_now) -> Store:
        """Create the database if needed and bring its schema up to date."""
        store = cls(path)
        store._prepare(clock())
        return store

    # -- lifecycle ----------------------------------------------------------------------
    def _prepare(self, now: datetime) -> None:
        if not self.path.exists():
            # Never start an empty cache next to the history it should continue.
            for other in _caches_beside(self.path):
                raise StoreError(
                    f"{self.path} does not exist but {other} holds a cache: set storage.path "
                    f"to {other} to keep its history, or move it away to start from scratch"
                )
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Opening is serialized across threads and processes: switching to WAL
            # and migrating both need an exclusive lock, which SQLite refuses at
            # once (without waiting) when another connection is doing the same.
            with _file_lock(self.path.with_name(f"{self.path.name}.open.lock")):
                with self._connect() as conn:
                    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                        conn.execute("PRAGMA journal_mode = WAL")
                    self._migrate(conn, now)
                self.path.chmod(0o600)
        except (OSError, sqlite3.OperationalError) as exc:
            raise StoreError(f"cannot open database {self.path}: {exc}") from exc

    def _migrate(self, conn: sqlite3.Connection, now: datetime) -> None:
        scripts = migrations()
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(scripts):
            raise StoreError(
                f"{self.path} has schema version {version}, newer than this releve "
                f"understands ({len(scripts)}) — upgrade releve"
            )
        if version == 0 and legacy.is_legacy_database(conn):
            backup = self._backup(now)
            log.warning("migrating a database in the legacy layout; backup kept at %s", backup)
            try:
                with _transaction(conn):
                    legacy.set_aside(conn)
                    for script in scripts:
                        conn.executescript(script)
                    counts = legacy.import_data(conn, now)
                    legacy.drop(conn)
                    conn.execute(f"PRAGMA user_version = {len(scripts)}")
            except (ValueError, TypeError, KeyError, sqlite3.DatabaseError) as exc:
                raise StoreError(
                    f"migrating the legacy database failed and left it untouched ({exc}); "
                    f"a copy is at {backup}"
                ) from exc
            log.warning("legacy database migrated: %s", counts)
            return
        for number, script in enumerate(scripts[version:], start=version + 1):
            with _transaction(conn):
                conn.executescript(script)
                conn.execute(f"PRAGMA user_version = {number}")
            log.info("database schema upgraded to version %d", number)

    def _backup(self, now: datetime) -> Path:
        """Copy the legacy database once; a retried migration reuses the first copy."""
        existing = sorted(self.path.parent.glob(f"{self.path.name}.pre-migration-*.bak"))
        if existing:
            return existing[0]
        target = self.path.with_name(f"{self.path.name}.pre-migration-{now:%Y%m%dT%H%M%SZ}.bak")
        with self._connect() as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
        target.chmod(0o600)
        return target

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, autocommit=True)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn, _transaction(conn):
            yield conn

    # -- runs and journal -------------------------------------------------------------
    def start_run(self, at: datetime) -> int:
        with self._write() as conn:
            cursor = conn.execute("INSERT INTO run (started_at) VALUES (?)", (to_unix(at),))
        assert cursor.lastrowid is not None  # noqa: S101 — sqlite3 always sets it after INSERT
        return cursor.lastrowid

    def finish_run(self, run_id: int, at: datetime) -> None:
        with self._write() as conn:
            conn.execute("UPDATE run SET finished_at = ? WHERE id = ?", (to_unix(at), run_id))

    def record_event(self, run_id: int, at: datetime, subject: str, ok: bool, detail: str) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO run_event (run_id, at, subject, ok, detail) VALUES (?, ?, ?, ?, ?)",
                (run_id, to_unix(at), subject, int(ok), detail),
            )

    def recent_events(self, limit: int) -> list[RunEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, at, subject, ok, detail FROM run_event ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunEvent(r[0], from_unix(r[1]), r[2], bool(r[3]), r[4]) for r in rows]

    def last_success(self) -> dict[str, datetime]:
        """When each subject last succeeded."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT subject, max(at) FROM run_event WHERE ok = 1 GROUP BY subject"
            ).fetchall()
        return {subject: from_unix(at) for subject, at in rows}

    def prune_journal(self, *, runs_before: datetime, calls_before: datetime) -> None:
        """Forget old runs (and their events) and old gateway calls."""
        with self._write() as conn:
            conn.execute("DELETE FROM run WHERE started_at < ?", (to_unix(runs_before),))
            conn.execute("DELETE FROM gateway_call WHERE reserved_at < ?", (to_unix(calls_before),))

    # -- quota bookkeeping --------------------------------------------------------------
    def reserve_call(
        self,
        bucket: str,
        endpoint: str,
        *,
        at: datetime,
        day_start: datetime,
        resets_at: datetime,
        budget: int,
    ) -> int | Refusal:
        """Atomically check the block and the budget, then record the call.

        Recording BEFORE the call is sent means a crash or a transport failure
        still counts: the conservative reading of an ambiguous situation.
        """
        with self._write() as conn:
            block = conn.execute(
                "SELECT until, cause FROM quota_block WHERE bucket = ?", (bucket,)
            ).fetchone()
            if block is not None:
                if block[0] > to_unix(at):
                    return Refusal(from_unix(block[0]), block[1])
                conn.execute("DELETE FROM quota_block WHERE bucket = ?", (bucket,))
            used = _calls_since(conn, bucket, day_start)
            if used >= budget:
                return Refusal(resets_at, f"daily budget spent ({used}/{budget})")
            cursor = conn.execute(
                "INSERT INTO gateway_call (bucket, endpoint, reserved_at) VALUES (?, ?, ?)",
                (bucket, endpoint, to_unix(at)),
            )
        assert cursor.lastrowid is not None  # noqa: S101 — sqlite3 always sets it after INSERT
        return cursor.lastrowid

    def settle_call(self, call_id: int, status: int | None) -> None:
        with self._write() as conn:
            conn.execute("UPDATE gateway_call SET status = ? WHERE id = ?", (status, call_id))

    def block(self, bucket: str, until: datetime, cause: str) -> None:
        """Refuse calls for `bucket` until `until` — an existing later block wins."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO quota_block (bucket, until, cause) VALUES (?, ?, ?) "
                "ON CONFLICT (bucket) DO UPDATE SET until = excluded.until, cause = excluded.cause "
                "WHERE excluded.until > quota_block.until",
                (bucket, to_unix(until), cause),
            )

    def quota_usage(self, bucket: str, *, at: datetime, day_start: datetime) -> QuotaUsage:
        with self._connect() as conn:
            used = _calls_since(conn, bucket, day_start)
            block = conn.execute(
                "SELECT until, cause FROM quota_block WHERE bucket = ? AND until > ?",
                (bucket, to_unix(at)),
            ).fetchone()
        if block is None:
            return QuotaUsage(used, None, None)
        return QuotaUsage(used, from_unix(block[0]), block[1])

    # -- metering writes ----------------------------------------------------------------
    def upsert_daily(self, run_id: int, rows: Iterable[DailyEnergy]) -> int:
        """Insert or update daily energy; returns how many rows actually changed."""
        with self._write() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT INTO daily_energy (usage_point, direction, day, wh, run_id) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (usage_point, direction, day) "
                "DO UPDATE SET wh = excluded.wh, run_id = excluded.run_id "
                "WHERE daily_energy.wh IS NOT excluded.wh",
                [(r.usage_point, r.direction, r.day.isoformat(), r.wh, run_id) for r in rows],
            )
            return conn.total_changes - before

    def upsert_curve(self, run_id: int, points: Iterable[LoadCurvePoint]) -> int:
        """Insert or update load-curve points; returns how many rows actually changed."""
        with self._write() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT INTO load_curve (usage_point, direction, end_at, day, watts, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (usage_point, direction, end_at) "
                "DO UPDATE SET watts = excluded.watts, run_id = excluded.run_id "
                "WHERE load_curve.watts IS NOT excluded.watts",
                [
                    (p.usage_point, p.direction, to_unix(p.end), p.day.isoformat(), p.watts, run_id)
                    for p in points
                ],
            )
            return conn.total_changes - before

    def upsert_peaks(self, run_id: int, peaks: Iterable[PowerPeak]) -> int:
        """Insert or update daily power peaks; returns how many rows actually changed."""
        with self._write() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT INTO power_peak (usage_point, day, va, at, run_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (usage_point, day) "
                "DO UPDATE SET va = excluded.va, at = excluded.at, run_id = excluded.run_id "
                "WHERE power_peak.va IS NOT excluded.va OR power_peak.at IS NOT excluded.at",
                [(p.usage_point, p.day.isoformat(), p.va, to_unix(p.at), run_id) for p in peaks],
            )
            return conn.total_changes - before

    def add_confirmed_gaps(self, usage_point: str, dataset: Dataset, days: Iterable[date]) -> None:
        with self._write() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO confirmed_gap (usage_point, dataset, day) VALUES (?, ?, ?)",
                [(usage_point, dataset, day.isoformat()) for day in days],
            )

    # -- metering reads -----------------------------------------------------------------
    def days_with_data(
        self, usage_point: str, dataset: Dataset, start: date, end: date
    ) -> set[date]:
        """Days in [start, end) holding at least one row of `dataset`."""
        with self._connect() as conn:
            rows = conn.execute(
                _DAYS_WITH_DATA[dataset], (usage_point, start.isoformat(), end.isoformat())
            ).fetchall()
        return {date.fromisoformat(row[0]) for row in rows}

    def confirmed_gaps(
        self, usage_point: str, dataset: Dataset, start: date, end: date
    ) -> set[date]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day FROM confirmed_gap "
                "WHERE usage_point = ? AND dataset = ? AND day >= ? AND day < ?",
                (usage_point, dataset, start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(row[0]) for row in rows}

    def latest_day(self, usage_point: str, dataset: Dataset) -> date | None:
        with self._connect() as conn:
            (day,) = conn.execute(_LATEST_DAY[dataset], (usage_point,)).fetchone()
        return date.fromisoformat(day) if day is not None else None

    def daily(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[DailyEnergy]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, wh FROM daily_energy "
                "WHERE usage_point = ? AND direction = ? AND day >= ? AND day < ? ORDER BY day",
                (usage_point, direction, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [DailyEnergy(usage_point, direction, date.fromisoformat(d), wh) for d, wh in rows]

    def curve(
        self, usage_point: str, direction: Direction, start: date, end: date
    ) -> list[LoadCurvePoint]:
        """Load-curve points of the Paris days in [start, end), in time order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT end_at, watts FROM load_curve "
                "WHERE usage_point = ? AND direction = ? AND day >= ? AND day < ? ORDER BY end_at",
                (usage_point, direction, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [LoadCurvePoint(usage_point, direction, from_unix(e), w) for e, w in rows]

    def peaks(self, usage_point: str, start: date, end: date) -> list[PowerPeak]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, va, at FROM power_peak "
                "WHERE usage_point = ? AND day >= ? AND day < ? ORDER BY day",
                (usage_point, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            PowerPeak(usage_point, date.fromisoformat(d), va, from_unix(at)) for d, va, at in rows
        ]

    # -- change feed ----------------------------------------------------------------------
    def earliest_changed_day(
        self, usage_point: str, direction: Direction, *, after_run: int, up_to_run: int
    ) -> date | None:
        """The oldest day whose daily energy or load curve changed in (after_run, up_to_run]."""
        with self._connect() as conn:
            (day,) = conn.execute(
                "SELECT min(day) FROM ("
                "  SELECT day FROM daily_energy WHERE usage_point = ? AND direction = ?"
                "    AND run_id > ? AND run_id <= ?"
                "  UNION ALL"
                "  SELECT day FROM load_curve WHERE usage_point = ? AND direction = ?"
                "    AND run_id > ? AND run_id <= ?"
                ")",
                (usage_point, direction, after_run, up_to_run) * 2,
            ).fetchone()
        return date.fromisoformat(day) if day is not None else None

    def changed_daily(self, *, after_run: int, up_to_run: int) -> list[DailyEnergy]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT usage_point, direction, day, wh FROM daily_energy "
                "WHERE run_id > ? AND run_id <= ? ORDER BY usage_point, direction, day",
                (after_run, up_to_run),
            ).fetchall()
        return [
            DailyEnergy(up, Direction(d), date.fromisoformat(day), wh) for up, d, day, wh in rows
        ]

    def changed_curve(self, *, after_run: int, up_to_run: int) -> list[LoadCurvePoint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT usage_point, direction, end_at, watts FROM load_curve "
                "WHERE run_id > ? AND run_id <= ? ORDER BY usage_point, direction, end_at",
                (after_run, up_to_run),
            ).fetchall()
        return [LoadCurvePoint(up, Direction(d), from_unix(e), w) for up, d, e, w in rows]

    def changed_peaks(self, *, after_run: int, up_to_run: int) -> list[PowerPeak]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT usage_point, day, va, at FROM power_peak "
                "WHERE run_id > ? AND run_id <= ? ORDER BY usage_point, day",
                (after_run, up_to_run),
            ).fetchall()
        return [PowerPeak(up, date.fromisoformat(d), va, from_unix(at)) for up, d, va, at in rows]

    # -- Tempo and Ecowatt ----------------------------------------------------------------
    def upsert_tempo(self, days: Iterable[TempoDay]) -> None:
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO tempo_day (day, color) VALUES (?, ?) "
                "ON CONFLICT (day) DO UPDATE SET color = excluded.color",
                [(d.day.isoformat(), d.color) for d in days],
            )

    def upsert_ecowatt(self, days: Iterable[EcowattDay]) -> None:
        with self._write() as conn:
            conn.executemany(
                "INSERT INTO ecowatt_day (day, level, message) VALUES (?, ?, ?) "
                "ON CONFLICT (day) "
                "DO UPDATE SET level = excluded.level, message = excluded.message",
                [(d.day.isoformat(), d.level, d.message) for d in days],
            )

    def tempo(self, start: date, end: date) -> list[TempoDay]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, color FROM tempo_day WHERE day >= ? AND day < ? ORDER BY day",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [TempoDay(date.fromisoformat(d), color) for d, color in rows]

    def ecowatt(self, start: date, end: date) -> list[EcowattDay]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT day, level, message FROM ecowatt_day "
                "WHERE day >= ? AND day < ? ORDER BY day",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [EcowattDay(date.fromisoformat(d), level, msg) for d, level, msg in rows]

    # -- export cursors -------------------------------------------------------------------
    def export_cursor(self, sink: str) -> int:
        """The last run whose changes `sink` delivered; 0 when it never delivered."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM export_cursor WHERE sink = ?", (sink,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def advance_export_cursor(self, sink: str, run_id: int, at: datetime) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO export_cursor (sink, run_id, exported_at) VALUES (?, ?, ?) "
                "ON CONFLICT (sink) DO UPDATE SET "
                "run_id = excluded.run_id, exported_at = excluded.exported_at",
                (sink, run_id, to_unix(at)),
            )

    def export_cursors(self) -> list[ExportCursor]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sink, run_id, exported_at FROM export_cursor ORDER BY sink"
            ).fetchall()
        return [ExportCursor(sink, run_id, from_unix(at)) for sink, run_id, at in rows]

    # -- Home Assistant boundaries -------------------------------------------------------
    def ha_boundary(self, statistic_id: str) -> HaBoundary | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT statistic_id, base_day, base_sum, usage_point FROM ha_boundary "
                "WHERE statistic_id = ?",
                (statistic_id,),
            ).fetchone()
        return _boundary(row) if row is not None else None

    def set_ha_boundary(self, boundary: HaBoundary, *, restart_sinks: str | None) -> None:
        """Pin a boundary. With `restart_sinks`, the export cursors of sinks starting
        with that prefix are dropped in the same transaction, so their next delivery
        rewrites everything after the new boundary."""
        base_day = boundary.base_day.isoformat() if boundary.base_day is not None else None
        with self._write() as conn:
            conn.execute(
                "INSERT INTO ha_boundary (statistic_id, usage_point, base_day, base_sum) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (statistic_id) DO UPDATE SET "
                "usage_point = excluded.usage_point, base_day = excluded.base_day, "
                "base_sum = excluded.base_sum",
                (boundary.statistic_id, boundary.usage_point, base_day, boundary.base_sum_kwh),
            )
            if restart_sinks is not None:
                conn.execute(
                    "DELETE FROM export_cursor WHERE substr(sink, 1, ?) = ?",
                    (len(restart_sinks), restart_sinks),
                )

    def ha_boundaries(self) -> list[HaBoundary]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT statistic_id, base_day, base_sum, usage_point FROM ha_boundary "
                "ORDER BY statistic_id"
            ).fetchall()
        return [_boundary(row) for row in rows]


def _boundary(row: tuple[str, str | None, float, str | None]) -> HaBoundary:
    statistic_id, base_day, base_sum, usage_point = row
    day = date.fromisoformat(base_day) if base_day is not None else None
    return HaBoundary(statistic_id, day, base_sum, usage_point)


def _calls_since(conn: sqlite3.Connection, bucket: str, since: datetime) -> int:
    (used,) = conn.execute(
        "SELECT count(*) FROM gateway_call WHERE bucket = ? AND reserved_at >= ?",
        (bucket, to_unix(since)),
    ).fetchone()
    return int(used)


_DAYS_WITH_DATA = {
    Dataset.DAILY_CONSUMPTION: (
        "SELECT day FROM daily_energy WHERE usage_point = ? "
        "AND direction = 'consumption' AND day >= ? AND day < ?"
    ),
    Dataset.DAILY_PRODUCTION: (
        "SELECT day FROM daily_energy WHERE usage_point = ? "
        "AND direction = 'production' AND day >= ? AND day < ?"
    ),
    Dataset.MAX_POWER: "SELECT day FROM power_peak WHERE usage_point = ? AND day >= ? AND day < ?",
    Dataset.CURVE_CONSUMPTION: (
        "SELECT DISTINCT day FROM load_curve WHERE usage_point = ? "
        "AND direction = 'consumption' AND day >= ? AND day < ?"
    ),
    Dataset.CURVE_PRODUCTION: (
        "SELECT DISTINCT day FROM load_curve WHERE usage_point = ? "
        "AND direction = 'production' AND day >= ? AND day < ?"
    ),
}

_LATEST_DAY = {
    Dataset.DAILY_CONSUMPTION: (
        "SELECT max(day) FROM daily_energy WHERE usage_point = ? AND direction = 'consumption'"
    ),
    Dataset.DAILY_PRODUCTION: (
        "SELECT max(day) FROM daily_energy WHERE usage_point = ? AND direction = 'production'"
    ),
    Dataset.MAX_POWER: "SELECT max(day) FROM power_peak WHERE usage_point = ?",
    Dataset.CURVE_CONSUMPTION: (
        "SELECT max(day) FROM load_curve WHERE usage_point = ? AND direction = 'consumption'"
    ),
    Dataset.CURVE_PRODUCTION: (
        "SELECT max(day) FROM load_curve WHERE usage_point = ? AND direction = 'production'"
    ),
}
