# ADR 0011 — Operated from its own commands, coupled by its API

Date: 2026-09-19 · Status: accepted

## Context

A week of running releve next to Home Assistant on a Raspberry Pi showed what
the deployment had to make up for:

- **No way to copy the database.** The deployment ran its own Python script
  inside the container to call SQLite's backup API: it had to know the image
  ships an interpreter, where the database is, and that a file copy of a WAL
  database proves nothing.
- **A journal that cried wolf.** Every night the first pass after midnight
  asked for a day Enedis had not published yet; the gateway answered 404 and
  releve journaled a failure — six nights out of six, each gone by the morning.
  The image listens on `0.0.0.0` by construction, so every start logged a
  warning about it. And a block was journaled in UTC (`until 09:00 UTC`) next to
  an event time in Paris time (`10:43`): it read as already over.
- **A version rule nobody checked.** The server and the integration were to be
  upgraded together, to the same version. Nothing verified it, and nothing
  needed it: the integration only reads `/api/v1`.

## Decision

- **`releve backup DEST`** is the one way to copy the database: SQLite's backup
  API, a single self-contained file (rollback-journal mode, no `-wal`), mode
  0600, kept only if `PRAGMA integrity_check` passes, never overwriting. It
  does not open the store the usual way, so it never creates nor migrates a
  database: a copy can be taken before an upgrade. `-` streams to standard
  output, for `docker exec releve releve backup - > releve.db`.
- **A 404 on unsettled days is not a failure.** `NotFoundError` (404) is told
  apart from `WindowRejectedError` (400). For days younger than the settle
  delay, a 404 is journaled as `(not published yet)` on a successful outcome
  and asked again at the next pass; a 400 stays a failure. Settled days become
  confirmed gaps either way, as before (ADR 0004).
- **One line, at the level of a fact, says who may read the web interface** at
  every start (`no authentication: open to whoever reaches it`), like
  `releve check`. Inside a container the published port decides who reaches it;
  releve cannot know, so it states and does not warn.
- **Every instant a person reads is in Paris time**, as the web page and
  `releve status` already were: the retry times in messages say `(Paris)`, and
  the image sets `TZ=Europe/Paris` so the log agrees with them.
- **`/api/v1` is the contract between the integration and the server**, not
  their version. They still ship from one tag (HACS installs a release tag, and
  the manifest carries it), but either side can be upgraded alone: the
  integration creates a sensor for each key the server publishes and ignores
  the rest. A breaking change to the API would be `/api/v2`.
- **The examples do what they advise**: `docker-compose.yaml` and the README pin
  the current release, and a test keeps them equal to the project version.

## Consequences

A deployment needs no knowledge of releve's insides: an image tag, a
configuration file, a data directory, and `releve backup`. What is wrong with
releve is fixed here, and reaches a deployment as a release. A warning in the
journal now means something to look at. Upgrading the server no longer implies
restarting Home Assistant.
