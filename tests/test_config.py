"""Configuration: sources, strictness, and upgrade hints."""

from __future__ import annotations

from pathlib import Path

import pytest

from releve.config import (
    Settings,
    default_config_path,
    default_database_path,
    example_config,
    load_settings,
    resolve_config_path,
)
from releve.domain import Dataset
from releve.errors import ConfigError
from tests.conftest import OTHER_PDL, PDL


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_shipped_example_is_valid_and_needs_only_a_token(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, example_config()), must_exist=True)
    with pytest.raises(ConfigError, match=r"gateway\.token is empty"):
        settings.require_runnable()
    assert settings.usage_points[0].datasets == (Dataset.DAILY_CONSUMPTION,)
    assert settings.storage.path == default_database_path()


def test_defaults_follow_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_config_path() == tmp_path / "xdg-config" / "releve" / "config.yaml"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/is/ignored")
    assert default_database_path() == Path.home() / ".local/state/releve/releve.db"


def test_the_environment_overrides_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, f'gateway:\n  token: from-file\nusage_points:\n  - id: "{PDL}"\n')
    monkeypatch.setenv("RELEVE_GATEWAY__TOKEN", "from-env")
    monkeypatch.setenv("RELEVE_CONFIG", str(path))
    resolved, must_exist = resolve_config_path(None)
    settings = load_settings(resolved, must_exist=must_exist)
    assert settings.gateway.token.get_secret_value() == "from-env"
    assert "from-env" not in repr(settings)


def test_an_absent_default_file_means_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELEVE_USAGE_POINTS", f'[{{"id": "{PDL}"}}]')
    path, must_exist = resolve_config_path(None)
    assert not must_exist
    assert load_settings(path, must_exist=must_exist).usage_points[0].id == PDL


def test_an_explicit_file_must_exist(tmp_path: Path) -> None:
    path, must_exist = resolve_config_path(tmp_path / "nope.yaml")
    with pytest.raises(ConfigError, match="not found"):
        load_settings(path, must_exist=must_exist)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("exporters:\n  mqtt:\n    enabeld: true\n", "exporters.mqtt.enabeld: unknown key (typo?)"),
        ("gateway:\n  base_url: http://gateway\n", "must start with https://"),
        ("usage_points:\n  - id: 12345678901234\n", "quote the PDL"),
        ("usage_points:\n  - id: '1234'\n", "usage_points[0].id: must be the 14-digit PDL"),
        (f"usage_points:\n  - id: '{PDL}'\n  - id: '{PDL}'\n", "declared twice"),
        ("storage:\n  path: relative.db\n", "must be an absolute path"),
        ("exporters:\n  home_assistant:\n    enabled: true\n", "token is required"),
        ("exporters:\n  home_assistant:\n    statistic_id: 'Bad Id'\n", "source:object_id"),
        ("exporters:\n  home_assistant:\n    statistic_id: 'a:{pdl}_{x}'\n", "only placeholder"),
        ("exporters:\n  home_assistant:\n    url: http://ha\n", "ws:// or wss://"),
        ("exporters:\n  influxdb:\n    url: influx:8086\n", "http:// or https://"),
        ("exporters:\n  mqtt:\n    base_topic: 'a/#'\n", "without wildcards"),
        ("log_level: chatty\n", "log_level"),
        ("web:\n  auth_token: ''\n", "remove the key to run without authentication"),
        (
            (
                "exporters:\n  home_assistant:\n"
                "    production_statistic_id: 'releve:{pdl}_consumption'\n"
            ),
            "need different statistic ids",
        ),
        ("gateway: [unclosed\n", "not valid YAML"),
    ],
)
def test_invalid_configurations_explain_themselves(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings(write(tmp_path, text), must_exist=True)
    assert message in str(caught.value)


def test_several_usage_points_need_pdl_in_statistic_ids(tmp_path: Path) -> None:
    text = (
        f"usage_points:\n  - id: '{PDL}'\n  - id: '{OTHER_PDL}'\n"
        "exporters:\n  home_assistant:\n    statistic_id: 'archive:consumption'\n"
    )
    with pytest.raises(ConfigError, match="needs \\{pdl\\}"):
        load_settings(write(tmp_path, text), must_exist=True)


def test_an_earlier_configuration_gets_upgrade_hints(tmp_path: Path) -> None:
    legacy = """
gateway: {token: t, prefer_cache: true, daily_call_budget: 40}
usage_points:
  - {id: "01234567890123", name: Maison, consumption: true}
  - {id: "01234567890124", consumption_detail: true, max_power: true}
storage: {url: "sqlite:///data/releve.db"}
sync: {bootstrap_days: 365, interval_hours: 4}
web: {host: "0.0.0.0", port: 8080}
exporters:
  home_assistant:
    enabled: true
    url: "ws://homeassistant.local:8123/api/websocket"
    token: t
    statistic_id: "myelectricaldata:{pdl}_consumption_full"
    statistic_name: "Linky {pdl} consommation"
    anchor_hour: 23
    allow_insecure: true
observability: {sentry_dsn: ""}
"""
    with pytest.raises(ConfigError) as caught:
        load_settings(write(tmp_path, legacy), must_exist=True)
    message = str(caught.value)
    assert "storage.url: replaced by storage.path" in message
    assert "sync.bootstrap_days: renamed to sync.history_days" in message
    assert (
        "exporters.home_assistant.anchor_hour: removed: a day without a complete load curve"
        in message
    )
    assert "exporters.home_assistant.allow_insecure: removed" in message
    assert "observability: removed" in message

    upgraded = (
        legacy.replace('storage: {url: "sqlite:///data/releve.db"}\n', "")
        .replace("bootstrap_days", "history_days")
        .replace("    anchor_hour: 23\n    allow_insecure: true\n", "")
        .replace('observability: {sentry_dsn: ""}\n', "")
    )
    settings = load_settings(write(tmp_path, upgraded), must_exist=True).require_runnable()
    assert settings.usage_points[0].datasets == (Dataset.DAILY_CONSUMPTION,)
    assert settings.usage_points[1].datasets == (
        Dataset.DAILY_CONSUMPTION,
        Dataset.MAX_POWER,
        Dataset.CURVE_CONSUMPTION,
    )


def test_log_level_is_case_insensitive() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_the_image_default_database_yields_to_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELEVE_DEFAULT_DATABASE", "/image/default.db")
    assert load_settings(write(tmp_path, "{}\n"), must_exist=True).storage.path == Path(
        "/image/default.db"
    )
    chosen = write(tmp_path, "storage:\n  path: /srv/mine.db\n")
    assert load_settings(chosen, must_exist=True).storage.path == Path("/srv/mine.db")


def test_a_config_file_in_the_working_directory_is_not_read_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path, "gateway:\n  token: t\n")
    path, must_exist = resolve_config_path(None)
    with pytest.raises(ConfigError, match="not read implicitly"):
        load_settings(path, must_exist=must_exist)
