"""Prometheus text exposition, written by hand for a handful of gauges.

Everything here is a snapshot of the store: quota use, data freshness, the
backlog still to fetch, and when each subject last succeeded — the metric to
alert on when the daemon stops doing its job.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from releve import __version__
from releve.clock import Clock, day_start, paris_today, to_unix, utc_now
from releve.config import Settings
from releve.quota import RTE_BUCKET, QuotaGovernor
from releve.store import Store
from releve.sync import backlog


@dataclass
class Family:
    name: str
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)

    def add(self, value: float, **labels: str) -> None:
        self.samples.append((labels, value))

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        for labels, value in self.samples:
            rendered = ",".join(f'{key}="{_escape(text)}"' for key, text in labels.items())
            yield f"{self.name}{{{rendered}}} {_number(value)}"


def render_metrics(
    settings: Settings, store: Store, governor: QuotaGovernor, clock: Clock = utc_now
) -> str:
    today = paris_today(clock())
    info = Family("releve_build_info", "Version of the running releve.")
    info.add(1, version=__version__)
    calls = Family("releve_gateway_calls_today", "Gateway calls counted today (UTC day).")
    budget = Family("releve_gateway_daily_budget", "Configured daily call budget.")
    blocked = Family(
        "releve_gateway_blocked_until_timestamp_seconds",
        "End of the active quota block; absent when calls are allowed.",
    )
    latest = Family(
        "releve_latest_day_timestamp_seconds", "Start of the newest cached day, per dataset."
    )
    missing = Family(
        "releve_missing_days", "Days of the history window still to fetch, per dataset."
    )
    success = Family(
        "releve_last_success_timestamp_seconds",
        "When a usage point, rte or an exporter last succeeded.",
    )

    buckets = [up.id for up in settings.usage_points]
    if settings.sync.rte_signals:
        buckets.append(RTE_BUCKET)
    for bucket in buckets:
        usage = governor.usage(bucket)
        calls.add(usage.used, bucket=bucket)
        budget.add(governor.daily_budget, bucket=bucket)
        if usage.blocked_until is not None:
            blocked.add(to_unix(usage.blocked_until), bucket=bucket)

    for up in settings.usage_points:
        for dataset in up.datasets:
            newest = store.latest_day(up.id, dataset)
            if newest is not None:
                latest.add(to_unix(day_start(newest)), usage_point=up.id, dataset=dataset)
            todo = backlog(store, up.id, dataset, settings.sync.history_days, today)
            missing.add(len(todo), usage_point=up.id, dataset=dataset)

    for subject, moment in sorted(store.last_success().items()):
        success.add(to_unix(moment), subject=subject)

    families = (info, calls, budget, blocked, latest, missing, success)
    return "\n".join(line for family in families for line in family.render()) + "\n"


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
