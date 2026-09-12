"""The failure vocabulary.

Every error this package raises on purpose derives from `ReleveError`.
Anything else escaping a function is a bug and is allowed to crash loudly.
"""

from __future__ import annotations

from datetime import datetime


class ReleveError(Exception):
    """Base class for every error raised on purpose by this package."""


class ConfigError(ReleveError):
    """The configuration file or environment is invalid."""


class StoreError(ReleveError):
    """The local database cannot be used as it is."""


class SyncAlreadyRunningError(ReleveError):
    """Another process is already running a sync pass on the same database."""


class GatewayError(ReleveError):
    """The MyElectricalData gateway did not give a usable answer."""


class GatewayUnreachableError(GatewayError):
    """No answer came back (connection refused, timeout, TLS failure...)."""


class WindowRejectedError(GatewayError):
    """The gateway refused the requested date window itself (HTTP 400 or 404)."""


class AuthError(GatewayError):
    """The gateway refused the token for this usage point (HTTP 401/403)."""


class RetryLaterError(GatewayError):
    """No call may be sent before `retry_at`."""

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class QuotaExhaustedError(RetryLaterError):
    """Refused LOCALLY by the quota governor — nothing was sent upstream."""


class ThrottledError(RetryLaterError):
    """Refused UPSTREAM — the gateway or Enedis throttled the call."""


class ExportError(ReleveError):
    """An exporter could not deliver."""
