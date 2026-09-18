"""Weeks of simulated operation: passes never overspend and the cache converges."""

from __future__ import annotations

import sqlite3
import tempfile
from collections import Counter
from collections.abc import Collection, Sequence
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from releve.clock import paris_today
from releve.config import Settings
from releve.planning import SETTLE_DAYS
from releve.quota import QuotaGovernor
from releve.store import Store
from releve.sync import backlog, run_pass
from tests.conftest import NOW, PDL, FrozenClock, make_settings
from tests.fakes import FakeGateway

PASSES_PER_DAY = 4
QUIET_DAYS = 15


def simulate(
    config: Settings,
    budget: int,
    holes: Collection[date],
    partial_curves: Collection[date],
    delays: Sequence[int],
) -> tuple[Store, FakeGateway, date]:
    """Run passes for days on end; returns the store, the gateway and the last pass's day.

    Partial curves are served as such on the first day only: then Enedis completes them.
    """
    clock = FrozenClock(NOW)
    store = Store.open(config.storage.path, clock)
    governor = QuotaGovernor(store, budget, clock)
    gateway = FakeGateway(
        governor,
        clock,
        published_until=paris_today(NOW),
        holes=set(holes),
        partial_curves=set(partial_curves),
    )
    for delay in [*delays, *[0] * QUIET_DAYS]:
        gateway.published_until = paris_today(clock()) - timedelta(days=delay)
        for _ in range(PASSES_PER_DAY):
            run_pass(config, gateway, store, [], clock)
            clock.advance(timedelta(hours=24 / PASSES_PER_DAY))
        gateway.partial_curves.clear()
    # The last pass ran one step before the clock's current time.
    return store, gateway, paris_today(clock() - timedelta(hours=24 / PASSES_PER_DAY))


@settings(max_examples=20, deadline=None)
@given(
    history=st.integers(min_value=1, max_value=40),
    # Enough for yesterday's three datasets plus some backlog: with less, old history
    # legitimately starves behind the new days, newest first.
    budget=st.integers(min_value=5, max_value=12),
    curve=st.booleans(),
    hole_offsets=st.sets(st.integers(min_value=1, max_value=45), max_size=6),
    partial_offsets=st.sets(st.integers(min_value=1, max_value=SETTLE_DAYS), max_size=3),
    delays=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=10),
)
def test_passes_never_overspend_and_eventually_complete_the_cache(
    history: int,
    budget: int,
    curve: bool,
    hole_offsets: set[int],
    partial_offsets: set[int],
    delays: list[int],
) -> None:
    start_day = paris_today(NOW)
    holes = {start_day - timedelta(days=offset) for offset in hole_offsets}
    partial = {start_day - timedelta(days=offset) for offset in partial_offsets}
    with tempfile.TemporaryDirectory() as directory:

        def config(name: str) -> Settings:
            point = {"id": PDL, "max_power": True, "consumption_detail": curve, "contract": False}
            database = Path(directory) / name / "cache.db"
            return make_settings(database, usage_points=[point], sync={"history_days": history})

        store, gateway, today = simulate(config("partial"), budget, holes, partial, delays)
        # The same weeks without partial curves make the very same calls: a partial
        # day never costs a call of its own.
        _, plain, _ = simulate(config("plain"), budget, holes, set(), delays)
        assert [call[:2] for call in gateway.calls] == [call[:2] for call in plain.calls]

        with closing(sqlite3.connect(store.path)) as conn, conn:
            reserved = [row[0] for row in conn.execute("SELECT reserved_at FROM gateway_call")]
            calls_per_utc_day = Counter(datetime.fromtimestamp(at, UTC).date() for at in reserved)
        assert max(calls_per_utc_day.values(), default=0) <= budget

        recent_days = (today - timedelta(days=n) for n in range(1, 4))
        unpublished = {day for day in recent_days if day >= gateway.published_until}
        for dataset in config("partial").usage_points[0].datasets:
            remaining = set(backlog(store, PDL, dataset, history, today))
            assert remaining <= holes | unpublished, dataset
            recent = today - timedelta(days=SETTLE_DAYS)
            assert all(day > recent for day in remaining - unpublished), dataset
