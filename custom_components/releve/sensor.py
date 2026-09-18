"""Sensors: what releve's cache states about each meter, and the grid signals.

An entity exists when releve publishes its key — the keys follow releve's
configuration (a dataset turned off there has no key, hence no entity). A value
releve cannot state truthfully is null in its API and "unknown" here, never a
misleading zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfApparentPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import JsonObject
from .const import DOMAIN
from .coordinator import ReleveConfigEntry, ReleveCoordinator

PARALLEL_UPDATES = 0  # one coordinator does all the reading

type Value = StateType | date | datetime


def _same(raw: Any) -> Value:
    return raw  # type: ignore[no-any-return]  # JSON scalars are StateType


def _date(raw: Any) -> Value:
    return date.fromisoformat(raw)


def _timestamp(raw: Any) -> Value:
    return dt_util.parse_datetime(raw)


def _tempo(raw: Any) -> Value:
    return raw.lower() if raw in {"BLUE", "WHITE", "RED"} else None


def _ecowatt(raw: Any) -> Value:
    return {1: "green", 2: "orange", 3: "red"}.get(raw)


@dataclass(frozen=True, kw_only=True)
class ReleveSensorDescription(SensorEntityDescription):
    """`key` is the JSON key releve publishes; `source` says in which document."""

    source: Literal["state", "info"] = "state"
    value: Callable[[Any], Value] = _same


def _energy(key: str, translation_key: str) -> ReleveSensorDescription:
    # Rolling totals, not meter readings: no state_class, so Home Assistant builds
    # no long-term statistics from them — releve's statistics exporter does that.
    return ReleveSensorDescription(
        key=key,
        translation_key=translation_key,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    )


def _diagnostic(key: str, **kwargs: Any) -> ReleveSensorDescription:
    return ReleveSensorDescription(
        key=key, translation_key=key, entity_category=EntityCategory.DIAGNOSTIC, **kwargs
    )


METER_SENSORS: tuple[ReleveSensorDescription, ...] = (
    _energy("energy_yesterday_kwh", "energy_yesterday"),
    _energy("energy_last_7_days_kwh", "energy_last_7_days"),
    _energy("energy_last_30_days_kwh", "energy_last_30_days"),
    _energy("energy_yesterday_peak_kwh", "energy_yesterday_peak"),
    _energy("energy_yesterday_offpeak_kwh", "energy_yesterday_offpeak"),
    _energy("production_yesterday_kwh", "production_yesterday"),
    _energy("production_last_7_days_kwh", "production_last_7_days"),
    _energy("production_last_30_days_kwh", "production_last_30_days"),
    ReleveSensorDescription(
        key="max_power_yesterday_va",
        translation_key="max_power_yesterday",
        device_class=SensorDeviceClass.APPARENT_POWER,
        native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _diagnostic("latest_day", device_class=SensorDeviceClass.DATE, value=_date),
    _diagnostic("latest_production_day", device_class=SensorDeviceClass.DATE, value=_date),
    _diagnostic(
        "last_success", source="info", device_class=SensorDeviceClass.TIMESTAMP, value=_timestamp
    ),
    _diagnostic("consent_expires_at", device_class=SensorDeviceClass.TIMESTAMP, value=_timestamp),
    _diagnostic("call_number"),
    _diagnostic("subscribed_power"),
    _diagnostic("distribution_tariff"),
    _diagnostic("offpeak_hours"),
)

GRID_SENSORS: tuple[ReleveSensorDescription, ...] = (
    ReleveSensorDescription(
        key="tempo_today",
        translation_key="tempo_today",
        device_class=SensorDeviceClass.ENUM,
        options=["blue", "white", "red"],
        value=_tempo,
    ),
    ReleveSensorDescription(
        key="ecowatt_today",
        translation_key="ecowatt_today",
        device_class=SensorDeviceClass.ENUM,
        options=["green", "orange", "red"],
        value=_ecowatt,
    ),
    *(
        ReleveSensorDescription(
            key=f"tempo_days_left_{color}", translation_key=f"tempo_days_left_{color}"
        )
        for color in ("blue", "white", "red")
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ReleveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    del hass
    coordinator = entry.runtime_data
    url = coordinator.client.url
    entities: list[ReleveSensor] = []
    for pdl, meter in coordinator.data.meters.items():
        device = DeviceInfo(
            identifiers={(DOMAIN, pdl)},
            name=meter.info.get("name") or f"Linky {pdl}",
            manufacturer="Enedis",
            model="Linky",
            serial_number=pdl,
            configuration_url=f"{url}/usage-points/{pdl}",
        )
        entities.extend(
            ReleveSensor(
                coordinator,
                description,
                pdl=pdl,
                device=device,
                unique_id=f"{pdl}_{description.key}",
            )
            for description in METER_SENSORS
            if description.key in (meter.info if description.source == "info" else meter.state)
        )
    if coordinator.data.grid is not None:
        device = DeviceInfo(
            identifiers={(DOMAIN, "grid")},  # device identifiers are scoped to the entry
            translation_key="grid",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=url,
        )
        entities.extend(
            ReleveSensor(
                coordinator,
                description,
                pdl=None,
                device=device,
                unique_id=f"{entry.entry_id}_grid_{description.key}",
            )
            for description in GRID_SENSORS
            if description.key in coordinator.data.grid
        )
    async_add_entities(entities)


class ReleveSensor(CoordinatorEntity[ReleveCoordinator], SensorEntity):
    """One key of a meter's state (pdl set) or of the grid state (pdl None)."""

    _attr_has_entity_name = True
    entity_description: ReleveSensorDescription

    def __init__(
        self,
        coordinator: ReleveCoordinator,
        description: ReleveSensorDescription,
        *,
        pdl: str | None,
        device: DeviceInfo,
        unique_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._pdl = pdl
        self._attr_device_info = device
        self._attr_unique_id = unique_id

    @property
    def _document(self) -> JsonObject | None:
        data = self.coordinator.data
        if self._pdl is None:
            return data.grid
        meter = data.meters.get(self._pdl)
        if meter is None:
            return None
        return meter.info if self.entity_description.source == "info" else meter.state

    @property
    def available(self) -> bool:
        return super().available and self._document is not None

    @property
    def native_value(self) -> Value:
        document = self._document
        raw = None if document is None else document.get(self.entity_description.key)
        return None if raw is None else self.entity_description.value(raw)
