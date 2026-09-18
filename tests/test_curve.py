"""Load-curve arithmetic: what a complete grid is, one series day at a time."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from releve.clock import day_start
from releve.curve import SeriesDay, grid_step, hourly_energy, incomplete_days, split_days
from releve.domain import Direction, LoadCurvePoint
from tests.conftest import OTHER_PDL, PDL
from tests.fakes import curve_of

C = Direction.CONSUMPTION
P = Direction.PRODUCTION
DAY = date(2026, 9, 10)


@pytest.mark.parametrize(
    ("day", "hours"), [(DAY, 24), (date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_a_complete_curve_gives_one_value_per_hour_of_the_day(day: date, hours: int) -> None:
    points = curve_of(PDL, C, day)
    energy = hourly_energy(day, points)
    assert energy is not None
    assert len(energy) == hours
    assert energy[0][0] == day_start(day)
    assert sum(wh for _, wh in energy) == sum(p.watts * 0.5 for p in points)


def test_an_incomplete_or_irregular_curve_is_not_guessed() -> None:
    points = curve_of(PDL, C, DAY)
    assert hourly_energy(DAY, []) is None
    assert hourly_energy(DAY, points[:-1]) is None  # a point missing
    shifted = [*points[:-1], LoadCurvePoint(PDL, C, points[-1].end - timedelta(minutes=10), 1)]
    assert hourly_energy(DAY, shifted) is None  # right count, wrong grid
    hourly = [
        LoadCurvePoint(PDL, C, day_start(DAY) + timedelta(hours=h), 1000) for h in range(1, 25)
    ]
    assert hourly_energy(DAY, hourly) == [
        (day_start(DAY) + timedelta(hours=h), 1000.0) for h in range(24)
    ]
    two = [LoadCurvePoint(PDL, C, day_start(DAY) + timedelta(hours=12 * k), 1) for k in (1, 2)]
    assert hourly_energy(DAY, two) is None  # a 12-hour step does not divide an hour


def test_the_grid_step_is_the_metering_interval_of_a_complete_day() -> None:
    half_hours = curve_of(PDL, C, DAY)
    assert grid_step(DAY, half_hours) == timedelta(minutes=30)
    assert grid_step(DAY, list(reversed(half_hours))) == timedelta(minutes=30)
    assert grid_step(DAY, half_hours[1::2]) == timedelta(hours=1)
    assert grid_step(DAY, half_hours[:-1]) is None
    assert grid_step(DAY + timedelta(days=1), half_hours) is None  # another day's grid


def test_points_are_split_by_usage_point_direction_and_day() -> None:
    today, tomorrow = curve_of(PDL, C, DAY), curve_of(PDL, C, DAY + timedelta(days=1))
    production, other = curve_of(PDL, P, DAY), curve_of(OTHER_PDL, C, DAY)

    days = split_days([*reversed(tomorrow), *other, *production, *today])

    assert days == {
        SeriesDay(PDL, C, DAY): today,
        SeriesDay(PDL, C, DAY + timedelta(days=1)): tomorrow,
        SeriesDay(PDL, P, DAY): production,
        SeriesDay(OTHER_PDL, C, DAY): other,
    }


def test_incomplete_days_names_only_the_days_whose_curve_is_not_a_grid() -> None:
    complete = date(2026, 9, 10)
    partial = date(2026, 9, 11)
    points = curve_of(PDL, C, complete) + curve_of(PDL, C, partial)[:24]
    assert incomplete_days(points) == {partial}
    assert incomplete_days(curve_of(PDL, C, complete)) == set()
    assert incomplete_days([]) == set()


def test_halves_of_different_series_never_make_a_complete_day() -> None:
    halves = [*curve_of(PDL, C, DAY)[:24], *curve_of(PDL, P, DAY)[24:]]
    assert incomplete_days(halves) == {DAY}
    mixed_meters = [*curve_of(PDL, C, DAY)[:24], *curve_of(OTHER_PDL, C, DAY)[24:]]
    assert incomplete_days(mixed_meters) == {DAY}
