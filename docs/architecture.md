# Architecture

One process, one package, flat modules with one-way dependencies. A *sync
pass* moves data from the gateway to the cache; exporters and the web page read
the cache. Nothing reads the gateway except the pass.

```
clock, errors, domain          the vocabulary (no dependencies)
        │
config ─┤
        │
store ──┴── legacy             SQLite; one-way import of the legacy layout
  │
quota                          reservations and blocks, on the store
  │
gateway                        HTTP + parsing, every call reserved first
  │
planning                       pure: which days, which windows
  │
exporters/                     home_assistant, mqtt, influxdb — read the store
  │
sync                           one pass: rte, usage points, exporters, journal
  │
metrics, scheduler, web        observe the store; run passes; serve pages
  │
cli                            composition root
```

| Module | Responsibility |
|---|---|
| `clock.py` | The time vocabulary: aware datetimes, Paris civil days, unix seconds |
| `domain.py` | Frozen value objects: `DailyEnergy`, `LoadCurvePoint`, `PowerPeak`, `Dataset`… |
| `errors.py` | Every error raised on purpose |
| `config.py` | YAML + environment settings, strict validation, upgrade hints |
| `store.py` | The only SQL: schema migrations, metering upserts, change feed, quota bookkeeping |
| `migrations/` | Numbered schema scripts; `PRAGMA user_version` records the last applied |
| `legacy.py` | One-way import of a database in the earlier SQLAlchemy layout |
| `quota.py` | `QuotaGovernor`: reserve before sending, settle after, honor blocks |
| `gateway.py` | The only HTTP to the gateway, and the parsing of its answers |
| `curve.py` | Load-curve arithmetic, one series day at a time: complete grids, energy per interval |
| `tariffs.py` | Off-peak hours from the contract; energy split by period |
| `planning.py` | Missing days, fetch windows, settled gaps — pure functions |
| `sync.py` | One pass; the pass lock; the journal; exporter cursors |
| `exporters/` | Home Assistant statistics, MQTT with discovery, Influx line protocol |
| `scheduler.py` | A thread that runs a pass every interval and reports its health |
| `metrics.py` | Prometheus text exposition |
| `web/` | Starlette app: dashboard, JSON API, `/metrics`, `/healthz`, token auth |
| `cli.py` | argparse commands; wires everything together |

## The rules that keep it simple

1. **Every gateway call is reserved first.** `QuotaGovernor.reserve` checks the
   block and the budget and records the call in one `BEGIN IMMEDIATE`
   transaction. There is no other way to reach the gateway.
2. **Parsing happens once**, in `gateway.py`. Everything downstream receives
   domain objects it can trust.
3. **All datetimes are aware.** Instants are stored as unix seconds (UTC); Paris
   civil days as ISO dates. Load-curve points are stamped at the END of their
   interval: a point ending at midnight belongs to the day before.
4. **The cache is the source of truth.** Answers are upserted; one holding
   less never erases what the cache has, and a load-curve day only ever gets
   better. A row carries the id of the run that last changed it, and a dropped
   load-curve point is journaled with the run that dropped it; exporters deliver
   the changes after their cursor and advance it only on success.
5. **One pass at a time per database**, across processes, with an advisory
   lock next to the database file.
6. **Errors are typed and journaled.** A pass records one outcome per subject
   (`rte`, each usage point, `export:<name>`); the scheduler logs a crashing
   pass with its traceback and keeps running.

## Decisions

The ADRs record why things are the way they are:

- [0001 — Apache-2.0, written from scratch](adr/0001-apache-license.md)
- [0002 — A quota governor reserves every call](adr/0002-quota-governor.md)
- [0003 — Standard-library SQLite store](adr/0003-sqlite-store.md)
- [0004 — Keep the history complete, not merely extended](adr/0004-complete-history.md)
- [0005 — Exporters read the cache through cursors](adr/0005-export-cursors.md)
- [0006 — Home Assistant series: pinned boundary, hourly rows](adr/0006-home-assistant-series.md)
- [0007 — A small, synchronous runtime](adr/0007-small-synchronous-runtime.md)
- [0008 — Hand-rolled Prometheus exposition](adr/0008-hand-rolled-prometheus.md)
- [0009 — Partially published load-curve days](adr/0009-partial-load-curve-days.md)
