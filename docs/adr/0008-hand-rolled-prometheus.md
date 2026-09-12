# ADR 0008 — Hand-rolled Prometheus exposition

Date: 2026-09-13 · Status: accepted

## Context

Operators need to alert on quota use, blocks, backlog and on a daemon that
stopped succeeding. That is a handful of gauges.

## Decision

`metrics.render_metrics()` writes the
[Prometheus text format](https://prometheus.io/docs/instrumenting/exposition_formats/)
as a string, from snapshots of the store. No client library.

## Consequences

- One file, one function, easy to explain.
- If the metric surface grows (histograms, exemplars), adopt `prometheus_client`.
