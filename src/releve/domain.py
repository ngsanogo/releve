"""Domain vocabulary: small, frozen value objects with no dependencies.

The gateway speaks stringly-typed JSON; the rest of the package speaks these
types. Parsing happens once, in `gateway.py`, so everything downstream can
trust its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
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


class CustomerResource(StrEnum):
    """Contract and customer data cached per usage point, refreshed every few days."""

    CONTRACT = "contract"
    IDENTITY = "identity"
    CONTACT = "contact"
    ADDRESSES = "addresses"


class CacheResource(StrEnum):
    """A MyElectricalData remote-cache resource that can be deleted."""

    ALL = "all"
    CONTRACTS = "contracts"
    IDENTITY = "identity"
    CONTACT = "contact"
    ADDRESSES = "addresses"
    DAILY_CONSUMPTION = "daily_consumption"
    DAILY_PRODUCTION = "daily_production"
    MAX_POWER = "daily_consumption_max_power"
    CURVE_CONSUMPTION = "consumption_load_curve"
    CURVE_PRODUCTION = "production_load_curve"

    @property
    def needs_dates(self) -> bool:
        return self in (
            CacheResource.DAILY_CONSUMPTION,
            CacheResource.DAILY_PRODUCTION,
            CacheResource.MAX_POWER,
            CacheResource.CURVE_CONSUMPTION,
            CacheResource.CURVE_PRODUCTION,
        )


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


@dataclass(frozen=True, slots=True)
class EcowattHour:
    """RTE Ecowatt signal for the hour starting at `at`."""

    at: datetime
    level: int

    def __post_init__(self) -> None:
        require_aware(self.at, "EcowattHour.at")


@dataclass(frozen=True, slots=True)
class EcowattForecast:
    """Daily and hourly Ecowatt signals from one gateway answer."""

    days: tuple[EcowattDay, ...]
    hours: tuple[EcowattHour, ...]


class Period(StrEnum):
    """A time-of-use tariff period."""

    PEAK = "peak"
    OFFPEAK = "offpeak"


class TempoColor(StrEnum):
    BLUE = "BLUE"
    WHITE = "WHITE"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class TempoSeason:
    """Days left in the current Tempo season, per color."""

    days_left: dict[TempoColor, int]


@dataclass(frozen=True, slots=True)
class TempoPrice:
    """The current Tempo price of one color and period, in euros per kWh."""

    color: TempoColor
    period: Period
    euros_per_kwh: Decimal


@dataclass(frozen=True, slots=True)
class Consent:
    """What the gateway says about a usage point's consent and quota."""

    usage_point: str
    valid: bool
    expires_at: datetime | None
    call_number: int | None
    quota_limit: int | None
    quota_reached: bool
    quota_reset_at: datetime | None
    last_call_at: datetime | None
    banned: bool
    information: str

    @property
    def granted(self) -> bool:
        """Whether the gateway lets releve read this usage point's data."""
        return self.valid and not self.banned


@dataclass(frozen=True, slots=True)
class Contract:
    """The distribution contract of a usage point, as Enedis publishes it."""

    usage_point: str
    segment: str | None = None
    subscribed_power: str | None = None
    distribution_tariff: str | None = None
    offpeak_hours: str | None = None
    contract_status: str | None = None
    last_activation_date: str | None = None
    last_tariff_change_date: str | None = None
    meter_type: str | None = None
    usage_point_status: str | None = None


@dataclass(frozen=True, slots=True)
class Identity:
    """The account holder's identity, as Enedis publishes it."""

    usage_point: str
    customer_id: str | None = None
    title: str | None = None
    firstname: str | None = None
    lastname: str | None = None


@dataclass(frozen=True, slots=True)
class Contact:
    """The account holder's contact details, as Enedis publishes them."""

    usage_point: str
    customer_id: str | None = None
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Address:
    """The usage point's address, as Enedis publishes it."""

    usage_point: str
    customer_id: str | None = None
    street: str | None = None
    locality: str | None = None
    postal_code: str | None = None
    insee_code: str | None = None
    city: str | None = None
    country: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    altitude: str | None = None
    meter_type: str | None = None
    usage_point_status: str | None = None
