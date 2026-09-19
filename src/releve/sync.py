"""One sync pass: Tempo and Ecowatt, then every usage point, then every exporter.

The rules a pass lives by:

* one pass at a time per database, across processes (an advisory file lock);
* a quota refusal, a refused token or an unreachable gateway stops the usage
  point it concerns — never the others;
* consent is checked before metering; an invalid or banned consent stops the
  usage point without burning the rest of the budget;
* a customer resource the gateway cannot give is asked again a day later, not
  on every pass;
* a load-curve day published only partially is asked again until it is settled,
  but only inside a call made for a missing day, and past the gateway's cache;
* recent days the gateway holds nothing for yet are not a failure: Enedis has
  not published them, and the next pass asks again;
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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import assert_never

from releve.clock import Clock, paris_today, utc_now
from releve.config import Settings, UsagePointSettings
from releve.curve import incomplete_days
from releve.domain import CustomerResource, Dataset, Direction
from releve.errors import (
    AuthError,
    ExportError,
    GatewayError,
    GatewayUnreachableError,
    NotFoundError,
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
# A customer resource the gateway could not give is asked again after this long.
CUSTOMER_RETRY = timedelta(days=1)

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
class Fetched:
    """What fetching one dataset did: cached rows changed, and whether recent days are awaited."""

    changed: int
    awaiting_publication: bool

    def __str__(self) -> str:
        return f"+{self.changed}" + (" (not published yet)" if self.awaiting_publication else "")


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
            journal(sync_rte(gateway, store, today, clock))
        for usage_point in settings.usage_points:
            journal(sync_usage_point(usage_point, settings, gateway, store, run_id, today, clock))
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


def sync_rte(gateway: Gateway, store: Store, today: date, clock: Clock = utc_now) -> Outcome:
    """Tempo colors, Ecowatt (daily + hourly); Tempo season and prices once per Paris day."""
    try:
        tempo = gateway.tempo(today - TEMPO_BACKFILL, today + timedelta(days=2))
        store.upsert_tempo(tempo)
        # RTE publishes Ecowatt up to three days ahead; yesterday's signal comes along.
        forecast = gateway.ecowatt(today - timedelta(days=1), today + timedelta(days=4))
        store.upsert_ecowatt(forecast.days)
        store.upsert_ecowatt_hours(forecast.hours)
        detail = (
            f"tempo: {len(tempo)} days, ecowatt: {len(forecast.days)} days/"
            f"{len(forecast.hours)} hours"
        )
        fetched_at = store.tempo_extras_fetched_at()
        if fetched_at is None or paris_today(fetched_at) < today:
            now = clock()
            store.upsert_tempo_season(gateway.tempo_season(), at=now)
            prices = gateway.tempo_prices()
            store.upsert_tempo_prices(prices, at=now)
            detail += f", season + {len(prices)} prices"
    except GatewayError as exc:
        return Outcome(RTE_BUCKET, False, str(exc))
    return Outcome(RTE_BUCKET, True, detail)


def sync_usage_point(
    usage_point: UsagePointSettings,
    settings: Settings,
    gateway: Gateway,
    store: Store,
    run_id: int,
    today: date,
    clock: Clock = utc_now,
) -> Outcome:
    now = clock()
    history_days = settings.sync.history_days
    customer_due = due_customer_resources(usage_point, settings, store, now)
    todo = {
        dataset: backlog(store, usage_point.id, dataset, history_days, today)
        for dataset in usage_point.datasets
    }
    if not customer_due and not any(todo.values()):
        return Outcome(usage_point.id, True, "nothing to fetch")  # an idle pass asks nothing

    try:
        consent = gateway.valid_access(usage_point.id)
        store.upsert_consent(consent, at=now)
    except GatewayError as exc:
        return Outcome(usage_point.id, False, str(exc))
    if not consent.granted:
        reason = consent.information or ("banned" if consent.banned else "consent invalid")
        return Outcome(usage_point.id, False, f"consent refused: {reason}")
    changes = ["consent ok"]
    problems: list[str] = []

    try:
        refreshed, problems = sync_customer(usage_point.id, customer_due, gateway, store, now)
        changes.extend(refreshed)
    except (RetryLaterError, AuthError, GatewayUnreachableError) as exc:
        problems.append(str(exc))
        detail = "; ".join(filter(None, [", ".join(changes), *problems]))
        return Outcome(usage_point.id, False, detail)

    for dataset, missing in todo.items():
        try:
            fetched = sync_dataset(
                usage_point.id, dataset, missing, history_days, gateway, store, run_id, today
            )
        except (RetryLaterError, AuthError, GatewayUnreachableError) as exc:
            problems.append(str(exc))
            break  # nothing more can be asked for this usage point right now
        except GatewayError as exc:
            problems.append(str(exc))
            continue
        changes.append(f"{dataset} {fetched}")
    detail = "; ".join(filter(None, [", ".join(changes), *problems])) or "nothing to fetch"
    return Outcome(usage_point.id, not problems, detail)


def due_customer_resources(
    usage_point: UsagePointSettings, settings: Settings, store: Store, now: datetime
) -> list[CustomerResource]:
    """The enabled customer resources to fetch now.

    A resource is due when its cached copy is missing or older than the refresh
    period — unless its last fetch failed less than `CUSTOMER_RETRY` ago.
    """
    refresh_after = timedelta(days=settings.sync.customer_refresh_days)
    failures = store.customer_failures(usage_point.id)
    return [
        resource
        for resource in usage_point.customer_resources
        if _due(store.customer_fetched_at(usage_point.id, resource), now, refresh_after)
        and (resource not in failures or _due(failures[resource].failed_at, now, CUSTOMER_RETRY))
    ]


def sync_customer(
    pdl: str, due: Sequence[CustomerResource], gateway: Gateway, store: Store, now: datetime
) -> tuple[list[str], list[str]]:
    """Refresh the `due` customer resources; returns those refreshed and the problems met.

    A resource the gateway cannot give is recorded as failed, which holds its
    next attempt back, and does not keep the others from refreshing. A quota,
    token or transport failure is not the resource's doing: it is raised, as
    nothing more can be asked for this usage point.
    """
    changed: list[str] = []
    problems: list[str] = []
    for resource in due:
        try:
            match resource:
                case CustomerResource.CONTRACT:
                    store.upsert_contract(gateway.contract(pdl), at=now)
                case CustomerResource.IDENTITY:
                    store.upsert_identity(gateway.identity(pdl), at=now)
                case CustomerResource.CONTACT:
                    store.upsert_contact(gateway.contact(pdl), at=now)
                case CustomerResource.ADDRESSES:
                    store.upsert_address(gateway.addresses(pdl), at=now)
                case _:
                    assert_never(resource)
        except (RetryLaterError, AuthError, GatewayUnreachableError):
            raise
        except GatewayError as exc:
            store.record_customer_failure(pdl, resource, at=now, detail=str(exc))
            problems.append(str(exc))
            continue
        changed.append(resource)
    return changed, problems


def _due(fetched_at: datetime | None, now: datetime, refresh_after: timedelta) -> bool:
    return fetched_at is None or now - fetched_at >= refresh_after


def backlog(
    store: Store, usage_point: str, dataset: Dataset, history_days: int, today: date
) -> list[date]:
    """The days of the history window still to fetch, newest first.

    A day is still to fetch while it holds no data and is not a confirmed gap.
    """
    start = history_start(today, dataset, history_days)
    known = store.days_with_data(usage_point, dataset, start, today)
    known |= store.confirmed_gaps(usage_point, dataset, start, today)
    return missing_days(start, today, known)


def partial_days(
    store: Store, usage_point: str, dataset: Dataset, history_days: int, today: date
) -> set[date]:
    """The unsettled days of a load-curve dataset whose curve is not a complete grid yet.

    Enedis may publish a curve day partially and complete it later. Such a day is
    worth asking for again, but not worth a call: it rides along in the windows
    of the missing days — and each new day brings one, yesterday. Once settled,
    an incomplete day is kept as it is.
    """
    if not dataset.is_curve:
        return set()
    unsettled = today - timedelta(days=SETTLE_DAYS - 1)
    start = max(history_start(today, dataset, history_days), unsettled)
    return incomplete_days(store.curve(usage_point, _DIRECTION[dataset], start, today))


def sync_dataset(
    usage_point: str,
    dataset: Dataset,
    missing: Sequence[date],
    history_days: int,
    gateway: Gateway,
    store: Store,
    run_id: int,
    today: date,
) -> Fetched:
    """Fetch the `missing` days of `dataset` (see `backlog`).

    Partial load-curve days ride along (see `partial_days`). A window holding one
    skips the gateway's cache: its copy of that day may be the same partial answer.

    A window the gateway holds nothing for (404) or refuses (400) is skipped. When
    all the missing days it holds are settled (typically before the meter's
    activation or beyond what Enedis keeps), they become confirmed gaps. Otherwise
    a 404 means Enedis has not published them yet (observed live: every night,
    until the morning) and the next pass asks again, while a 400 is raised once
    the other windows are done.
    """
    partial = partial_days(store, usage_point, dataset, history_days, today) if missing else set()
    changed = 0
    awaiting_publication = False
    refused: WindowRejectedError | None = None
    for start, end in plan_windows(missing, dataset.window_days, refresh=partial):
        asked = [day for day in missing if start <= day < end]
        use_cache = not any(start <= day < end for day in partial)
        settled = max(asked) <= today - timedelta(days=SETTLE_DAYS)
        try:
            delivered, window_changes = _fetch_window(
                dataset, usage_point, start, end, gateway, store, run_id, use_cache=use_cache
            )
        except NotFoundError:
            if not settled:
                awaiting_publication = True
                continue
            delivered, window_changes = set(), 0
        except WindowRejectedError as exc:
            if not settled:
                refused = exc
                continue
            delivered, window_changes = set(), 0
        changed += window_changes
        store.add_confirmed_gaps(usage_point, dataset, settled_gaps(asked, delivered, today))
    if refused is not None:
        raise refused
    return Fetched(changed, awaiting_publication)


def _fetch_window(
    dataset: Dataset,
    usage_point: str,
    start: date,
    end: date,
    gateway: Gateway,
    store: Store,
    run_id: int,
    *,
    use_cache: bool,
) -> tuple[set[date], int]:
    """Fetch [start, end), cache what falls inside it; return the days delivered and rows changed.

    Answers are clamped to the window because the gateway sometimes returns more
    than asked (observed live: the load curve includes the `end` day).
    `use_cache` only matters to the load curve, the one dataset with partial days.
    """
    match dataset:
        case Dataset.DAILY_CONSUMPTION | Dataset.DAILY_PRODUCTION:
            answer = gateway.daily(usage_point, _DIRECTION[dataset], start, end)
            daily = [reading for reading in answer if start <= reading.day < end]
            return {reading.day for reading in daily}, store.upsert_daily(run_id, daily)
        case Dataset.CURVE_CONSUMPTION | Dataset.CURVE_PRODUCTION:
            answer_curve = gateway.load_curve(
                usage_point, _DIRECTION[dataset], start, end, use_cache=use_cache
            )
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
