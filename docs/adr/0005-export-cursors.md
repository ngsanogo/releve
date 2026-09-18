# ADR 0005 — Exporters read the cache through cursors

Date: 2026-09-13 · Status: accepted, amended by [ADR 0009](0009-partial-load-curve-days.md)

## Context

An exporter that only sends what the current pass fetched loses data whenever
its destination is down during that pass.

## Decision

- A pass is a *run* with an id. Every metering row carries the id of the run
  that last **changed** it (an identical answer does not bump it).
- Each exporter has a cursor, keyed by its destination (`sink`): the last run
  whose changes it delivered. A pass hands it `after_run < run_id <= up_to_run`
  and advances the cursor only on success.
- Changing an exporter's destination creates a new sink, which starts with a
  full delivery.

## Consequences

- A destination that was down receives everything it missed on its next success.
- Enabling an exporter on an existing cache backfills it.
- MQTT state is time-dependent ("yesterday") and is recomputed from the cache
  on every pass; it ignores its cursor.
