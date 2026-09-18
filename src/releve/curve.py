"""Load-curve arithmetic: when a day's curve can be trusted, and the energy it holds.

A day's curve is used only when it is a complete, regular grid: N points ending
exactly at start + k * step (k = 1..N), N * step equal to the day's length (23,
24 or 25 hours) and a step that divides an hour. Anything else would be a guess.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from releve.clock import day_end, day_start
from releve.domain import LoadCurvePoint

ONE_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Interval:
    """One metering interval [start, end) and the energy consumed or produced in it."""

    start: datetime
    end: datetime
    wh: float


def intervals(day: date, points: Sequence[LoadCurvePoint]) -> list[Interval] | None:
    """The day's intervals in time order, or None when its curve is not a complete grid."""
    if not points:
        return None
    start, end = day_start(day), day_end(day)
    step = (end - start) / len(points)
    if step * len(points) != end - start or ONE_HOUR % step:
        return None
    ordered = sorted(points, key=lambda point: point.end)
    if any(point.end != start + step * k for k, point in enumerate(ordered, start=1)):
        return None
    hours = step / ONE_HOUR
    return [Interval(point.end - step, point.end, point.watts * hours) for point in ordered]


def incomplete_days(points: Iterable[LoadCurvePoint]) -> set[date]:
    """Days among `points` whose curve is not a complete grid."""
    by_day: dict[date, list[LoadCurvePoint]] = defaultdict(list)
    for point in points:
        by_day[point.day].append(point)
    return {day for day, day_points in by_day.items() if intervals(day, day_points) is None}


def hour_of(moment: datetime) -> datetime:
    """The UTC hour a moment falls in (Paris offsets are whole hours)."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def day_hours(day: date) -> list[datetime]:
    """Every hour of a Paris civil day, as UTC instants."""
    hours = []
    hour, end = day_start(day), day_end(day)
    while hour < end:
        hours.append(hour)
        hour += ONE_HOUR
    return hours


def hourly_energy(
    day: date, points: Sequence[LoadCurvePoint]
) -> list[tuple[datetime, float]] | None:
    """Wh per hour of `day` if its curve is a complete regular grid, else None."""
    day_intervals = intervals(day, points)
    if day_intervals is None:
        return None
    energy: dict[datetime, float] = defaultdict(float)
    for interval in day_intervals:
        energy[hour_of(interval.start)] += interval.wh
    return sorted(energy.items())
