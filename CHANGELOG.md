# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
