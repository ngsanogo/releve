# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- Load-curve points dropped from the cache are journaled (schema version 3).
  Home Assistant rewrites their day, and the InfluxDB exporter deletes them
  through `/api/v2/delete`. A destination without that endpoint, such as
  VictoriaMetrics, is reported in the export summary.

### Changed
- Consent: an answer without a readable `valid` or `ban` flag is refused; an
  unreadable informational field (call count, quota, dates) is logged and left
  unknown instead of stopping metering.
- With the Home Assistant exporter enabled, `consumption_detail` needs
  `consumption`, and `production_detail` needs `production`. A day whose load
  curve is incomplete is exported from its daily total, and was exported as
  0 kWh when there was no daily total.

### Fixed
- A load-curve day published only partially was never asked for again, so Home
  Assistant kept that day as a single total at 23:00. Until the day is settled
  (7 days), it is now asked for again inside the calls made for missing days,
  which include yesterday every day, so it costs no call of its own. It is asked
  without the gateway's cache, whose copy may be the same partial day.
- A day's cached load curve now only gets better. An answer that is not a
  complete grid never touches a complete day. A complete grid replaces the day,
  dropping points that are not on it, unless the day already holds a finer
  grid. Fetching an unchanged day again changes nothing.
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

[Unreleased]: https://github.com/ngsanogo/releve/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ngsanogo/releve/releases/tag/v0.1.0
