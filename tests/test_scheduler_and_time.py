"""The scheduler loop, the time vocabulary and the domain's guards."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from releve.clock import (
    day_end,
    day_start,
    format_paris,
    next_utc_midnight,
    paris_today,
    to_unix,
)
from releve.domain import Direction, LoadCurvePoint, PowerPeak
from releve.scheduler import Scheduler
from tests.conftest import PDL, FrozenClock


def test_the_loop_runs_repeatedly_and_survives_a_crash(caplog: pytest.LogCaptureFixture) -> None:
    runs = 0
    done = threading.Event()

    def job() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("bug in a pass")
        if runs == 3:
            done.set()

    scheduler = Scheduler(job, timedelta(milliseconds=10), first_delay=timedelta(0))
    scheduler.start()
    assert done.wait(5)
    scheduler.stop()
    assert "sync pass crashed" in caplog.text
    assert scheduler.last_pass_finished is not None


def test_health_means_alive_and_not_overdue(clock: FrozenClock) -> None:
    scheduler = Scheduler(
        lambda: None, timedelta(hours=1), first_delay=timedelta(hours=1), clock=clock
    )
    assert not scheduler.is_healthy()
    scheduler.start()
    try:
        clock.advance(timedelta(hours=3, minutes=10))
        assert scheduler.is_healthy()
        clock.advance(timedelta(minutes=10))
        assert not scheduler.is_healthy()
    finally:
        scheduler.stop()
    assert not scheduler.is_healthy()


def test_paris_days_have_23_24_or_25_hours() -> None:
    assert day_end(date(2026, 3, 29)) - day_start(date(2026, 3, 29)) == timedelta(hours=23)
    assert day_end(date(2026, 10, 25)) - day_start(date(2026, 10, 25)) == timedelta(hours=25)
    assert day_start(date(2026, 9, 12)) == datetime(2026, 9, 11, 22, tzinfo=UTC)


def test_the_paris_day_and_the_utc_day_differ_around_midnight() -> None:
    late_utc = datetime(2026, 9, 11, 23, 30, tzinfo=UTC)
    assert paris_today(late_utc) == date(2026, 9, 12)
    assert next_utc_midnight(late_utc) == datetime(2026, 9, 12, tzinfo=UTC)
    assert format_paris(late_utc) == "2026-09-12 01:30"
    assert format_paris(None) == "—"


def test_naive_datetimes_are_refused() -> None:
    naive = datetime(2026, 9, 12, 10, 0)  # noqa: DTZ001 — the point of the test
    with pytest.raises(ValueError, match="timezone-aware"):
        to_unix(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        LoadCurvePoint(PDL, Direction.CONSUMPTION, naive, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        PowerPeak(PDL, date(2026, 9, 12), 1, naive)


def test_a_curve_point_ending_at_midnight_belongs_to_the_day_before() -> None:
    point = LoadCurvePoint(PDL, Direction.CONSUMPTION, day_start(date(2026, 9, 12)), 1)
    assert point.day == date(2026, 9, 11)


def test_a_crashing_pass_is_unhealthy_until_one_succeeds() -> None:
    outcomes = iter([RuntimeError("disk full"), None])
    first_done, second_done = threading.Event(), threading.Event()

    def job() -> None:
        outcome = next(outcomes, None)
        if isinstance(outcome, Exception):
            first_done.set()
            raise outcome
        second_done.set()

    scheduler = Scheduler(job, timedelta(hours=1), first_delay=timedelta(0))
    scheduler.last_pass_crashed = False
    scheduler.start()
    try:
        assert first_done.wait(5)
    finally:
        scheduler.stop()
    assert scheduler.last_pass_crashed
    assert not scheduler.is_healthy()
