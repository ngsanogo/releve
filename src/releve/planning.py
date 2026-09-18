"""What to ask the gateway for — pure functions, no I/O.

A usage point's history window is kept COMPLETE, not merely extended: every
day in it that is still to fetch is asked for, newest first, until the gateway
has either delivered it or confirmed — long enough after the fact — that it has
nothing for that day.

Some days are worth asking for again but never worth a call of their own, like
a load-curve day Enedis published only partially: they ride along in the
windows planned for the missing days.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import date, timedelta

from releve.domain import Dataset

# Enedis may publish or correct a day a few days late. Only an empty answer
# obtained this long after the day is taken as final.
SETTLE_DAYS = 7
ONE_DAY = timedelta(days=1)


def history_start(today: date, dataset: Dataset, history_days: int) -> date:
    return today - timedelta(days=min(history_days, dataset.max_history_days))


def missing_days(start: date, end: date, known: Collection[date]) -> list[date]:
    """Days in [start, end) that are not in `known`, newest first."""
    span = (end - start).days
    days = (end - timedelta(days=offset) for offset in range(1, span + 1))
    return [day for day in days if day not in known]


def plan_windows(
    missing: Sequence[date], max_days: int, refresh: Iterable[date] = ()
) -> list[tuple[date, date]]:
    """Cover newest-first `missing` days with as few [start, end) windows as possible.

    Each window spans at most `max_days`, starts at a missing day and ends the
    day after one; windows come out newest first. Greedy is optimal here:
    anchoring each window on the newest uncovered day covers the most.

    `refresh` days never add a window: a window is stretched, within `max_days`,
    to take in those it can reach; the others wait for a later pass.
    """
    windows = []
    index = 0
    while index < len(missing):
        end = missing[index] + ONE_DAY
        floor = end - timedelta(days=max_days)
        oldest = index
        while oldest + 1 < len(missing) and missing[oldest + 1] >= floor:
            oldest += 1
        windows.append((missing[oldest], end))
        index = oldest + 1
    for day in sorted(refresh, reverse=True):
        if not any(start <= day < end for start, end in windows):
            _stretch_to(day, windows, max_days)
    return windows


def _stretch_to(day: date, windows: list[tuple[date, date]], max_days: int) -> None:
    """Stretch the newest window that can take in `day` without spanning more than `max_days`.

    A stretched window never reaches into another one: from a greedy window's
    start to the end of any newer window is more than `max_days`.
    """
    for number, (start, end) in enumerate(windows):
        stretched = (min(start, day), max(end, day + ONE_DAY))
        if stretched[1] - stretched[0] <= timedelta(days=max_days):
            windows[number] = stretched
            return


def settled_gaps(asked: Iterable[date], delivered: Collection[date], today: date) -> set[date]:
    """Asked-for days the gateway answered without data, old enough to be final."""
    horizon = today - timedelta(days=SETTLE_DAYS)
    return {day for day in asked if day not in delivered and day <= horizon}
