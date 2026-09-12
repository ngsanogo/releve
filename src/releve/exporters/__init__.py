"""Exporters: deliver the cache to the outside world.

An exporter reads the store — never the gateway — and is told which runs'
changes it has not delivered yet: `after_run < run_id <= up_to_run`. It
returns a one-line summary or raises `ExportError`; the pass advances its
cursor only on success, so whatever failed is delivered next time.

`sink` identifies WHERE an exporter delivers: change the destination in the
configuration and the new sink starts from scratch, with a full delivery.
"""

from __future__ import annotations

from typing import Protocol

from releve.clock import Clock, utc_now
from releve.config import Settings
from releve.exporters.home_assistant import HomeAssistantExporter
from releve.exporters.influxdb import InfluxExporter
from releve.exporters.mqtt import MqttExporter
from releve.store import Store


class Exporter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def sink(self) -> str: ...

    def export(self, store: Store, *, after_run: int, up_to_run: int) -> str: ...


def build_exporters(settings: Settings, clock: Clock = utc_now) -> list[Exporter]:
    configured = settings.exporters
    exporters: list[Exporter] = []
    if configured.mqtt.enabled:
        exporters.append(
            MqttExporter(
                configured.mqtt, settings.usage_points, rte=settings.sync.rte_signals, clock=clock
            )
        )
    if configured.home_assistant.enabled:
        exporters.append(HomeAssistantExporter(configured.home_assistant, settings.usage_points))
    if configured.influxdb.enabled:
        exporters.append(InfluxExporter(configured.influxdb))
    return exporters
