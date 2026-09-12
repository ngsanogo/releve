# ADR 0003 — Standard-library SQLite store

Date: 2026-09-13 · Status: accepted

## Context

The cache holds a few tens of thousands of rows per meter, written in small
transactions (one row per gateway call, upserts per window), read by a web page
and a CLI while the daemon runs. It must never depend on the working directory,
and its timestamps must be unambiguous across daylight-saving changes.

## Decision

- One SQLite file through the standard library's `sqlite3`: WAL journal, a
  short-lived connection per operation, `BEGIN IMMEDIATE` for writes, `STRICT`
  tables. No ORM.
- `storage.path` is an absolute path; the default follows XDG
  (`~/.local/state/releve/releve.db`). The file is created `0600` in a `0700`
  directory. A new database is refused next to an existing cache.
- Schema changes are numbered scripts in `migrations/`; `PRAGMA user_version`
  records the last one applied; a newer database is refused.
- Instants are unix seconds (UTC); Paris civil days are ISO dates. The load
  curve is keyed by its UTC interval end.
- A database in the earlier SQLAlchemy layout is imported once, in one
  transaction, after a backup copy.

Considered and rejected:

- *DuckDB*: an analytical engine for a transactional workload; only one process
  may open the file, which would break `releve status` while the daemon runs;
  tens of megabytes of native code. It can still read this file.
- *An ORM with several database backends*: portability nobody self-hosting
  needs, at the cost of an untested promise.
- *Alembic*: with one backend and hand-written SQL, numbered scripts say the
  same thing with less machinery.

## Consequences

The store is the only module with SQL; every query is visible in one file.
