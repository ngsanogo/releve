"""The command line, end to end — the gateway is mocked at the HTTP layer."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from releve import __version__
from releve.cli import EXIT_BUSY, EXIT_CONFIG, EXIT_FAILURES, EXIT_OK, main
from releve.clock import paris_today, utc_now
from releve.config import default_config_path
from releve.gateway import GatewayClient
from releve.store import Store
from releve.sync import exclusive_pass
from tests.conftest import PDL
from tests.fakes import HttpDouble

TODAY = paris_today(utc_now())  # the CLI runs on the real clock


def config(tmp_path: Path, history_days: int = 3) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "gateway:\n  token: test-token\n  prefer_cache: false\n"
        f'usage_points:\n  - id: "{PDL}"\n'
        f"sync:\n  history_days: {history_days}\n  rte_signals: false\n"
        f"storage:\n  path: {tmp_path / 'cache.db'}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> HttpDouble:
    """Route the CLI's gateway client to an HTTP double."""
    double = HttpDouble()

    def with_double(*args: Any, **kwargs: Any) -> GatewayClient:
        return GatewayClient(*args, transport=double.transport, **kwargs)

    monkeypatch.setattr("releve.cli.GatewayClient", with_double)
    return double


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == __version__


def test_init_writes_a_private_starter_file_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init"]) == EXIT_OK
    path = default_config_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "gateway:" in path.read_text()
    assert main(["init"]) == EXIT_CONFIG
    assert "already exists" in capsys.readouterr().err
    assert main(["init", "--force"]) == EXIT_OK
    assert main(["init", "--stdout"]) == EXIT_OK
    assert capsys.readouterr().out.startswith("wrote")


def test_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "-c", str(config(tmp_path))]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"usage point   : {PDL} ({PDL}): daily_consumption" in out
    assert "✔ ready" in out
    assert main(["check", "-c", str(tmp_path / "absent.yaml")]) == EXIT_CONFIG


def test_sync_then_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], gateway: HttpDouble
) -> None:
    path = config(tmp_path)
    start, end = TODAY - timedelta(days=3), TODAY
    payload = {
        "meter_reading": {
            "interval_reading": [
                {"date": str(start + timedelta(days=n)), "value": "5000"} for n in range(3)
            ]
        }
    }
    gateway.on(f"/daily_consumption/{PDL}/start/{start}/end/{end}", json=payload)
    assert main(["sync", "-c", str(path)]) == EXIT_OK
    assert f"✔ {PDL}: daily_consumption +3" in capsys.readouterr().out

    assert main(["status", "-c", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "calls today  : 1/45" in out
    assert f"daily_consumption : newest {end - timedelta(days=1)}, 0 days still to fetch" in out


def test_a_failing_sync_exits_1_and_a_busy_one_exits_3(tmp_path: Path, gateway: HttpDouble) -> None:
    path = config(tmp_path, history_days=1)
    start = TODAY - timedelta(days=1)
    gateway.on(f"/daily_consumption/{PDL}/start/{start}/end/{TODAY}", status=403, json={})
    assert main(["sync", "-c", str(path)]) == EXIT_FAILURES
    Store.open(tmp_path / "cache.db")
    with exclusive_pass(tmp_path / "cache.db"):
        assert main(["sync", "-c", str(path)]) == EXIT_BUSY


def test_ha_boundary_show_and_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = str(config(tmp_path))
    assert main(["ha-boundary", "-c", path]) == EXIT_OK
    assert "no boundary pinned yet" in capsys.readouterr().out
    assert (
        main(["ha-boundary", "-c", path, "--set", "archive:series", "2026-08-28", "8481.922"])
        == EXIT_OK
    )
    assert main(["ha-boundary", "-c", path, "--set", "archive:fresh", "none", "0"]) == EXIT_OK
    assert main(["ha-boundary", "-c", path]) == EXIT_OK
    out = capsys.readouterr().out
    assert "archive:series: owned after 2026-08-28, from 8481.922 kWh, for the next" in out
    assert "archive:fresh: owned after its start, from 0.0 kWh" in out
    assert (
        main(["ha-boundary", "-c", path, "--set", "archive:series", "yesterday", "1"])
        == EXIT_CONFIG
    )


def test_serve_runs_the_web_interface_with_a_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: dict[str, Any] = {}

    def fake_uvicorn_run(app: Any, **options: Any) -> None:
        served.update(options, app=app)

    monkeypatch.setattr("releve.cli.uvicorn.run", fake_uvicorn_run)
    path = config(tmp_path)
    path.write_text(path.read_text() + "web:\n  host: 0.0.0.0\n", encoding="utf-8")

    assert main(["serve", "-c", str(path)]) == EXIT_OK

    assert served["host"] == "0.0.0.0"  # noqa: S104 — what the configuration asked for
    assert served["log_config"] is None
    assert served["app"].routes
    assert "without web.auth_token" in caplog.text
