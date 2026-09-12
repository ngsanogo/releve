-- releve schema, version 1.
-- Instants are unix seconds (UTC). Days are ISO dates of the Paris civil day.

-- One row per sync pass. Its id doubles as the change id stamped on every
-- metering row the pass changes; AUTOINCREMENT guarantees ids are never reused.
CREATE TABLE run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER
) STRICT;

-- What a pass did, per subject: a usage point, "rte", or "export:<name>".
CREATE TABLE run_event (
    id      INTEGER PRIMARY KEY,
    run_id  INTEGER NOT NULL REFERENCES run (id) ON DELETE CASCADE,
    at      INTEGER NOT NULL,
    subject TEXT    NOT NULL,
    ok      INTEGER NOT NULL CHECK (ok IN (0, 1)),
    detail  TEXT    NOT NULL
) STRICT;
CREATE INDEX run_event_subject ON run_event (subject, ok, at);
CREATE INDEX run_event_run ON run_event (run_id);

-- Every gateway call, reserved BEFORE it is sent. `status` is the HTTP status,
-- NULL when no response came back (transport failure or still in flight).
CREATE TABLE gateway_call (
    id          INTEGER PRIMARY KEY,
    bucket      TEXT    NOT NULL,
    endpoint    TEXT    NOT NULL,
    reserved_at INTEGER NOT NULL,
    status      INTEGER
) STRICT;
CREATE INDEX gateway_call_bucket ON gateway_call (bucket, reserved_at);

-- Upstream refusals: no call for `bucket` before `until`.
CREATE TABLE quota_block (
    bucket TEXT    PRIMARY KEY,
    until  INTEGER NOT NULL,
    cause  TEXT    NOT NULL
) STRICT;

CREATE TABLE daily_energy (
    usage_point TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('consumption', 'production')),
    day         TEXT    NOT NULL,
    wh          INTEGER NOT NULL,
    run_id      INTEGER NOT NULL,
    PRIMARY KEY (usage_point, direction, day)
) STRICT, WITHOUT ROWID;
CREATE INDEX daily_energy_run ON daily_energy (run_id);

-- `end_at` is the END of the metering interval; `day` the Paris day it belongs to.
CREATE TABLE load_curve (
    usage_point TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('consumption', 'production')),
    end_at      INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    watts       INTEGER NOT NULL,
    run_id      INTEGER NOT NULL,
    PRIMARY KEY (usage_point, direction, end_at)
) STRICT, WITHOUT ROWID;
CREATE INDEX load_curve_day ON load_curve (usage_point, direction, day);
CREATE INDEX load_curve_run ON load_curve (run_id);

CREATE TABLE power_peak (
    usage_point TEXT    NOT NULL,
    day         TEXT    NOT NULL,
    va          INTEGER NOT NULL,
    at          INTEGER NOT NULL,
    run_id      INTEGER NOT NULL,
    PRIMARY KEY (usage_point, day)
) STRICT, WITHOUT ROWID;
CREATE INDEX power_peak_run ON power_peak (run_id);

-- Days the gateway answered WITHOUT data, long enough after the fact that no
-- data will ever come: the planner stops asking for them.
CREATE TABLE confirmed_gap (
    usage_point TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    day         TEXT NOT NULL,
    PRIMARY KEY (usage_point, dataset, day)
) STRICT, WITHOUT ROWID;

CREATE TABLE tempo_day (
    day   TEXT PRIMARY KEY,
    color TEXT NOT NULL
) STRICT;

CREATE TABLE ecowatt_day (
    day     TEXT    PRIMARY KEY,
    level   INTEGER NOT NULL,
    message TEXT    NOT NULL
) STRICT;

-- The last run whose changes an exporter delivered.
CREATE TABLE export_cursor (
    sink        TEXT    PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    exported_at INTEGER NOT NULL
) STRICT;

-- Where our Home Assistant series begins: the last (day, sum) Home Assistant
-- held before our first import, pinned once. Days after `base_day` are ours,
-- and belong to `usage_point` (NULL until the first export adopts it).
CREATE TABLE ha_boundary (
    statistic_id TEXT PRIMARY KEY,
    usage_point  TEXT,
    base_day     TEXT,
    base_sum     REAL NOT NULL
) STRICT;
