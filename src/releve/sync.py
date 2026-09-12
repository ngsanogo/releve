"""One sync pass: Tempo and Ecowatt, then every usage point, then every exporter.

The rules a pass lives by:

* one pass at a time per database, across processes (an advisory file lock);
* a quota refusal, a refused token or an unreachable gateway stops the usage
  point it concerns — never the others;
* every answer is cached before any exporter runs; exporters read the cache
  from where they last stopped, so a failed delivery is retried, not lost;
* every outcome, success or failure, is journaled.
"""

from __future__ import annotations

import fcntl
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import assert_never

from releve.clock import Clock, paris_today, utc_now
from releve.config import Settings, UsagePointSettings
from releve.domain import Dataset, Direction
from releve.errors import (
    AuthError,
    ExportError,
    GatewayError,
    GatewayUnreachableError,
    RetryLaterError,
    SyncAlreadyRunningError,
    WindowRejectedError,
)
from releve.exporters import Exporter
from releve.gateway import Gateway
from releve.planning import (
    SETTLE_DAYS,
    history_start,
    missing_days,
    plan_windows,
    settled_gaps,
)
from releve.quota import RTE_BUCKET
from releve.store import Store

log = logging.getLogger(__name__)

JOURNAL_KEEP = timedelta(days=90)
GATEWAY_CALLS_KEEP = timedelta(days=30)
TEMPO_BACKFILL = timedelta(days=30)

_DIRECTION = {
    Dataset.DAILY_CONSUMPTION: Direction.CONSUMPTION,
    Dataset.DAILY_PRODUCTION: Direction.PRODUCTION,
    Dataset.CURVE_CONSUMPTION: Direction.CONSUMPTION,
    Dataset.CURVE_PRODUCTION: Direction.PRODUCTION,
}


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a pass did for one subject: a usage point, `rte`, or `export:<name>`."""

    subject: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PassReport:
    run_id: int
    outcomes: tuple[Outcome, ...]

    @property
    def ok(self) -> bool:
        return all(outcome.ok for outcome in self.outcomes)


def run_pass(
    settings: Settings,
    gateway: Gateway,
    store: Store,
    exporters: Sequence[Exporter],
    clock: Clock = utc_now,
) -> PassReport:
    """Run one complete pass; raises `SyncAlreadyRunningError` if another one is running."""
    with exclusive_pass(store.path):
        started = clock()
        today = paris_today(started)
        run_id = store.start_run(started)
        outcomes: list[Outcome] = []

        def journal(outcome: Outcome) -> None:
            store.record_event(run_id, clock(), outcome.subject, outcome.ok, outcome.detail)
            level = logging.INFO if outcome.ok else logging.WARNING
            log.log(level, "%s: %s", outcome.subject, outcome.detail)
            outcomes.append(outcome)

        if settings.sync.rte_signals:
            journal(sync_rte(gateway, store, today))
        for usage_point in settings.usage_points:
            journal(sync_usage_point(usage_point, settings, gateway, store, run_id, today))
        for exporter in exporters:
            journal(export(exporter, store, run_id, clock))

        finished = clock()
        store.prune_journal(
            runs_before=finished - JOURNAL_KEEP, calls_before=finished - GATEWAY_CALLS_KEEP
        )
        store.finish_run(run_id, finished)
    return PassReport(run_id, tuple(outcomes))


@contextmanager
def exclusive_pass(database: Path) -> Iterator[None]:
    """Hold the database's pass lock, or raise at once if another process holds it."""
    lock_path = database.with_name(f"{database.name}.lock")
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncAlreadyRunningError(f"another sync pass is running ({lock_path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sync_rte(gateway: Gateway, store: Store, today: date) -> Outcome:
    """Tempo colors (with a month of history) and Ecowatt signals (with the days ahead)."""
    try:
        tempo = gateway.tempo(today - TEMPO_BACKFILL, today + timedelta(days=2))
        store.upsert_tempo(tempo)
        ecowatt = gateway.ecowatt(today - timedelta(days=1), today + timedelta(days=3))
        store.upsert_ecowatt(ecowatt)
    except GatewayError as exc:
        return Outcome(RTE_BUCKET, False, str(exc))
    return Outcome(RTE_BUCKET, True, f"tempo: {len(tempo)} days, ecowatt: {len(ecowatt)} days")


def sync_usage_point(
    usage_point: UsagePointSettings,
    settings: Settings,
    gateway: Gateway,
    store: Store,
    run_id: int,
    today: date,
) -> Outcome:
    changes: list[str] = []
    problems: list[str] = []
    for dataset in usage_point.datasets:
        try:
            changed = sync_dataset(
                usage_point.id, dataset, settings.sync.history_days, gateway, store, run_id, today
            )
        except (RetryLaterError, AuthError, GatewayUnreachableError) as exc:
            problems.append(str(exc))
            break  # nothing more can be asked for this usage point right now
        except GatewayError as exc:
            problems.append(str(exc))
            continue
        changes.append(f"{dataset} +{changed}")
    detail = "; ".join(filter(None, [", ".join(changes), *problems])) or "nothing to fetch"
    return Outcome(usage_point.id, not problems, detail)


def backlog(
    store: Store, usage_point: str, dataset: Dataset, history_days: int, today: date
) -> list[date]:
    """The days of the history window still to fetch, newest first."""
    start = history_start(today, dataset, history_days)
    known = store.days_with_data(usage_point, dataset, start, today)
    known |= store.confirmed_gaps(usage_point, dataset, start, today)
    return missing_days(start, today, known)


def sync_dataset(
    usage_point: str,
    dataset: Dataset,
    history_days: int,
    gateway: Gateway,
    store: Store,
    run_id: int,
    today: date,
) -> int:
    """Fetch every missing day of `dataset`; returns how many cached rows changed.

    A window the gateway explicitly refuses is skipped: when all its days are
    settled (typically before the meter's activation or beyond what Enedis
    keeps), they become confirmed gaps; otherwise the refusal is raised once
    the other windows are done.
    """
    missing = backlog(store, usage_point, dataset, history_days, today)
    changed = 0
    refused: WindowRejectedError | None = None
    for start, end in plan_windows(missing, dataset.window_days):
        asked = [day for day in missing if start <= day < end]
        try:
            delivered, window_changes = _fetch_window(
                dataset, usage_point, start, end, gateway, store, run_id
            )
        except WindowRejectedError as exc:
            if end - timedelta(days=1) > today - timedelta(days=SETTLE_DAYS):
                refused = exc
                continue
            delivered, window_changes = set(), 0
        changed += window_changes
        store.add_confirmed_gaps(usage_point, dataset, settled_gaps(asked, delivered, today))
    if refused is not None:
        raise refused
    return changed


def _fetch_window(
    dataset: Dataset,
    usage_point: str,
    start: date,
    end: date,
    gateway: Gateway,
    store: Store,
    run_id: int,
) -> tuple[set[date], int]:
    """Fetch [start, end), cache what falls inside it; return the days delivered and rows changed.

    Answers are clamped to the window because the gateway sometimes returns more
    than asked (observed live: the load curve includes the `end` day).
    """
    match dataset:
        case Dataset.DAILY_CONSUMPTION | Dataset.DAILY_PRODUCTION:
            answer = gateway.daily(usage_point, _DIRECTION[dataset], start, end)
            daily = [reading for reading in answer if start <= reading.day < end]
            return {reading.day for reading in daily}, store.upsert_daily(run_id, daily)
        case Dataset.CURVE_CONSUMPTION | Dataset.CURVE_PRODUCTION:
            answer_curve = gateway.load_curve(usage_point, _DIRECTION[dataset], start, end)
            points = [point for point in answer_curve if start <= point.day < end]
            return {point.day for point in points}, store.upsert_curve(run_id, points)
        case Dataset.MAX_POWER:
            answer_peaks = gateway.max_power(usage_point, start, end)
            peaks = [peak for peak in answer_peaks if start <= peak.day < end]
            return {peak.day for peak in peaks}, store.upsert_peaks(run_id, peaks)
        case _:
            assert_never(dataset)


def export(exporter: Exporter, store: Store, run_id: int, clock: Clock = utc_now) -> Outcome:
    """Deliver what changed since the exporter's cursor; advance the cursor only on success."""
    subject = f"export:{exporter.name}"
    after_run = store.export_cursor(exporter.sink)
    try:
        detail = exporter.export(store, after_run=after_run, up_to_run=run_id)
    except ExportError as exc:
        return Outcome(subject, False, str(exc))
    store.advance_export_cursor(exporter.sink, run_id, clock())
    return Outcome(subject, True, detail)
