# ADR 0006 — Home Assistant series: pinned boundary, hourly rows

Date: 2026-09-13 · Status: accepted, amended by [ADR 0009](0009-partial-load-curve-days.md)

## Context

Home Assistant statistics are cumulative sums. A series may continue one that an
earlier integration wrote for years, and that archive must never be damaged.

## Decision

- **Boundary.** The first export of a series asks Home Assistant for its last
  hourly point, in kWh (the last month holding data, then the last hour within
  it), and pins that (day, sum) in the database, for this usage point, before
  importing anything. Days after it are owned; nothing at or before it is ever
  written. A failed or unexpected answer is an error — never an import from
  zero. A series pinned for another usage point is refused. Boundaries imported
  from the legacy database layout are kept as they are, and adopted by the first
  usage point that exports to them.
- **Explicit re-pinning.** `releve ha-boundary --set` pins a boundary and, in
  the same transaction, drops the Home Assistant export cursors, so the next
  delivery rewrites everything after the new boundary instead of leaving a step
  in the sums.
- **Hourly rows for every owned day**, from the first to the last day with data,
  days without data included (flat). A day whose load curve is a complete,
  regular grid (N points ending at start + k·step, N·step = the day's 23, 24 or
  25 hours, step dividing an hour) gets its measured hourly energy — the curve's
  own total. Any other day with a daily total is written flat until 23:00, where
  the day's total lands. Writing all hours makes a change of granularity
  overwrite every row it affects.
- **23:00, fixed.** A daily total is known at the end of the day; stamping it
  at 00:00 would claim the whole day was consumed in its first hour.
- **Incremental.** Only series with changes since the exporter's cursor are
  rewritten, from their earliest changed day.
- **Metadata by version.** Home Assistant 2025.11 introduced `mean_type` and
  `unit_class`, rejects them before, and stops accepting their absence in
  2026.11: the exporter reads `ha_version` from the authentication reply and
  sends the matching form.

Considered and rejected: scaling the curve's hourly energy to the daily total
(it would alter the sums of an existing archive), and accepting nearly complete
curves (a guess about data that is not there).

## Consequences

A day whose curve is incomplete is exported as a daily total: exact, less
detailed. Back up Home Assistant before pointing releve at a series you care
about.
