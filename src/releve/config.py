"""Configuration: one YAML file, overridable key by key from the environment.

Precedence, highest first: environment (`RELEVE_` prefix, `__` between
levels, e.g. `RELEVE_GATEWAY__TOKEN`) > YAML file > defaults.

Every section rejects unknown keys: a typo is an error, never a silently
ignored setting. Keys of the earlier layout fail with a hint.
"""

from __future__ import annotations

import os
import re
import string
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from releve.domain import Dataset
from releve.errors import ConfigError

CONFIG_PATH_ENV = "RELEVE_CONFIG"
# The DEFAULT of storage.path: lower precedence than the YAML file, unlike
# RELEVE_STORAGE__PATH. The Docker image sets it.
DEFAULT_DATABASE_ENV = "RELEVE_DEFAULT_DATABASE"
GATEWAY_DAILY_QUOTA = 50  # the gateway's documented limit, per usage point per day

# Home Assistant's own rule for external statistic ids.
_STATISTIC_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$")
_SAMPLE_PDL = "01234567890123"


def default_config_path() -> Path:
    return _xdg_base("XDG_CONFIG_HOME", Path(".config")) / "releve" / "config.yaml"


def default_database_path() -> Path:
    if from_env := os.environ.get(DEFAULT_DATABASE_ENV):
        return Path(from_env)
    return _xdg_base("XDG_STATE_HOME", Path(".local/state")) / "releve" / "releve.db"


def _xdg_base(variable: str, fallback: Path) -> Path:
    # The XDG spec says relative values must be ignored.
    value = Path(os.environ.get(variable, ""))
    return value if value.is_absolute() else Path.home() / fallback


def example_config() -> str:
    """The commented reference configuration shipped with the package."""
    return files("releve").joinpath("config.example.yaml").read_text(encoding="utf-8")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GatewaySettings(_Section):
    token: SecretStr = SecretStr("")
    base_url: str = "https://www.myelectricaldata.fr"
    prefer_cache: bool = True
    daily_call_budget: int = Field(default=45, ge=1, le=GATEWAY_DAILY_QUOTA)
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must start with https:// — the token travels in a request header")
        return value.rstrip("/")


class UsagePointSettings(_Section):
    id: str
    name: str = ""
    consumption: bool = True
    consumption_detail: bool = False
    production: bool = False
    production_detail: bool = False
    max_power: bool = False

    @field_validator("id", mode="before")
    @classmethod
    def _quoted(cls, value: object) -> object:
        if isinstance(value, int):
            raise ValueError(
                'quote the PDL (id: "01234567890123") — YAML reads bare digits as a number'
            )
        return value

    @field_validator("id")
    @classmethod
    def _fourteen_digits(cls, value: str) -> str:
        if not re.fullmatch(r"\d{14}", value):
            raise ValueError("must be the 14-digit PDL")
        return value

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def datasets(self) -> tuple[Dataset, ...]:
        """The datasets to cache, in fetch order."""
        wanted = {
            Dataset.DAILY_CONSUMPTION: self.consumption,
            Dataset.DAILY_PRODUCTION: self.production,
            Dataset.MAX_POWER: self.max_power,
            Dataset.CURVE_CONSUMPTION: self.consumption_detail,
            Dataset.CURVE_PRODUCTION: self.production_detail,
        }
        return tuple(dataset for dataset in Dataset if wanted[dataset])


class SyncSettings(_Section):
    history_days: int = Field(default=365, ge=1, le=1094)
    interval_hours: float = Field(default=4.0, ge=0.5, le=24)
    rte_signals: bool = True


class StorageSettings(_Section):
    path: Path = Field(default_factory=default_database_path)

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError(
                "must be an absolute path — a relative one would change with the working directory"
            )
        return expanded


class MqttSettings(_Section):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=1883, ge=1, le=65535)
    tls: bool = False
    tls_ca_certs: Path | None = None
    username: str = ""
    password: SecretStr = SecretStr("")
    base_topic: str = "releve"
    home_assistant_discovery: bool = True
    discovery_prefix: str = "homeassistant"

    @field_validator("base_topic", "discovery_prefix")
    @classmethod
    def _plain_topic(cls, value: str) -> str:
        if not value or value.startswith("/") or value.endswith("/") or set(value) & {"+", "#"}:
            raise ValueError("must be a non-empty topic without wildcards or edge slashes")
        return value


class HomeAssistantSettings(_Section):
    enabled: bool = False
    url: str = "ws://homeassistant.local:8123/api/websocket"
    token: SecretStr = SecretStr("")
    # `{pdl}` is replaced by the usage point id. Pointing `statistic_id` at a
    # series that already exists in Home Assistant CONTINUES it (see README).
    statistic_id: str = "releve:{pdl}_consumption"
    statistic_name: str = "Electricity consumption {pdl}"
    production_statistic_id: str = "releve:{pdl}_production"
    production_statistic_name: str = "Electricity production {pdl}"

    @field_validator("url")
    @classmethod
    def _websocket_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("must be a ws:// or wss:// URL ending in /api/websocket")
        return value

    @field_validator(
        "statistic_id", "statistic_name", "production_statistic_id", "production_statistic_name"
    )
    @classmethod
    def _pdl_template(cls, value: str) -> str:
        fields = {name for _, name, _, _ in string.Formatter().parse(value) if name is not None}
        if fields - {"pdl"}:
            raise ValueError("the only placeholder allowed is {pdl}")
        return value

    @field_validator("statistic_id", "production_statistic_id")
    @classmethod
    def _valid_statistic_id(cls, value: str) -> str:
        if not _STATISTIC_ID.match(value.format(pdl=_SAMPLE_PDL)):
            raise ValueError("must look like 'source:object_id' (lower-case letters, digits, _)")
        return value


class InfluxSettings(_Section):
    enabled: bool = False
    url: str = "http://127.0.0.1:8086/api/v2/write"
    token: SecretStr = SecretStr("")
    org: str = ""
    bucket: str = "energy"
    measurement: str = "energy"

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must be an http:// or https:// write endpoint")
        return value


class ExportersSettings(_Section):
    mqtt: MqttSettings = Field(default_factory=MqttSettings)
    home_assistant: HomeAssistantSettings = Field(default_factory=HomeAssistantSettings)
    influxdb: InfluxSettings = Field(default_factory=InfluxSettings)


class WebSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    # When set, every route but /healthz requires this token, either as
    # `Authorization: Bearer <token>` or as the password of HTTP Basic auth.
    auth_token: SecretStr | None = None

    @field_validator("auth_token")
    @classmethod
    def _not_empty(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("is empty — remove the key to run without authentication")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELEVE_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )

    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    usage_points: tuple[UsagePointSettings, ...] = ()
    sync: SyncSettings = Field(default_factory=SyncSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    exporters: ExportersSettings = Field(default_factory=ExportersSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [up.id for up in self.usage_points]
        duplicates = sorted({pdl for pdl in ids if ids.count(pdl) > 1})
        if duplicates:
            raise ValueError(f"usage point declared twice: {', '.join(duplicates)}")
        ha = self.exporters.home_assistant
        if ha.enabled and not ha.token.get_secret_value():
            raise ValueError("exporters.home_assistant.token is required when enabled")
        if ha.statistic_id == ha.production_statistic_id:
            raise ValueError("consumption and production need different statistic ids")
        if len(ids) > 1:
            for template in (ha.statistic_id, ha.production_statistic_id):
                if "{pdl}" not in template:
                    raise ValueError(
                        f"statistic id {template!r} needs {{pdl}} with several usage points"
                    )
        return self

    def require_runnable(self) -> Self:
        """Checks that only matter to commands that talk to the gateway."""
        if not self.gateway.token.get_secret_value():
            raise ConfigError("gateway.token is empty — generate one on myelectricaldata.fr")
        if not self.usage_points:
            raise ConfigError("usage_points is empty — declare at least one PDL")
        return self


def resolve_config_path(cli_path: Path | None) -> tuple[Path, bool]:
    """Where to read the configuration, and whether that file MUST exist.

    An explicit choice (`--config` or `$RELEVE_CONFIG`) must exist; the
    default location may be absent, for environment-only configurations.
    """
    if cli_path is not None:
        return cli_path, True
    if from_env := os.environ.get(CONFIG_PATH_ENV):
        return Path(from_env), True
    return default_config_path(), False


def load_settings(path: Path, *, must_exist: bool) -> Settings:
    if not path.is_file():
        if must_exist:
            raise ConfigError(f"configuration file not found: {path}")
        if Path("config.yaml").is_file():
            raise ConfigError(
                f"./config.yaml is not read implicitly: pass --config config.yaml, "
                f"set ${CONFIG_PATH_ENV}, or move it to {path}"
            )
        return _load(None)
    return _load(path)


def _load(yaml_file: Path | None) -> Settings:
    class _FromFile(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            del dotenv_settings, file_secret_settings  # no .env, no secrets directory
            if yaml_file is None:
                return (init_settings, env_settings)
            yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file)
            return (init_settings, env_settings, yaml_source)

    try:
        return _FromFile()
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration:\n{_describe(exc)}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{yaml_file} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {yaml_file}: {exc}") from exc


# Keys of the earlier layout and what to do about them.
_RETIRED_KEYS = {
    "storage.url": ("replaced by storage.path, the absolute path of the SQLite file"),
    "storage.call_retention_days": "removed: journals are pruned automatically",
    "storage.sync_retention_days": "removed: journals are pruned automatically",
    "storage.load_curve_retention_days": "removed: the cache keeps metering data",
    "sync.bootstrap_days": (
        "renamed to sync.history_days — how many days back the cache is kept complete"
    ),
    "sync.curve_bootstrap_days": (
        "removed: the load curve follows sync.history_days (at most 729 days), newest first"
    ),
    "exporters.mqtt.retain_discovery": "removed: discovery and state are always retained",
    "exporters.mqtt.retain_state": "removed: discovery and state are always retained",
    "exporters.*.allow_insecure": "removed: the URL scheme or mqtt.tls you set is used as-is",
    "exporters.home_assistant.anchor_hour": (
        "removed: a day without a complete load curve is always stamped at 23:00, "
        "when its total is known"
    ),
    "exporters.home_assistant.hourly_from_curve": (
        "removed: days with a complete load curve are always exported hour by hour"
    ),
    "web.protect_metrics": "removed: web.auth_token guards /metrics too (/healthz stays open)",
    "web.rate_limit_per_minute": "removed: use a reverse proxy to expose the web interface",
    "web.expose_docs": "removed: there is no OpenAPI page any more",
    "web.max_range_days": "removed: API ranges are capped at 1096 days",
    "web.hash_metrics_labels": "removed",
    "observability": "removed: failures are logged and journaled; Sentry support is gone",
}


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        location = _location(item["loc"])
        message = str(item["msg"]).removeprefix("Value error, ")
        if item["type"] == "extra_forbidden":
            message = _retired_hint(item["loc"]) or "unknown key (typo?)"
        lines.append(f"  {location or '(root)'}: {message}")
    return "\n".join(lines)


def _location(loc: tuple[int | str, ...]) -> str:
    text = ""
    for part in loc:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text.lstrip(".")


def _retired_hint(loc: tuple[int | str, ...]) -> str | None:
    dotted = ".".join(str(part) for part in loc)
    if dotted in _RETIRED_KEYS:
        return _RETIRED_KEYS[dotted]
    if len(loc) == 3 and loc[0] == "exporters":
        return _RETIRED_KEYS.get(f"exporters.*.{loc[2]}")
    return None
