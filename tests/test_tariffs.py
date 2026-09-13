"""Off-peak hours parsing and energy splits."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from releve.clock import PARIS, day_start
from releve.domain import Direction, LoadCurvePoint, Period
from releve.tariffs import OffpeakHours, daily_energy_by_period, period_at


def test_offpeak_hours_parse_overnight_and_split_ranges() -> None:
    overnight = OffpeakHours.parse("HC (22H00-6H00)")
    assert overnight is not None
    assert overnight.contains(
        datetime(2026, 9, 12, 23, 0, tzinfo=PARIS).time().replace(tzinfo=None)
    )
    assert overnight.contains(
        datetime(2026, 9, 12, 5, 59, tzinfo=PARIS).time().replace(tzinfo=None)
    )
    assert not overnight.contains(
        datetime(2026, 9, 12, 12, 0, tzinfo=PARIS).time().replace(tzinfo=None)
    )

    split = OffpeakHours.parse("HC (0H50-6H50;14H20-16H20)")
    assert split is not None
    assert len(split.ranges) == 2


def test_daily_energy_by_period_needs_a_complete_curve() -> None:
    day = date(2026, 9, 10)
    offpeak = OffpeakHours.parse("HC (22H00-6H00)")
    assert offpeak is not None
    step = timedelta(minutes=30)
    start = day_start(day)
    points = [
        LoadCurvePoint("01234567890123", Direction.CONSUMPTION, start + step * k, 1000)
        for k in range(1, 49)
    ]
    # 1000 W for 8 off-peak hours and 16 peak hours.
    assert daily_energy_by_period(day, points, offpeak) == {
        Period.PEAK: 16000.0,
        Period.OFFPEAK: 8000.0,
    }
    assert period_at(start + timedelta(hours=23), offpeak) is Period.OFFPEAK
    assert daily_energy_by_period(day, points[:-1], offpeak) is None
