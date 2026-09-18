"""What to ask the gateway for — pure functions, no I/O.

A usage point's history window is kept COMPLETE, not merely extended: every
day in it that is still to fetch is asked for, newest first, until the gateway
has either delivered it or confirmed — long enough after the fact — that it has
nothing for that day. For the load curve, "still to fetch" also covers recent
days whose cached points do not yet form a complete grid (see `curve.py`).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import date, timedelta

from releve.domain import Dataset

# Enedis may publish or correct a day a few days late. Only an empty answer
# obtained this long after the day is taken as final.
SETTLE_DAYS = 7


def history_start(today: date, dataset: Dataset, history_days: int) -> date:
    return today - timedelta(days=min(history_days, dataset.max_history_days))


def missing_days(start: date, end: date, known: Collection[date]) -> list[date]:
    """Days in [start, end) that are not in `known`, newest first."""
    span = (end - start).days
    days = (end - timedelta(days=offset) for offset in range(1, span + 1))
    return [day for day in days if day not in known]


def plan_windows(missing: Sequence[date], max_days: int) -> list[tuple[date, date]]:
    """Cover newest-first `missing` days with as few [start, end) windows as possible.

    Each window spans at most `max_days`, starts at a missing day and ends the
    day after one; windows come out newest first. Greedy is optimal here:
    anchoring each window on the newest uncovered day covers the most.
    """
    windows = []
    index = 0
    while index < len(missing):
        end = missing[index] + timedelta(days=1)
        floor = end - timedelta(days=max_days)
        oldest = index
        while oldest + 1 < len(missing) and missing[oldest + 1] >= floor:
            oldest += 1
        windows.append((missing[oldest], end))
        index = oldest + 1
    return windows


def settled_gaps(asked: Iterable[date], delivered: Collection[date], today: date) -> set[date]:
    """Asked-for days the gateway answered without data, old enough to be final."""
    horizon = today - timedelta(days=SETTLE_DAYS)
    return {day for day in asked if day not in delivered and day <= horizon}
