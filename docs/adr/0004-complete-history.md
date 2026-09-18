# ADR 0004 — Keep the history complete, not merely extended

Date: 2026-09-13 · Status: accepted

## Context

Extending the cache from its newest day onwards never revisits a day missing in
the middle, and replacing a window by its latest answer lets an answer holding
less data erase what the cache had.

## Decision

- Every pass computes the days of the history window (`sync.history_days`,
  capped by what Enedis keeps) that are still to fetch, and fetches them
  **newest first**, covering them with as few gateway windows as possible
  (365 days for daily data, 7 for the load curve).
- A day is still to fetch when it holds no data — or, for the load curve,
  when its points do not form a complete grid and the day is younger than
  `SETTLE_DAYS`. Enedis sometimes publishes a curve day partially and fills it
  in later; treating one point as enough would leave Home Assistant with that
  day's total at 23:00 forever.
- A day the gateway answers without data is retried until it is `SETTLE_DAYS`
  (7) old; then it is recorded as a *confirmed gap* and no longer asked for.
  A partial load-curve day that is still incomplete after `SETTLE_DAYS` is kept
  as it is (exporters fall back to the daily total).
- A window the gateway explicitly refuses (HTTP 400 or 404) is skipped so the
  older windows still get fetched. If all its days are settled, they become
  confirmed gaps; otherwise the refusal is reported and asked again later.
- Answers are clamped to their window (the gateway sometimes returns more) and
  upserted. A sync never deletes metering data.

## Consequences

- A fresh install shows recent data first; older history fills in over the
  following passes, within the budget.
- `releve status`, the dashboard and `/metrics` say how many days are still to
  fetch — including recent load-curve days that are only partially published.
