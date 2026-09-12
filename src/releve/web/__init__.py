"""Read-only web interface: what the cache knows and what the governor is doing.

The web layer never talks to the gateway.

    GET /                                   dashboard
    GET /usage-points/{pdl}                 the last 31 days of one usage point
    GET /api/v1/usage-points/{pdl}/daily    ?start=YYYY-MM-DD&end=YYYY-MM-DD[&direction=]
    GET /api/v1/rte/tempo                   ?start=&end=
    GET /api/v1/rte/ecowatt                 ?start=&end=
    GET /metrics                            Prometheus text format
    GET /healthz                            200 while the daemon does its job, else 503

When `web.auth_token` is set, every route but /healthz requires it — as a
Bearer token, or as the password of HTTP Basic auth (any user name), which
lets a browser in.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from releve import __version__
from releve.clock import Clock, format_paris, paris_today, utc_now
from releve.config import Settings, UsagePointSettings
from releve.domain import Dataset, Direction
from releve.metrics import render_metrics
from releve.quota import RTE_BUCKET, QuotaGovernor
from releve.scheduler import Scheduler
from releve.store import QuotaUsage, Store
from releve.sync import backlog

log = logging.getLogger(__name__)

MAX_RANGE_DAYS = 1096
PAGE_DAYS = 31
OPEN_PATHS = frozenset({"/healthz"})


@dataclass(frozen=True, slots=True)
class DatasetView:
    dataset: Dataset
    latest: date | None
    missing: int


@dataclass(frozen=True, slots=True)
class UsagePointView:
    settings: UsagePointSettings
    datasets: list[DatasetView]
    quota: QuotaUsage
    last_success: datetime | None


@dataclass(frozen=True, slots=True)
class DayRow:
    day: date
    consumption_wh: int | None
    production_wh: int | None
    max_power_va: int | None


def create_app(
    settings: Settings,
    store: Store,
    governor: QuotaGovernor,
    scheduler: Scheduler | None = None,
    clock: Clock = utc_now,
) -> Starlette:
    environment = Environment(
        loader=PackageLoader("releve", "web/templates"), autoescape=select_autoescape()
    )
    environment.filters["paris"] = format_paris
    templates = Jinja2Templates(env=environment)
    usage_points = {up.id: up for up in settings.usage_points}

    def dashboard(request: Request) -> Response:
        today = paris_today(clock())
        successes = store.last_success()
        views = [
            UsagePointView(
                up,
                [
                    DatasetView(
                        dataset,
                        store.latest_day(up.id, dataset),
                        len(backlog(store, up.id, dataset, settings.sync.history_days, today)),
                    )
                    for dataset in up.datasets
                ],
                governor.usage(up.id),
                successes.get(up.id),
            )
            for up in settings.usage_points
        ]
        tomorrow = today + timedelta(days=1)
        context = {
            "version": __version__,
            "usage_points": views,
            "budget": governor.daily_budget,
            "rte_quota": governor.usage(RTE_BUCKET) if settings.sync.rte_signals else None,
            "tempo": next(iter(store.tempo(today, tomorrow)), None),
            "ecowatt": next(iter(store.ecowatt(today, tomorrow)), None),
            "events": store.recent_events(30),
            "exports": store.export_cursors(),
        }
        return templates.TemplateResponse(request, "index.html", context)

    def usage_point_page(request: Request) -> Response:
        up = _known_usage_point(request, usage_points)
        end = paris_today(clock())
        start = end - timedelta(days=PAGE_DAYS)
        consumption = {r.day: r.wh for r in store.daily(up.id, Direction.CONSUMPTION, start, end)}
        production = {r.day: r.wh for r in store.daily(up.id, Direction.PRODUCTION, start, end)}
        peaks = {p.day: p.va for p in store.peaks(up.id, start, end)}
        rows = [
            DayRow(day, consumption.get(day), production.get(day), peaks.get(day))
            for day in (end - timedelta(days=offset) for offset in range(1, PAGE_DAYS + 1))
        ]
        context = {"version": __version__, "usage_point": up, "rows": rows}
        return templates.TemplateResponse(request, "usage_point.html", context)

    def api_daily(request: Request) -> Response:
        up = _known_usage_point(request, usage_points)
        start, end = _date_range(request)
        try:
            direction = Direction(request.query_params.get("direction", Direction.CONSUMPTION))
        except ValueError as exc:
            raise HTTPException(400, "direction must be consumption or production") from exc
        readings = store.daily(up.id, direction, start, end)
        return JSONResponse(
            [{"day": r.day.isoformat(), "wh": r.wh, "direction": r.direction} for r in readings]
        )

    def api_tempo(request: Request) -> Response:
        start, end = _date_range(request)
        return JSONResponse(
            [{"day": d.day.isoformat(), "color": d.color} for d in store.tempo(start, end)]
        )

    def api_ecowatt(request: Request) -> Response:
        start, end = _date_range(request)
        return JSONResponse(
            [
                {"day": d.day.isoformat(), "value": d.level, "message": d.message}
                for d in store.ecowatt(start, end)
            ]
        )

    def metrics(request: Request) -> Response:
        del request
        return PlainTextResponse(
            render_metrics(settings, store, governor, clock),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    def healthz(request: Request) -> Response:
        del request
        try:
            store.recent_events(1)
        except sqlite3.Error:
            log.exception("health check: database unavailable")
            return JSONResponse({"ok": False, "reason": "database unavailable"}, 503)
        if scheduler is not None and not scheduler.is_healthy():
            stalled = (
                "last sync pass crashed" if scheduler.last_pass_crashed else "scheduler stalled"
            )
            return JSONResponse({"ok": False, "reason": stalled}, 503)
        return JSONResponse({"ok": True, "version": __version__})

    routes = [
        Route("/", dashboard),
        Route("/usage-points/{pdl}", usage_point_page),
        Route("/api/v1/usage-points/{pdl}/daily", api_daily),
        Route("/api/v1/rte/tempo", api_tempo),
        Route("/api/v1/rte/ecowatt", api_ecowatt),
        Route("/metrics", metrics),
        Route("/healthz", healthz),
        Mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"),
    ]
    token = settings.web.auth_token
    middleware = [Middleware(TokenAuth, token=token.get_secret_value())] if token else []
    return Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={HTTPException: _http_error},  # type: ignore[dict-item]  # Starlette's handler typing is too narrow
    )


class TokenAuth:
    """Require the configured token on every route but OPEN_PATHS."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS or self._authorized(scope):
            await self._app(scope, receive, send)
            return
        challenge = PlainTextResponse(
            "authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="releve", charset="UTF-8"'},
        )
        await challenge(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        match scheme.lower():
            case "bearer":
                candidate = credentials.encode()
            case "basic":
                try:
                    decoded = base64.b64decode(credentials, validate=True)
                except binascii.Error:
                    return False
                candidate = decoded.partition(b":")[2]
            case _:
                return False
        return hmac.compare_digest(candidate, self._token)


def _known_usage_point(
    request: Request, usage_points: dict[str, UsagePointSettings]
) -> UsagePointSettings:
    pdl = request.path_params["pdl"]
    if pdl not in usage_points:
        raise HTTPException(404, "unknown usage point")
    return usage_points[pdl]


def _date_range(request: Request) -> tuple[date, date]:
    try:
        start = date.fromisoformat(request.query_params["start"])
        end = date.fromisoformat(request.query_params["end"])
    except KeyError as exc:
        raise HTTPException(400, f"missing query parameter {exc.args[0]!r}") from exc
    except ValueError as exc:
        raise HTTPException(400, "start and end must be dates (YYYY-MM-DD)") from exc
    if not start < end:
        raise HTTPException(400, "start must be before end (end is exclusive)")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(400, f"ranges are limited to {MAX_RANGE_DAYS} days")
    return start, end


async def _http_error(request: Request, exc: HTTPException) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, exc.status_code)
    return PlainTextResponse(exc.detail, exc.status_code)
