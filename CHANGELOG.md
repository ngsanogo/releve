# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.1] - 2026-09-19

The first release published since 0.2.0. 0.3.0 was tagged but never published:
a test of the Home Assistant integration failed about one run in eight and
stopped its release, and a tag does not move. 0.3.1 is 0.3.0 with that test
fixed — what changed for users, schema migration included, is listed under 0.3.0.

### Fixed
- The integration's reauthentication test waits for the reload a successful
  reauthentication schedules, and checks the entry comes up with the new token.
  It ended while Home Assistant was still reloading, which left a timer behind.

## [0.3.0] - 2026-09-19

Tagged, not published — see 0.3.1.

This release has a schema migration (version 3): take a `releve backup` first —
with 0.2.0, which has no such command, stop releve and copy the database file.

### Added
- `releve backup DEST`: a consistent copy of the database through SQLite's
  backup API, safe while the daemon runs, checked with `PRAGMA integrity_check`,
  private (0600), never overwriting, and changing nothing in the database. `-`
  streams it to standard output (`docker exec releve releve backup - > copy.db`).
  The README says how to back up, restore and upgrade
  ([ADR 0011](docs/adr/0011-operated-from-its-own-commands.md)).
- Load-curve points dropped from the cache are journaled (schema version 3).
  Home Assistant rewrites their day, and the InfluxDB exporter deletes them
  through `/api/v2/delete`. A destination without that endpoint, such as
  VictoriaMetrics, is reported in the export summary.

### Changed
- Recent days the gateway holds nothing for yet (HTTP 404) are no longer a
  failure: the outcome succeeds and says `(not published yet)`, and the next
  pass asks again. Observed live, this was the first pass of every night. A
  refused window (HTTP 400) on recent days stays a failure; settled days become
  confirmed gaps either way.
- Retry times in messages are in Paris time and say so (`throttled upstream
  until 2026-09-19 11:00 (Paris)`), like every other instant releve shows; they
  were in UTC next to Paris event times. The image sets `TZ=Europe/Paris`, so
  the log agrees with them.
- The daemon no longer warns at every start when it listens beyond loopback
  without `web.auth_token` — the image always does, and there the published
  port decides. One line states it instead, as `releve check` does:
  `releve 0.3.0 — http://0.0.0.0:8080 (no authentication: open to whoever reaches it)`.
- The Home Assistant integration and the server no longer have to run the same
  version: `/api/v1` is their contract, and either side can be upgraded alone.
- `docker-compose.yaml` and the README pin the current release instead of
  `:latest`; a test keeps them equal to the project version.
- Partially published load-curve days ride along in windows planned for missing
  days: they cost no call of their own, skip the gateway's cache, and no longer
  count among the days still to fetch in `releve status`, the dashboard and
  `/metrics` ([ADR 0009](docs/adr/0009-partial-load-curve-days.md)).
- A day's cached load curve only ever gets better: an answer that is not a
  complete grid never touches a complete day, a coarser grid never replaces a
  finer one, and fetching an unchanged day again changes and exports nothing.
- With the Home Assistant exporter enabled, a configuration with
  `consumption_detail` but not `consumption`, or `production_detail` but not
  `production`, is refused. A day whose load curve is incomplete is exported
  from its daily total; without one, it was exported as 0 kWh.

## [0.2.0] - 2026-09-18

### Added
- A Home Assistant integration, installable with HACS (`custom_components/releve`,
  `hacs.json`): set up from the UI with releve's URL (and `web.auth_token` if
  set), it shows each usage point's state and the grid signals as native
  sensors — no MQTT broker needed. Tested against Home Assistant 2026.9 with
  `hassfest`, the HACS validation and `pytest-homeassistant-custom-component`.
- `GET /api/v1/usage-points`, `GET /api/v1/usage-points/{pdl}/state` and
  `GET /api/v1/rte/state`: the configured usage points and the state MQTT
  publishes, as JSON. The state is computed in one place (`releve.state`) for
  both.
- Full MyElectricalData coverage beyond metering: `valid_access` (consent),
  contracts, identity, contact, addresses, Tempo season and prices, hourly
  Ecowatt detail, and `DELETE` remote-cache endpoints (`releve purge-cache`).
- Local cache tables for consent, contract, customer data, Tempo season/prices
  and hourly Ecowatt; weekly customer refresh; consent checked on every pass that fetches;
  Tempo season and prices refreshed once a day.
- Peak/off-peak energy on MQTT when the contract and load curve allow it;
  JSON API routes for consent/contract/customer/curve/max-power/Tempo extras.
- Importable Grafana dashboard for InfluxDB in `contrib/grafana/`.
- `GET /api/v1/rte/ecowatt/hours`: the hourly Ecowatt signal of a range of Paris days.
- A contract or customer resource the gateway cannot give is asked again a day
  later rather than on every pass, and shown on the usage point page.
- Shared load-curve arithmetic (`curve.py`) and tariff helpers (`tariffs.py`).

### Changed
- Consent: an answer without a readable `valid` or `ban` flag is refused; an
  unreadable informational field (call count, quota, dates) is logged and left
  unknown instead of stopping metering.

### Fixed
- A load-curve day published only partially was never asked for again, so Home
  Assistant kept that day as a single total at 23:00. It is now fetched again
  until its curve is complete or the day is settled (7 days). After upgrading,
  recent incomplete days re-enter the backlog and may use a few of the daily
  gateway calls. A complete-grid answer replaces that day's points so an
  irregular earlier answer cannot poison the day.
- Daily Ecowatt signals were dated one day early: the gateway keys each day by
  the day before. Days are now dated by their hourly detail, the range asked is
  shifted accordingly, and upgrading moves every cached Ecowatt day — including
  those imported from the legacy layout — one day later.

## [0.1.0] - 2026-09-13

First release.

- Quota-aware client for the MyElectricalData gateway: every call reserved in
  the database before it is sent, persisted upstream blocks, one pass at a time.
- Local SQLite cache kept complete over the history window — daily energy,
  30-minute load curve, daily peak power, Tempo and Ecowatt — with confirmed gaps.
- Exporters that resume from a cursor: Home Assistant long-term statistics
  (hourly, continuing existing series after a pinned boundary), MQTT with
  discovery, InfluxDB/VictoriaMetrics line protocol.
- Read-only web interface, JSON API, Prometheus metrics, health check, optional
  token authentication.
- CLI: `init`, `check`, `sync`, `status`, `serve`, `ha-boundary`, `version`.
- One-way import of databases in the earlier SQLAlchemy layout, with a backup.
- Container image for amd64 and arm64.

[Unreleased]: https://github.com/ngsanogo/releve/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/ngsanogo/releve/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ngsanogo/releve/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/ngsanogo/releve/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ngsanogo/releve/releases/tag/v0.1.0
