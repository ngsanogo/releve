-- releve schema, version 2: consent, contract, customer data, Tempo season and
-- prices, hourly Ecowatt; daily Ecowatt dated correctly.

-- The gateway's view of a usage point's consent and quota, checked on every
-- pass that fetches something for the usage point.
CREATE TABLE consent (
    usage_point    TEXT    PRIMARY KEY,
    checked_at     INTEGER NOT NULL,
    valid          INTEGER NOT NULL CHECK (valid IN (0, 1)),
    expires_at     INTEGER,
    call_number    INTEGER,
    quota_limit    INTEGER,
    quota_reached  INTEGER NOT NULL CHECK (quota_reached IN (0, 1)),
    quota_reset_at INTEGER,
    last_call_at   INTEGER,
    banned         INTEGER NOT NULL CHECK (banned IN (0, 1)),
    information    TEXT    NOT NULL
) STRICT;

-- The distribution contract of a usage point, refreshed weekly. Values are kept
-- as Enedis publishes them (e.g. subscribed_power "9 kVA", offpeak_hours
-- "HC (22H00-6H00)").
CREATE TABLE contract (
    usage_point             TEXT    PRIMARY KEY,
    fetched_at              INTEGER NOT NULL,
    segment                 TEXT,
    subscribed_power        TEXT,
    distribution_tariff     TEXT,
    offpeak_hours           TEXT,
    contract_status         TEXT,
    last_activation_date    TEXT,
    last_tariff_change_date TEXT,
    meter_type              TEXT,
    usage_point_status      TEXT
) STRICT;

-- Account holder identity (PII). Fetched only when configured.
CREATE TABLE identity (
    usage_point TEXT PRIMARY KEY,
    fetched_at  INTEGER NOT NULL,
    customer_id TEXT,
    title       TEXT,
    firstname   TEXT,
    lastname    TEXT
) STRICT;

-- Account holder contact details (PII). Fetched only when configured.
CREATE TABLE contact (
    usage_point TEXT PRIMARY KEY,
    fetched_at  INTEGER NOT NULL,
    customer_id TEXT,
    phone       TEXT,
    email       TEXT
) STRICT;

-- Usage-point address. Fetched only when configured.
CREATE TABLE address (
    usage_point        TEXT PRIMARY KEY,
    fetched_at         INTEGER NOT NULL,
    customer_id        TEXT,
    street             TEXT,
    locality           TEXT,
    postal_code        TEXT,
    insee_code         TEXT,
    city               TEXT,
    country            TEXT,
    latitude           TEXT,
    longitude          TEXT,
    altitude           TEXT,
    meter_type         TEXT,
    usage_point_status TEXT
) STRICT;

-- Hourly Ecowatt signal, keyed by the start of the hour.
CREATE TABLE ecowatt_hour (
    at    INTEGER PRIMARY KEY,
    level INTEGER NOT NULL
) STRICT;

-- The current Tempo season: days left per color.
CREATE TABLE tempo_season (
    color      TEXT    PRIMARY KEY CHECK (color IN ('BLUE', 'WHITE', 'RED')),
    days_left  INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL
) STRICT;

-- Current Tempo prices, in euros per kWh, as published (e.g. "0.1654").
CREATE TABLE tempo_price (
    color      TEXT    NOT NULL CHECK (color IN ('BLUE', 'WHITE', 'RED')),
    period     TEXT    NOT NULL CHECK (period IN ('peak', 'offpeak')),
    price      TEXT    NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (color, period)
) STRICT, WITHOUT ROWID;

-- A customer resource the gateway could not give, and why: it is not asked
-- again for a while, so a resource that always fails does not cost a call a pass.
-- Cleared when the resource is next cached.
CREATE TABLE customer_failure (
    usage_point TEXT    NOT NULL,
    resource    TEXT    NOT NULL CHECK (resource IN ('contract', 'identity', 'contact', 'addresses')),
    failed_at   INTEGER NOT NULL,
    detail      TEXT    NOT NULL,
    PRIMARY KEY (usage_point, resource)
) STRICT, WITHOUT ROWID;

-- Daily Ecowatt signals were dated by the gateway's day key, which is always one
-- day early (observed live: the key 2026-09-12 holds the hours of 2026-09-13).
-- Every row moves one day later. The table is rebuilt: updating the key in
-- place would collide with the next day's row before that row moves.
CREATE TABLE ecowatt_day_dated (
    day     TEXT    PRIMARY KEY,
    level   INTEGER NOT NULL,
    message TEXT    NOT NULL
) STRICT;
INSERT INTO ecowatt_day_dated (day, level, message)
    SELECT date(day, '+1 day'), level, message FROM ecowatt_day;
DROP TABLE ecowatt_day;
ALTER TABLE ecowatt_day_dated RENAME TO ecowatt_day;
