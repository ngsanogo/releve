# ADR 0009 — Partially published load-curve days

Date: 2026-09-18 · Status: accepted · Amends ADRs [0004](0004-complete-history.md),
[0005](0005-export-cursors.md) and [0006](0006-home-assistant-series.md)

## Context

Enedis sometimes publishes a load-curve day partially and completes it later.
A day that counts as fetched as soon as it holds one point (ADR 0004) leaves
Home Assistant with that day's total at 23:00 for good.

A first fix put incomplete days back in the backlog and let a complete answer
replace a day's points. A review found that it:

- asked for a partial day on every pass, six times a day by default, although
  Enedis publishes once a day: it spent the budget and invited throttles, which
  hold back the daily data too;
- asked through the gateway's cache, which may serve the same partial day again;
- let a coarser complete grid, such as the hourly points of a half-hourly curve,
  erase a finer day, and let a partial answer break a complete day;
- rewrote unchanged days on every pass, and deleted points without the
  exporters knowing.

## Decision

- **A partial day rides along; it never costs a call.** The backlog is the days
  without data. Windows are planned for those days alone, then stretched, within
  the 7-day window limit, to take in the unsettled partial days they can reach.
  Every new day brings a missing day, yesterday, whose window reaches every
  unsettled day. So a partial day is asked for again at least once a day, at no
  cost, until it is `SETTLE_DAYS` old. After that it is kept as it is.
- **A partial day skips the gateway's cache.** A window holding a partial day is
  asked for without the gateway's cache, whatever `gateway.prefer_cache` says.
- **A day's curve only ever gets better.** `Store.upsert_curve` takes one usage
  point, direction and day at a time:
  - an answer that is not a complete grid is merged into a day that is not one
    either, and never touches a complete day;
  - a complete grid replaces the day, unless the day already holds a finer
    complete grid. Replacing updates the points that changed and drops the
    points that are not on the new grid.
- **A dropped point is a change.** It is journaled with the run that dropped it
  (`load_curve_removed`). Home Assistant rewrites its day. The InfluxDB exporter
  deletes it through `/api/v2/delete` before writing. A destination that refuses
  the deletion (VictoriaMetrics has no such endpoint) is reported and not asked
  again.
- **Home Assistant needs the daily totals.** A day whose curve is incomplete is
  exported from its daily total. With the Home Assistant exporter enabled,
  `consumption_detail` therefore requires `consumption`, and `production_detail`
  requires `production`. The configuration refuses the combination rather than
  export such days as 0 kWh.

Considered and rejected:

- *Retrying a partial day at most once a day*, with a timestamp per day: the
  retry needs a call of its own and new state, and it may come before Enedis
  publishes.
- *Planning partial days together with the missing days*, then dropping windows
  that hold no missing day: simpler, but it can shift the windows and cost an
  extra call while old history is still being fetched.
- *Reading Enedis' `interval_length`* to know each day's step: its presence in
  the gateway's answers is not verified.

## Consequences

- A partial day is complete the day after Enedis completes it. The gateway calls
  are the same with or without partial days.
- `releve status`, the dashboard and `/metrics` count the days still to fetch.
  Partial days are not among them.
- Fetching an unchanged day again changes nothing and exports nothing.
- A point that InfluxDB cannot delete stays there. The export summary says how
  many points remain.
