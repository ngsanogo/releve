-- releve schema, version 3: the load-curve points the cache drops are journaled.

-- A load-curve point dropped from `load_curve`, as it was, and the run that
-- dropped it: a complete answer replaced its day without it. Exporters read
-- this journal to drop the point too, as they read `run_id` to write the rest.
CREATE TABLE load_curve_removed (
    usage_point TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('consumption', 'production')),
    end_at      INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    watts       INTEGER NOT NULL,
    run_id      INTEGER NOT NULL,
    PRIMARY KEY (usage_point, direction, end_at)
) STRICT, WITHOUT ROWID;
CREATE INDEX load_curve_removed_run ON load_curve_removed (run_id);
