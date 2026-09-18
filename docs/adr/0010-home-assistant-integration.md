# ADR 0010 — A Home Assistant integration over the JSON API

Date: 2026-09-18 · Status: accepted

(0009 is reserved by a pending change on load-curve days.)

## Context

releve reached Home Assistant two ways: long-term statistics (the
`home_assistant` exporter) and MQTT with discovery. Without a broker there were
no sensors at all, and installing releve's side of Home Assistant was not
something HACS could do.

## Decision

- **A HACS integration in the same repository**, `custom_components/releve`,
  released with the same version as the server (a test holds them equal). HACS
  installs a release tag; the integration it gets matches the server of that tag.
- **A client of the JSON API, nothing else.** It never imports the `releve`
  package and never talks to the gateway: three read-only routes —
  `/api/v1/usage-points`, `/api/v1/usage-points/{pdl}/state`,
  `/api/v1/rte/state` — polled every ten minutes by one coordinator. The
  quota stays governed in one place.
- **One definition of the state.** `releve.state` computes it once for MQTT and
  for the API, so both publish the same keys, units and nulls.
- **Sensors follow the published keys**: a dataset turned off in releve has no
  key, hence no entity. Rolling totals carry no `state_class`: statistics remain
  the exporter's job (ADR 0006), and two writers of one series would fight.
- **Tested against a pinned Home Assistant**, in its own environment
  (`tests_ha/`, Python 3.14): Home Assistant pins its own versions of libraries
  releve also uses, so the two cannot share a lock file. CI runs `hassfest`, the
  HACS validation (the `brands` check aside: the domain is not in
  home-assistant/brands), mypy strict and the tests.

Considered and rejected: moving the statistics import into the integration (it
would duplicate ADR 0006's boundary logic in a second runtime); reading the
SQLite file directly from Home Assistant (couples the integration to the
schema, and to a shared filesystem).

## Consequences

A user without MQTT gets native sensors from the UI. The integration only needs
the server to be reachable over HTTP, with `web.auth_token` when set. Adding a
usage point in releve needs a reload of the integration to get its entities.
