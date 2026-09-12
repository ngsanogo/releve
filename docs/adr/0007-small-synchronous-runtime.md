# ADR 0007 — A small, synchronous runtime

Date: 2026-09-13 · Status: accepted

## Context

The workload is a handful of HTTP calls every few hours for one or a few usage
points, plus a read-only status page. There is no concurrency problem to solve,
so none of the machinery for one is worth its complexity, its install size or
its maintenance.

## Decision

- One process, one package, flat modules with one-way dependencies
  (architecture.md). No plugin system: an exporter is one module and one line in
  `build_exporters`.
- Synchronous code; passes run on one background thread.

| Need | Choice |
|---|---|
| Database | standard-library `sqlite3` ([ADR 0003](0003-sqlite-store.md)) |
| Web | Starlette + Jinja2 on plain `uvicorn` |
| Scheduling | one thread and a `threading.Event` |
| CLI | standard-library `argparse` |
| HTTP client | `httpx2` (tests use its `MockTransport`) |
| Home Assistant | `websockets` (typed, sync client) |
| MQTT | `paho-mqtt` |
| Configuration | `pydantic-settings` with YAML |

Deliberately absent: error-tracking services, telemetry, rate limiting, ETags,
metric label hashing, `.env` loading. Each solves a problem a single-household,
self-hosted daemon does not have, or that a reverse proxy solves better. Token
authentication is built in, accepting Bearer and HTTP Basic so browsers work.

## Consequences

- About 20 MB of dependencies, every one of them typed.
- Every code path is a stack trace a human can read top to bottom.
