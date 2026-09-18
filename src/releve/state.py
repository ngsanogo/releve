"""The published state of a usage point and of the grid signals.

One definition, two readers: the MQTT exporter publishes it as retained JSON,
the web API serves it (and the Home Assistant integration in custom_components/
reads it there). A value the cache cannot state truthfully — yesterday not
published yet, a week with a missing day — is None, never a misleading zero.
Keys are stable: entities built on them survive upgrades.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from releve.config import UsagePointSettings
from releve.domain import Dataset, Direction, Period, TempoColor
from releve.store import Store
from releve.tariffs import OffpeakHours, daily_energy_by_period


def state(store: Store, up: UsagePointSettings, today: date) -> dict[str, Any]:
    """The JSON state of one usage point."""
    payload: dict[str, Any] = {}
    if consent := store.consent(up.id):
        payload |= {
            "consent_valid": consent.valid,
            "consent_banned": consent.banned,
            "consent_expires_at": consent.expires_at.isoformat() if consent.expires_at else None,
            "quota_reached": consent.quota_reached,
            "quota_limit": consent.quota_limit,
            "call_number": consent.call_number,
        }
    if contract := store.contract(up.id):
        payload |= {
            "subscribed_power": contract.subscribed_power,
            "distribution_tariff": contract.distribution_tariff,
            "offpeak_hours": contract.offpeak_hours,
            "contract_status": contract.contract_status,
            "meter_type": contract.meter_type,
        }
        if up.consumption_detail and contract.offpeak_hours:
            payload |= _period_totals(store, up.id, today, contract.offpeak_hours)
    if up.consumption:
        by_day = _daily_by_day(store, up.id, Direction.CONSUMPTION, today)
        payload |= {
            "energy_yesterday_kwh": _total_kwh(by_day, today, 1),
            "energy_last_7_days_kwh": _total_kwh(by_day, today, 7),
            "energy_last_30_days_kwh": _total_kwh(by_day, today, 30),
            "latest_day": _iso(store.latest_day(up.id, Dataset.DAILY_CONSUMPTION)),
        }
    if up.max_power:
        peaks = store.peaks(up.id, today - timedelta(days=1), today)
        payload["max_power_yesterday_va"] = peaks[0].va if peaks else None
    if up.production:
        by_day = _daily_by_day(store, up.id, Direction.PRODUCTION, today)
        payload |= {
            "production_yesterday_kwh": _total_kwh(by_day, today, 1),
            "production_last_7_days_kwh": _total_kwh(by_day, today, 7),
            "production_last_30_days_kwh": _total_kwh(by_day, today, 30),
            "latest_production_day": _iso(store.latest_day(up.id, Dataset.DAILY_PRODUCTION)),
        }
    return payload


def rte_state(store: Store, today: date) -> dict[str, Any]:
    tomorrow = today + timedelta(days=1)
    tempo = store.tempo(today, tomorrow)
    ecowatt = store.ecowatt(today, tomorrow)
    season = store.tempo_season()
    prices = {
        f"{price.color.value.lower()}_{price.period.value}": float(price.euros_per_kwh)
        for price in store.tempo_prices()
    }
    return {
        "day": today.isoformat(),
        "tempo_today": tempo[0].color if tempo else None,
        "ecowatt_today": ecowatt[0].level if ecowatt else None,
        "ecowatt_message": ecowatt[0].message if ecowatt else None,
        "tempo_days_left_blue": season.days_left.get(TempoColor.BLUE) if season else None,
        "tempo_days_left_white": season.days_left.get(TempoColor.WHITE) if season else None,
        "tempo_days_left_red": season.days_left.get(TempoColor.RED) if season else None,
        "tempo_prices": prices or None,
    }


def _period_totals(
    store: Store, pdl: str, today: date, offpeak_hours: str
) -> dict[str, float | None]:
    offpeak = OffpeakHours.parse(offpeak_hours)
    yesterday = today - timedelta(days=1)
    if offpeak is None:
        return {"energy_yesterday_peak_kwh": None, "energy_yesterday_offpeak_kwh": None}
    points = store.curve(pdl, Direction.CONSUMPTION, yesterday, today)
    totals = daily_energy_by_period(yesterday, points, offpeak)
    if totals is None:
        return {"energy_yesterday_peak_kwh": None, "energy_yesterday_offpeak_kwh": None}
    return {
        "energy_yesterday_peak_kwh": round(totals[Period.PEAK] / 1000, 3),
        "energy_yesterday_offpeak_kwh": round(totals[Period.OFFPEAK] / 1000, 3),
    }


def _daily_by_day(store: Store, pdl: str, direction: Direction, today: date) -> dict[date, int]:
    readings = store.daily(pdl, direction, today - timedelta(days=30), today)
    return {reading.day: reading.wh for reading in readings}


def _total_kwh(by_day: dict[date, int], today: date, days: int) -> float | None:
    """Energy of the `days` days before today, or None when any of them is missing."""
    window = [today - timedelta(days=offset) for offset in range(1, days + 1)]
    if any(day not in by_day for day in window):
        return None
    return round(sum(by_day[day] for day in window) / 1000, 3)


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None
