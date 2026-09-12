# ADR 0002 — A quota governor reserves every call

Date: 2026-09-13 · Status: accepted

## Context

The gateway allows 50 calls per usage point per UTC day and relays Enedis
throttling as HTTP 429 (code 900804, with a `nextAccessTime`) or 409. Retrying
against a throttled endpoint can spend a whole daily budget within minutes, and
two processes — the daemon and a manual `releve sync` — could both pass a
budget check and overspend.

## Decision

- `QuotaGovernor.reserve` checks the active block and the day's count and
  records the call in one `BEGIN IMMEDIATE` transaction, before the call is
  sent; the HTTP status is attached afterwards. A call that crashes or fails in
  transit still counts. The default budget is 45 of 50, leaving headroom.
- An upstream refusal becomes a persisted block (`nextAccessTime` for 429, the
  next UTC midnight for 409), honored across restarts; a later block is never
  shortened.
- A refusal raises a typed error carrying `retry_at`. A quota refusal, a
  throttle, a refused token or an unreachable gateway stops the usage point
  concerned — never the others — and the schedule brings the next attempt.
  Retry loops are structurally impossible.
- An advisory lock next to the database allows one pass at a time.

## Consequences

- The budget holds across restarts and processes.
- `releve status`, the web page and `/metrics` can always say why nothing is
  happening, with a timestamp and a cause.
