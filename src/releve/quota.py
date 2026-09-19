"""The quota governor: nothing reaches the gateway without its permission.

The gateway allows 50 calls per usage point per UTC day and relays Enedis
throttling. The governor guarantees, across restarts and across processes:

* a call is RESERVED in the database before it is sent, in the same
  transaction that checks the budget — two processes cannot overspend it, and
  a call that crashes or fails in transit still counts;
* an upstream refusal becomes a persisted block honored until its end;
* a refusal raises `QuotaExhaustedError` with `retry_at`: callers stop and let
  the schedule bring them back. Retry loops are structurally impossible.

Calls are counted per *bucket*: the usage point id, or `rte` for Tempo and
Ecowatt, which belong to no usage point.
"""

from __future__ import annotations

from datetime import datetime

from releve.clock import Clock, format_paris, next_utc_midnight, utc_midnight, utc_now
from releve.errors import QuotaExhaustedError
from releve.store import QuotaUsage, Refusal, Store

RTE_BUCKET = "rte"


class QuotaGovernor:
    def __init__(self, store: Store, daily_budget: int, clock: Clock = utc_now) -> None:
        self._store = store
        self.daily_budget = daily_budget
        self._clock = clock

    def reserve(self, bucket: str, endpoint: str) -> int:
        """Record a call about to be sent and return its id, or refuse it."""
        now = self._clock()
        outcome = self._store.reserve_call(
            bucket,
            endpoint,
            at=now,
            day_start=utc_midnight(now),
            resets_at=next_utc_midnight(now),
            budget=self.daily_budget,
        )
        if isinstance(outcome, Refusal):
            raise QuotaExhaustedError(
                f"{bucket}: {outcome.cause}; "
                f"next call allowed at {format_paris(outcome.until)} (Paris)",
                retry_at=outcome.until,
            )
        return outcome

    def settle(self, call_id: int, status: int | None) -> None:
        """Attach the HTTP status to a reserved call (None: no response came back)."""
        self._store.settle_call(call_id, status)

    def block(self, bucket: str, until: datetime, cause: str) -> None:
        self._store.block(bucket, until, cause)

    def usage(self, bucket: str) -> QuotaUsage:
        now = self._clock()
        return self._store.quota_usage(bucket, at=now, day_start=utc_midnight(now))
