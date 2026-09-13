"""Tariff periods: off-peak hours from the contract, energy per period.

* Off-peak hours come from the contract, as Enedis publishes them, e.g.
  "HC (22H00-6H00)" or "HC (1H30-7H30;12H30-14H30)". A metering interval belongs
  to the period its START falls in, in Paris wall-clock time.
* Energy is split only for days whose load curve is a complete grid
  (`curve.intervals`) — a split built on missing intervals would be wrong.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from releve.clock import PARIS
from releve.curve import intervals
from releve.domain import LoadCurvePoint, Period

_RANGE = re.compile(r"(\d{1,2})H(\d{2})\s*-\s*(\d{1,2})H(\d{2})")


@dataclass(frozen=True, slots=True)
class OffpeakHours:
    """Daily off-peak ranges [start, end) in Paris wall-clock time; a range may cross midnight."""

    ranges: tuple[tuple[time, time], ...]

    @classmethod
    def parse(cls, text: str | None) -> OffpeakHours | None:
        """Read Enedis' notation; None when the text holds no usable range."""
        ranges = []
        for start_h, start_m, end_h, end_m in _RANGE.findall(text or ""):
            try:
                start = time(int(start_h) % 24, int(start_m))
                end = time(int(end_h) % 24, int(end_m))
            except ValueError:
                return None
            if start == end:
                return None
            ranges.append((start, end))
        return cls(tuple(ranges)) if ranges else None

    def contains(self, moment: time) -> bool:
        for start, end in self.ranges:
            if start < end and start <= moment < end:
                return True
            if start > end and (moment >= start or moment < end):
                return True
        return False

    def __str__(self) -> str:
        return ", ".join(f"{start:%H:%M}-{end:%H:%M}" for start, end in self.ranges)


def period_at(moment: datetime, offpeak: OffpeakHours) -> Period:
    local = moment.astimezone(PARIS).time().replace(tzinfo=None)
    return Period.OFFPEAK if offpeak.contains(local) else Period.PEAK


def daily_energy_by_period(
    day: date, points: Sequence[LoadCurvePoint], offpeak: OffpeakHours
) -> dict[Period, float] | None:
    """Wh per period over the whole day, or None when the curve is not a complete grid."""
    day_intervals = intervals(day, points)
    if day_intervals is None:
        return None
    totals = dict.fromkeys(Period, 0.0)
    for interval in day_intervals:
        totals[period_at(interval.start, offpeak)] += interval.wh
    return totals
