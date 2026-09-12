"""Time vocabulary.

Enedis counts civil days in Europe/Paris; everything else runs on UTC. Every
datetime in this package is timezone-aware — a naive datetime is a bug, and the
domain types refuse them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def paris_today(now: datetime) -> date:
    """The Paris civil day `now` falls in."""
    return now.astimezone(PARIS).date()


def day_start(day: date) -> datetime:
    """The instant a Paris civil day begins, in UTC.

    Midnight always exists exactly once in Paris (DST switches happen at 02:00
    and 03:00), so this is never ambiguous.
    """
    return datetime.combine(day, time(), tzinfo=PARIS).astimezone(UTC)


def day_end(day: date) -> datetime:
    """The instant a Paris civil day ends (the next day's start), in UTC."""
    return day_start(day + timedelta(days=1))


def at_paris_hour(day: date, hour: int) -> datetime:
    """The instant a given Paris wall-clock hour of `day` begins, in UTC."""
    return datetime.combine(day, time(hour), tzinfo=PARIS).astimezone(UTC)


def utc_midnight(now: datetime) -> datetime:
    """Start of the UTC day `now` falls in — when the gateway resets its quota."""
    return datetime.combine(now.astimezone(UTC).date(), time(), tzinfo=UTC)


def next_utc_midnight(now: datetime) -> datetime:
    return utc_midnight(now) + timedelta(days=1)


def format_paris(moment: datetime | None) -> str:
    """An instant as people read it here: Paris wall-clock time, to the minute."""
    return moment.astimezone(PARIS).strftime("%Y-%m-%d %H:%M") if moment is not None else "—"


def require_aware(moment: datetime, name: str) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {moment!r}")


def to_unix(moment: datetime) -> int:
    """Unix seconds of an aware datetime (sub-second precision is dropped)."""
    require_aware(moment, "moment")
    return int(moment.timestamp())


def from_unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
