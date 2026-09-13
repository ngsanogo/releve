"""The releve command line.

    releve init          write a commented configuration file to start from
    releve check         validate the configuration and show what it means
    releve sync          run one sync pass now
    releve status        freshness, backlog, quota and exports
    releve serve         the daemon: a sync pass every interval, plus the web interface
    releve ha-boundary   show or set where Home Assistant series begin
    releve purge-cache   delete MyElectricalData's remote cache for a usage point
    releve version

The configuration file is `--config`, else `$RELEVE_CONFIG`, else
`~/.config/releve/config.yaml`.

Exit status: 0 success; 1 the pass completed with failures; 2 invalid
configuration or unusable database; 3 another sync pass is already running.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from pathlib import Path

import uvicorn

from releve import __version__
from releve.clock import format_paris, paris_today, utc_now
from releve.config import (
    Settings,
    example_config,
    load_settings,
    resolve_config_path,
)
from releve.domain import CacheResource
from releve.errors import ConfigError, GatewayError, StoreError, SyncAlreadyRunningError
from releve.exporters import build_exporters
from releve.exporters.home_assistant import SINK_PREFIX as HA_SINK_PREFIX
from releve.gateway import GatewayClient
from releve.quota import RTE_BUCKET, QuotaGovernor
from releve.scheduler import Scheduler
from releve.store import HaBoundary, Store
from releve.sync import PassReport, backlog, run_pass
from releve.web import create_app

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_CONFIG = 2
EXIT_BUSY = 3

log = logging.getLogger(__name__)

Handler = Callable[[argparse.Namespace], int]


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args)
    except (ConfigError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", type=Path, help="configuration file (default: see `releve --help`)"
    )
    parser = argparse.ArgumentParser(
        prog="releve",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    init = commands.add_parser("init", parents=[common], help="write a starter configuration")
    init.add_argument("--stdout", action="store_true", help="print it instead of writing a file")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(handler=_init)

    for name, handler, summary in (
        ("check", _check, "validate the configuration"),
        ("sync", _sync, "run one sync pass now"),
        ("status", _status, "show freshness, backlog, quota and exports"),
        ("serve", _serve, "run the daemon and the web interface"),
    ):
        commands.add_parser(name, parents=[common], help=summary).set_defaults(handler=handler)

    boundary = commands.add_parser(
        "ha-boundary",
        parents=[common],
        help="show or set Home Assistant series boundaries",
        description=(
            "A series boundary is the last (day, sum) Home Assistant held before releve "
            "first exported to it. Days after it belong to releve."
        ),
    )
    boundary.add_argument(
        "--set",
        nargs=3,
        metavar=("STATISTIC_ID", "DAY", "SUM_KWH"),
        help="pin a boundary; DAY is YYYY-MM-DD, or 'none' for a series owned from its start",
    )
    boundary.set_defaults(handler=_ha_boundary)

    purge = commands.add_parser(
        "purge-cache",
        parents=[common],
        help="delete MyElectricalData's remote cache for a usage point",
        description=(
            "Asks MyElectricalData to drop its encrypted 30-day cache for a resource. "
            "Counts against the daily quota. The local SQLite cache is never touched."
        ),
    )
    purge.add_argument("usage_point", help="14-digit PDL")
    purge.add_argument(
        "--resource",
        default=CacheResource.ALL.value,
        choices=[resource.value for resource in CacheResource],
        help="what to delete (default: all)",
    )
    purge.add_argument(
        "--start", type=date.fromisoformat, help="inclusive start for dated resources"
    )
    purge.add_argument("--end", type=date.fromisoformat, help="exclusive end for dated resources")
    purge.set_defaults(handler=_purge_cache)

    commands.add_parser("version", help="print the version").set_defaults(
        handler=lambda _: _print_version()
    )
    return parser


# -- commands ---------------------------------------------------------------------------
def _print_version() -> int:
    print(__version__)
    return EXIT_OK


def _init(args: argparse.Namespace) -> int:
    if args.stdout:
        print(example_config(), end="")
        return EXIT_OK
    path, _ = resolve_config_path(args.config)
    if path.exists() and not args.force:
        raise ConfigError(f"{path} already exists (use --force to overwrite it)")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(example_config())
    print(f"wrote {path} — set gateway.token and your usage point, then run: releve check")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    path, must_exist = resolve_config_path(args.config)
    settings = load_settings(path, must_exist=must_exist)
    source = path if path.is_file() else f"none ({path} absent) — environment only"
    print(f"configuration : {source}")
    print(f"database      : {settings.storage.path}")
    gateway = settings.gateway
    token = "set" if gateway.token.get_secret_value() else "MISSING"
    print(f"gateway       : {gateway.base_url} (token {token})")
    print(f"quota         : {gateway.daily_call_budget} calls per day per usage point")
    sync = settings.sync
    rte = "with" if sync.rte_signals else "without"
    print(
        f"sync          : every {sync.interval_hours:g} h, "
        f"{sync.history_days} days of history, {rte} Tempo/Ecowatt"
    )
    for up in settings.usage_points:
        datasets = ", ".join(up.datasets) or "nothing enabled"
        customer = ", ".join(up.customer_resources)
        extra = f"; customer: {customer}" if customer else ""
        print(f"usage point   : {up.id} ({up.label}): {datasets}{extra}")
    exporters = settings.exporters
    destinations = [
        f"mqtt → {exporters.mqtt.host}:{exporters.mqtt.port}" if exporters.mqtt.enabled else "",
        f"home_assistant → {exporters.home_assistant.url}"
        if exporters.home_assistant.enabled
        else "",
        f"influxdb → {exporters.influxdb.url}" if exporters.influxdb.enabled else "",
    ]
    print(f"exporters     : {', '.join(filter(None, destinations)) or 'none'}")
    auth = "token required" if settings.web.auth_token else "no authentication"
    print(f"web           : http://{settings.web.host}:{settings.web.port} ({auth})")
    settings.require_runnable()
    print("✔ ready")
    return EXIT_OK


def _sync(args: argparse.Namespace) -> int:
    settings = _settings(args).require_runnable()
    store = Store.open(settings.storage.path)
    governor = QuotaGovernor(store, settings.gateway.daily_call_budget)
    with GatewayClient(settings.gateway, governor) as gateway:
        try:
            report = run_pass(settings, gateway, store, build_exporters(settings))
        except SyncAlreadyRunningError as exc:
            print(f"✘ {exc}", file=sys.stderr)
            return EXIT_BUSY
    _print_report(report)
    return EXIT_OK if report.ok else EXIT_FAILURES


def _status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store.open(settings.storage.path)
    governor = QuotaGovernor(store, settings.gateway.daily_call_budget)
    today = paris_today(utc_now())
    successes = store.last_success()
    for up in settings.usage_points:
        print(f"{up.id} ({up.label})")
        print(f"  last success : {format_paris(successes.get(up.id))}")
        consent = store.consent(up.id)
        if consent is not None:
            state = "valid" if consent.granted else "invalid"
            print(f"  consent      : {state}, expires {format_paris(consent.expires_at)}")
        contract = store.contract(up.id)
        if contract is not None:
            print(
                f"  contract     : {contract.distribution_tariff or '—'}, "
                f"{contract.subscribed_power or '—'}, {contract.offpeak_hours or 'no HC'}"
            )
        _print_quota(governor, up.id)
        for dataset in up.datasets:
            latest = store.latest_day(up.id, dataset)
            missing = len(backlog(store, up.id, dataset, settings.sync.history_days, today))
            print(f"  {dataset:<18}: newest {latest or '—'}, {missing} days still to fetch")
    if settings.sync.rte_signals:
        print(f"rte (Tempo/Ecowatt), last success {format_paris(successes.get(RTE_BUCKET))}")
        _print_quota(governor, RTE_BUCKET)
    for cursor in store.export_cursors():
        delivered = format_paris(cursor.exported_at)
        print(f"export {cursor.sink}: delivered up to run {cursor.run_id} at {delivered}")
    failures = [event for event in store.recent_events(20) if not event.ok]
    for event in failures[:5]:
        print(f"recent failure {format_paris(event.at)} {event.subject}: {event.detail}")
    return EXIT_OK


def _serve(args: argparse.Namespace) -> int:
    settings = _settings(args).require_runnable()
    store = Store.open(settings.storage.path)
    governor = QuotaGovernor(store, settings.gateway.daily_call_budget)
    exporters = build_exporters(settings)
    with GatewayClient(settings.gateway, governor) as gateway:

        def one_pass() -> None:
            try:
                run_pass(settings, gateway, store, exporters)
            except SyncAlreadyRunningError as exc:
                log.warning("pass skipped: %s", exc)

        scheduler = Scheduler(one_pass, timedelta(hours=settings.sync.interval_hours))
        web = settings.web
        if web.host not in ("127.0.0.1", "::1", "localhost") and web.auth_token is None:
            log.warning(
                "the web interface listens on %s without web.auth_token: "
                "anyone who reaches the port can read your consumption history",
                web.host,
            )
        scheduler.start()
        log.info("releve %s — http://%s:%d", __version__, web.host, web.port)
        try:
            uvicorn.run(
                create_app(settings, store, governor, scheduler),
                host=web.host,
                port=web.port,
                log_config=None,
                access_log=False,
            )
        finally:
            scheduler.stop()
    return EXIT_OK


def _ha_boundary(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = Store.open(settings.storage.path)
    if args.set:
        statistic_id, raw_day, raw_sum = args.set
        try:
            day = None if raw_day.lower() == "none" else date.fromisoformat(raw_day)
            total = float(raw_sum)
        except ValueError as exc:
            raise ConfigError(f"invalid boundary: {exc}") from exc
        store.set_ha_boundary(HaBoundary(statistic_id, day, total), restart_sinks=HA_SINK_PREFIX)
        print(
            f"pinned {statistic_id}: owned after {day or 'its start'}, continuing from {total} kWh;"
            " the next Home Assistant export rewrites everything after it"
        )
        return EXIT_OK
    boundaries = store.ha_boundaries()
    for boundary in boundaries:
        after = boundary.base_day or "its start"
        owner = boundary.usage_point or "the next usage point exporting to it"
        print(
            f"{boundary.statistic_id}: owned after {after}, from {boundary.base_sum_kwh} kWh,"
            f" for {owner}"
        )
    if not boundaries:
        print("no boundary pinned yet — the first export pins one per series")
    return EXIT_OK


def _purge_cache(args: argparse.Namespace) -> int:
    settings = _settings(args).require_runnable()
    if not re.fullmatch(r"\d{14}", args.usage_point):
        raise ConfigError(f"{args.usage_point!r} is not a 14-digit PDL")
    resource = CacheResource(args.resource)
    if resource.needs_dates and (args.start is None or args.end is None):
        raise ConfigError(f"{resource}: --start and --end are required")
    if not resource.needs_dates and (args.start is not None or args.end is not None):
        raise ConfigError(f"{resource}: does not take a date range")
    store = Store.open(settings.storage.path)
    governor = QuotaGovernor(store, settings.gateway.daily_call_budget)
    with GatewayClient(settings.gateway, governor) as gateway:
        try:
            gateway.delete_cache(args.usage_point, resource, start=args.start, end=args.end)
        except GatewayError as exc:
            print(f"✘ {exc}", file=sys.stderr)
            return EXIT_FAILURES
    print(f"✔ purged remote cache {resource} for {args.usage_point}")
    return EXIT_OK


# -- helpers ------------------------------------------------------------------------------
def _settings(args: argparse.Namespace) -> Settings:
    path, must_exist = resolve_config_path(args.config)
    settings = load_settings(path, must_exist=must_exist)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return settings


def _print_report(report: PassReport) -> None:
    for outcome in report.outcomes:
        mark = "✔" if outcome.ok else "✘"
        print(f"{mark} {outcome.subject}: {outcome.detail}")


def _print_quota(governor: QuotaGovernor, bucket: str) -> None:
    usage = governor.usage(bucket)
    line = f"  calls today  : {usage.used}/{governor.daily_budget}"
    if usage.blocked_until is not None:
        line += f", blocked until {format_paris(usage.blocked_until)} ({usage.blocked_cause})"
    print(line)
