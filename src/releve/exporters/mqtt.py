"""MQTT exporter, with Home Assistant discovery.

Retained topics:

    {base_topic}/{pdl}/state                                  one usage point, JSON
    {base_topic}/rte/state                                    today's Tempo and Ecowatt, JSON
    {discovery_prefix}/sensor/releve_{pdl}/{key}/config  discovery
    {discovery_prefix}/sensor/releve_rte/{key}/config    discovery

A value the cache cannot state truthfully — yesterday not published yet, a
week with a missing day — is null (Home Assistant shows "unknown"), never a
misleading zero. Discovery follows the configuration: a sensor whose dataset
is disabled gets an empty retained config, which removes it. Topics, JSON keys
and unique ids are stable, so entities survive upgrades.

The connection lives only for a delivery: connect, publish, wait for every
acknowledgement, disconnect.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.reasoncodes import ReasonCode

from releve.clock import Clock, paris_today, utc_now
from releve.config import MqttSettings, UsagePointSettings
from releve.domain import Dataset, Direction
from releve.errors import ExportError
from releve.store import Store

TIMEOUT_SECONDS = 10.0

Message = tuple[str, str]
Deliver = Callable[[MqttSettings, Sequence[Message]], None]


@dataclass(frozen=True, slots=True)
class Sensor:
    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None

    def config(self, unique_prefix: str, state_topic: str, device: dict[str, Any]) -> str:
        fields: dict[str, Any] = {
            "unique_id": f"{unique_prefix}_{self.key}",
            "name": self.name,
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{self.value_key} }}}}",
            "device": device,
            "unit_of_measurement": self.unit,
            "device_class": self.device_class,
            "state_class": self.state_class,
            "icon": self.icon,
        }
        return json.dumps({k: v for k, v in fields.items() if v is not None}, sort_keys=True)

    @property
    def value_key(self) -> str:
        suffix = {"kWh": "_kwh", "VA": "_va"}.get(self.unit or "", "")
        return f"{self.key}{suffix}"


# Rolling totals are not meter readings: no state_class, so Home Assistant does
# not build long-term statistics from them (the home_assistant exporter does).
_ENERGY = {"unit": "kWh", "device_class": "energy"}
CONSUMPTION_SENSORS = (
    Sensor("energy_yesterday", "Energy yesterday", **_ENERGY),
    Sensor("energy_last_7_days", "Energy last 7 days", **_ENERGY),
    Sensor("energy_last_30_days", "Energy last 30 days", **_ENERGY),
)
MAX_POWER_SENSORS = (
    Sensor(
        "max_power_yesterday",
        "Max power yesterday",
        unit="VA",
        device_class="apparent_power",
        state_class="measurement",
    ),
)
PRODUCTION_SENSORS = (
    Sensor("production_yesterday", "Production yesterday", **_ENERGY),
    Sensor("production_last_7_days", "Production last 7 days", **_ENERGY),
    Sensor("production_last_30_days", "Production last 30 days", **_ENERGY),
)
RTE_SENSORS = (
    Sensor("tempo_today", "Tempo today", icon="mdi:calendar-today"),
    Sensor("ecowatt_today", "Ecowatt today", icon="mdi:lightning-bolt"),
)


class MqttExporter:
    name = "mqtt"

    def __init__(
        self,
        settings: MqttSettings,
        usage_points: Sequence[UsagePointSettings],
        *,
        rte: bool,
        clock: Clock = utc_now,
        deliver: Deliver | None = None,
    ) -> None:
        self._settings = settings
        self._usage_points = usage_points
        self._rte = rte
        self._clock = clock
        self._deliver = deliver or deliver_with_paho
        self.sink = f"mqtt {settings.host}:{settings.port}"

    def export(self, store: Store, *, after_run: int, up_to_run: int) -> str:
        del after_run, up_to_run  # the state is recomputed from the cache on every pass
        messages = self.messages(store, paris_today(self._clock()))
        self._deliver(self._settings, messages)
        return f"{len(messages)} retained messages published"

    def messages(self, store: Store, today: date) -> list[Message]:
        base = self._settings.base_topic
        messages: list[Message] = []
        for up in self._usage_points:
            state_topic = f"{base}/{up.id}/state"
            if self._settings.home_assistant_discovery:
                device = {
                    "identifiers": [f"releve_{up.id}"],
                    "name": f"Electricity meter {up.id}",
                    "model": "Electricity meter",
                }
                wanted = (
                    (CONSUMPTION_SENSORS, up.consumption),
                    (MAX_POWER_SENSORS, up.max_power),
                    (PRODUCTION_SENSORS, up.production),
                )
                for sensors, enabled in wanted:
                    messages.extend(
                        self._discovery(f"releve_{up.id}", sensors, enabled, state_topic, device)
                    )
            messages.append((state_topic, json.dumps(state(store, up, today), sort_keys=True)))
        if self._rte:
            state_topic = f"{base}/rte/state"
            if self._settings.home_assistant_discovery:
                device = {
                    "identifiers": ["releve_rte"],
                    "name": "Grid signals",
                    "model": "Tempo and Ecowatt",
                }
                messages.extend(
                    self._discovery("releve_rte", RTE_SENSORS, True, state_topic, device)
                )
            messages.append((state_topic, json.dumps(rte_state(store, today), sort_keys=True)))
        return messages

    def _discovery(
        self,
        unique_prefix: str,
        sensors: Sequence[Sensor],
        enabled: bool,
        state_topic: str,
        device: dict[str, Any],
    ) -> list[Message]:
        prefix = self._settings.discovery_prefix
        return [
            (
                f"{prefix}/sensor/{unique_prefix}/{sensor.key}/config",
                sensor.config(unique_prefix, state_topic, device) if enabled else "",
            )
            for sensor in sensors
        ]


def state(store: Store, up: UsagePointSettings, today: date) -> dict[str, Any]:
    """The JSON state of one usage point."""
    payload: dict[str, Any] = {}
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
    return {
        "day": today.isoformat(),
        "tempo_today": tempo[0].color if tempo else None,
        "ecowatt_today": ecowatt[0].level if ecowatt else None,
        "ecowatt_message": ecowatt[0].message if ecowatt else None,
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


def deliver_with_paho(settings: MqttSettings, messages: Sequence[Message]) -> None:
    """Publish retained QoS 1 messages and wait until the broker acknowledged each."""
    client = paho.Client(CallbackAPIVersion.VERSION2)
    if settings.username:
        client.username_pw_set(settings.username, settings.password.get_secret_value() or None)
    if settings.tls:
        client.tls_set(ca_certs=str(settings.tls_ca_certs) if settings.tls_ca_certs else None)
    connected = threading.Event()
    refusals: list[str] = []

    def on_connect(
        client: paho.Client,
        userdata: Any,
        flags: paho.ConnectFlags,
        reason_code: ReasonCode,
        properties: Any,
    ) -> None:
        del client, userdata, flags, properties
        if reason_code.is_failure:
            refusals.append(str(reason_code))
        connected.set()

    client.on_connect = on_connect
    destination = f"{settings.host}:{settings.port}"
    try:
        client.connect(settings.host, settings.port)
        client.loop_start()
        if not connected.wait(TIMEOUT_SECONDS):
            raise ExportError(f"MQTT broker {destination} did not answer within {TIMEOUT_SECONDS}s")
        if refusals:
            raise ExportError(f"MQTT broker {destination} refused the connection: {refusals[0]}")
        pending = [
            (topic, client.publish(topic, payload, qos=1, retain=True))
            for topic, payload in messages
        ]
        for topic, info in pending:
            info.wait_for_publish(timeout=TIMEOUT_SECONDS)
            if not info.is_published():
                raise ExportError(f"MQTT broker {destination} did not acknowledge {topic}")
    except (OSError, ValueError, RuntimeError) as exc:
        raise ExportError(f"MQTT delivery to {destination} failed: {exc}") from exc
    finally:
        client.disconnect()
        client.loop_stop()
