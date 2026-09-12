"""The daemon's heartbeat: run a job every interval on one background thread.

The scheduler decides WHEN a pass runs, never WHETHER a call may go out (that
is the quota governor's job). A crashing pass is logged with its traceback and
the loop carries on: a daemon that dies on the first bug would silently stop
collecting data.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from releve.clock import Clock, utc_now

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        job: Callable[[], None],
        interval: timedelta,
        *,
        first_delay: timedelta = timedelta(seconds=5),
        clock: Clock = utc_now,
    ) -> None:
        self._job = job
        self._interval = interval
        self._first_delay = first_delay
        self._clock = clock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="releve-sync", daemon=True)
        self.started_at: datetime | None = None
        self.last_pass_started: datetime | None = None
        self.last_pass_finished: datetime | None = None
        self.last_pass_crashed = False

    def start(self) -> None:
        self.started_at = self._clock()
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def is_healthy(self) -> bool:
        """Alive, the last pass did not crash, and none is overdue or stuck for two intervals."""
        if not self._thread.is_alive() or self.started_at is None or self.last_pass_crashed:
            return False
        reference = self.last_pass_started or self.started_at + self._first_delay
        return self._clock() - reference <= 2 * self._interval + timedelta(minutes=15)

    def _loop(self) -> None:
        delay = self._first_delay
        while not self._stop.wait(delay.total_seconds()):
            self.last_pass_started = self._clock()
            try:
                self._job()
            except Exception:  # explicitly silenced: the daemon must outlive a bad pass
                log.exception("sync pass crashed")
                self.last_pass_crashed = True
            else:
                self.last_pass_crashed = False
            self.last_pass_finished = self._clock()
            delay = self._interval
