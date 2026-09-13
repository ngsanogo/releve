"""MQTT: the messages themselves, and delivery through paho.

Delivery against a real broker runs when MQTT_TEST_BROKER=host:port is set (CI
starts one); everything else needs no network.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Sequence
from datetime import date, timedelta

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from releve.clock import at_paris_hour
from releve.config import MqttSettings, UsagePointSettings
from releve.domain import DailyEnergy, Direction, EcowattDay, PowerPeak, TempoDay
from releve.errors import ExportError
from releve.exporters.mqtt import Message, MqttExporter, deliver_with_paho
from releve.store import Store
from tests.conftest import NOW, PDL, FrozenClock

TODAY = date(2026, 9, 12)
YESTERDAY = TODAY - timedelta(days=1)


class Outbox:
    def __init__(self) -> None:
        self.messages: list[Message] = []

    def __call__(self, settings: MqttSettings, messages: Sequence[Message]) -> None:
        del settings
        self.messages.extend(messages)

    def payload(self, topic: str) -> str:
        return dict(self.messages)[topic]


def fill(store: Store, days: int) -> None:
    run = store.start_run(NOW)
    store.upsert_daily(
        run,
        [
            DailyEnergy(PDL, Direction.CONSUMPTION, TODAY - timedelta(days=n), 1000)
            for n in range(1, days + 1)
        ],
    )
    store.upsert_peaks(run, [PowerPeak(PDL, YESTERDAY, 6100, at_paris_hour(YESTERDAY, 19))])
    store.upsert_tempo([TempoDay(TODAY, "RED")])
    store.upsert_ecowatt([EcowattDay(TODAY, 3, "coupures possibles")])


def export(store: Store, clock: FrozenClock, *, rte: bool = True, **up: bool) -> Outbox:
    outbox = Outbox()
    usage_point = UsagePointSettings(id=PDL, **up)
    exporter = MqttExporter(
        MqttSettings(enabled=True), [usage_point], rte=rte, clock=clock, deliver=outbox
    )
    assert exporter.export(store, after_run=0, up_to_run=1).endswith("retained messages published")
    return outbox


def test_state_reports_complete_windows_and_null_otherwise(
    store: Store, clock: FrozenClock
) -> None:
    fill(store, days=10)
    outbox = export(store, clock, max_power=True)

    state = json.loads(outbox.payload(f"releve/{PDL}/state"))
    assert state == {
        "energy_yesterday_kwh": 1.0,
        "energy_last_7_days_kwh": 7.0,
        "energy_last_30_days_kwh": None,
        "latest_day": "2026-09-11",
        "max_power_yesterday_va": 6100,
    }
    assert json.loads(outbox.payload("releve/rte/state")) == {
        "day": "2026-09-12",
        "tempo_today": "RED",
        "ecowatt_today": 3,
        "ecowatt_message": "coupures possibles",
        "tempo_days_left_blue": None,
        "tempo_days_left_white": None,
        "tempo_days_left_red": None,
        "tempo_prices": None,
    }


def test_an_unpublished_yesterday_is_unknown_not_zero(store: Store, clock: FrozenClock) -> None:
    state = json.loads(export(store, clock).payload(f"releve/{PDL}/state"))
    assert state["energy_yesterday_kwh"] is None
    assert state["latest_day"] is None


def test_discovery_is_stable_and_follows_the_configuration(
    store: Store, clock: FrozenClock
) -> None:
    outbox = export(store, clock, rte=False, max_power=False, production=True)
    topics = dict(outbox.messages)

    energy = json.loads(topics[f"homeassistant/sensor/releve_{PDL}/energy_yesterday/config"])
    assert energy["unique_id"] == f"releve_{PDL}_energy_yesterday"
    assert energy["value_template"] == "{{ value_json.energy_yesterday_kwh }}"
    assert energy["device_class"] == "energy"
    assert "state_class" not in energy  # a rolling total is not a meter reading
    assert energy["device"]["identifiers"] == [f"releve_{PDL}"]
    assert topics[f"homeassistant/sensor/releve_{PDL}/max_power_yesterday/config"] == ""
    assert f"homeassistant/sensor/releve_{PDL}/production_yesterday/config" in topics
    assert "releve/rte/state" not in topics


def test_discovery_can_be_turned_off(store: Store, clock: FrozenClock) -> None:
    outbox = Outbox()
    settings = MqttSettings(enabled=True, home_assistant_discovery=False, base_topic="energy/linky")
    MqttExporter(
        settings, [UsagePointSettings(id=PDL)], rte=True, clock=clock, deliver=outbox
    ).export(store, after_run=0, up_to_run=0)
    assert [topic for topic, _ in outbox.messages] == [
        f"energy/linky/{PDL}/state",
        "energy/linky/rte/state",
    ]


def test_an_unreachable_broker_is_an_export_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ExportError, match=r"MQTT delivery to 127\.0\.0\.1"):
        deliver_with_paho(MqttSettings(host="127.0.0.1", port=port), [("t", "p")])


@pytest.mark.skipif("MQTT_TEST_BROKER" not in os.environ, reason="needs MQTT_TEST_BROKER=host:port")
def test_messages_reach_a_real_broker_retained() -> None:
    host, port = os.environ["MQTT_TEST_BROKER"].rsplit(":", 1)
    topic = f"releve-test/{os.getpid()}/state"
    deliver_with_paho(MqttSettings(host=host, port=int(port)), [(topic, '{"ok": true}')])

    received: list[bytes] = []
    subscriber = paho.Client(CallbackAPIVersion.VERSION2)
    subscriber.on_message = lambda _client, _userdata, message: received.append(message.payload)
    subscriber.connect(host, int(port))
    subscriber.subscribe(topic, qos=1)
    for _ in range(50):
        subscriber.loop(timeout=0.1)
        if received:
            break
    subscriber.disconnect()
    assert received == [b'{"ok": true}']  # retained: delivered to a later subscriber
    deliver_with_paho(MqttSettings(host=host, port=int(port)), [(topic, "")])  # clean up
