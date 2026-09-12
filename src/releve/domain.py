"""Domain vocabulary: small, frozen value objects with no dependencies.

The gateway speaks stringly-typed JSON; the rest of the package speaks these
types. Parsing happens once, in `gateway.py`, so everything downstream can
trust its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from releve.clock import PARIS, require_aware


class Direction(StrEnum):
    """Which way energy flows through the meter."""

    CONSUMPTION = "consumption"
    PRODUCTION = "production"


class Dataset(StrEnum):
    """A kind of metering data cached per usage point, in the order it is fetched.

    Cheap datasets come first and load curves last: Enedis throttles curves in
    practice, and a throttle must never starve the daily data.
    """

    DAILY_CONSUMPTION = "daily_consumption"
    DAILY_PRODUCTION = "daily_production"
    MAX_POWER = "max_power"
    CURVE_CONSUMPTION = "curve_consumption"
    CURVE_PRODUCTION = "curve_production"

    @property
    def window_days(self) -> int:
        """The widest date range one gateway call may ask for."""
        return 7 if self.is_curve else 365

    @property
    def max_history_days(self) -> int:
        """How far back Enedis keeps this dataset, with a day of margin."""
        return 729 if self.is_curve else 1094

    @property
    def is_curve(self) -> bool:
        return self in (Dataset.CURVE_CONSUMPTION, Dataset.CURVE_PRODUCTION)


@dataclass(frozen=True, slots=True)
class DailyEnergy:
    """Energy of one Paris civil day, in watt-hours, as published by Enedis."""

    usage_point: str
    direction: Direction
    day: date
    wh: int


@dataclass(frozen=True, slots=True)
class LoadCurvePoint:
    """Mean power over one metering interval, stamped at the interval's END."""

    usage_point: str
    direction: Direction
    end: datetime
    watts: int

    def __post_init__(self) -> None:
        require_aware(self.end, "LoadCurvePoint.end")

    @property
    def day(self) -> date:
        """The Paris civil day the interval belongs to (a midnight END closes the previous day)."""
        return (self.end - timedelta(microseconds=1)).astimezone(PARIS).date()


@dataclass(frozen=True, slots=True)
class PowerPeak:
    """Highest apparent power reached during one Paris civil day."""

    usage_point: str
    day: date
    va: int
    at: datetime

    def __post_init__(self) -> None:
        require_aware(self.at, "PowerPeak.at")


@dataclass(frozen=True, slots=True)
class TempoDay:
    """EDF Tempo color of one day, upper-case as published (BLUE, WHITE, RED)."""

    day: date
    color: str


@dataclass(frozen=True, slots=True)
class EcowattDay:
    """RTE Ecowatt signal of one day."""

    day: date
    level: int
    message: str
