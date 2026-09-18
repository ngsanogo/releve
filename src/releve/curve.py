"""Load-curve arithmetic: when a day's curve can be trusted, and the energy it holds.

A day's curve is used only when it is a complete, regular grid: N points ending
exactly at start + k * step (k = 1..N), N * step equal to the day's length (23,
24 or 25 hours) and a step that divides an hour. Anything else would be a guess.

A curve is always judged one *series day* at a time: one usage point, one
direction, one Paris day. Points of different series never make a grid together.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple

from releve.clock import day_end, day_start
from releve.domain import Direction, LoadCurvePoint

ONE_HOUR = timedelta(hours=1)


class SeriesDay(NamedTuple):
    """One Paris day of one usage point's curve, in one direction."""

    usage_point: str
    direction: Direction
    day: date


@dataclass(frozen=True, slots=True)
class Interval:
    """One metering interval [start, end) and the energy consumed or produced in it."""

    start: datetime
    end: datetime
    wh: float


def split_days(points: Iterable[LoadCurvePoint]) -> dict[SeriesDay, list[LoadCurvePoint]]:
    """`points` grouped by series day, each day's points in time order."""
    days: dict[SeriesDay, list[LoadCurvePoint]] = defaultdict(list)
    for point in sorted(points, key=lambda point: point.end):
        days[SeriesDay(point.usage_point, point.direction, point.day)].append(point)
    return dict(days)


def grid_step(day: date, points: Sequence[LoadCurvePoint]) -> timedelta | None:
    """The metering step of one series day, or None when its curve is not a complete grid."""
    if not points:
        return None
    start, end = day_start(day), day_end(day)
    step = (end - start) / len(points)
    if step * len(points) != end - start or ONE_HOUR % step:
        return None
    ends = sorted(point.end for point in points)
    if any(moment != start + step * k for k, moment in enumerate(ends, start=1)):
        return None
    return step


def intervals(day: date, points: Sequence[LoadCurvePoint]) -> list[Interval] | None:
    """The intervals of one series day in time order, or None when its curve is not a grid."""
    step = grid_step(day, points)
    if step is None:
        return None
    hours = step / ONE_HOUR
    ordered = sorted(points, key=lambda point: point.end)
    return [Interval(point.end - step, point.end, point.watts * hours) for point in ordered]


def incomplete_days(points: Iterable[LoadCurvePoint]) -> set[date]:
    """Days on which the curve of a series among `points` is not a complete grid."""
    return {
        key.day
        for key, day_points in split_days(points).items()
        if grid_step(key.day, day_points) is None
    }


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
    """Wh per hour of one series day if its curve is a complete grid, else None."""
    day_intervals = intervals(day, points)
    if day_intervals is None:
        return None
    energy: dict[datetime, float] = defaultdict(float)
    for interval in day_intervals:
        energy[hour_of(interval.start)] += interval.wh
    return sorted(energy.items())
