# ADR 0004 — Keep the history complete, not merely extended

Date: 2026-09-13 · Status: accepted, amended by [ADR 0009](0009-partial-load-curve-days.md)

## Context

Extending the cache from its newest day onwards never revisits a day missing in
the middle, and replacing a window by its latest answer lets an answer holding
less data erase what the cache had.

## Decision

- Every pass computes the days of the history window (`sync.history_days`,
  capped by what Enedis keeps) that hold no data, and fetches them **newest
  first**, covering them with as few gateway windows as possible (365 days for
  daily data, 7 for the load curve).
- A day the gateway answers without data is retried until it is `SETTLE_DAYS`
  (7) old; then it is recorded as a *confirmed gap* and no longer asked for.
- A window the gateway explicitly refuses (HTTP 400 or 404) is skipped so the
  older windows still get fetched. If all its days are settled, they become
  confirmed gaps; otherwise the refusal is reported and asked again later.
- Answers are clamped to their window (the gateway sometimes returns more) and
  upserted. How a load-curve day may replace or drop points is in
  [ADR 0009](0009-partial-load-curve-days.md).

## Consequences

- A fresh install shows recent data first; older history fills in over the
  following passes, within the budget.
- `releve status`, the dashboard and `/metrics` say how many days are still to
  fetch (days with no data). Partially published load-curve days are covered by
  [ADR 0009](0009-partial-load-curve-days.md).
