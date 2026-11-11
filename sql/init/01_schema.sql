-- =============================================================================
-- Cowshed - PostgreSQL schema
-- Executed automatically by the postgres container on first start
-- (/docker-entrypoint-initdb.d). Mirrors src/cowshed/db.py; the application only
-- creates tables that are missing, so this file is the source of truth in Postgres.
-- =============================================================================

CREATE TABLE IF NOT EXISTS cows (
    id          VARCHAR(36)  PRIMARY KEY,                 -- UUID sent by the client
    name        VARCHAR(100) NOT NULL,                    -- not unique: two "Mary #8" exist
    birthdate   DATE         NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sensors (
    id          VARCHAR(36)  PRIMARY KEY,
    unit        VARCHAR(8)   NOT NULL CHECK (unit IN ('L', 'kg')),  -- L = milk, kg = weight
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS measurements (
    id           SERIAL           PRIMARY KEY,
    sensor_id    VARCHAR(36)      NOT NULL REFERENCES sensors (id),
    cow_id       VARCHAR(36)      NOT NULL REFERENCES cows (id),
    unit         VARCHAR(8)       NOT NULL CHECK (unit IN ('L', 'kg')),  -- copied from the sensor
    measured_at  BIGINT           NOT NULL,                -- epoch seconds, UTC
    value        DOUBLE PRECISION NULL,                    -- raw value exactly as received
    quality      VARCHAR(16)      NOT NULL
                 CHECK (quality IN ('valid', 'missing', 'negative', 'zero', 'out_of_range')),
    received_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    -- one reading per sensor, cow and instant: makes ingestion idempotent
    CONSTRAINT uq_measurement UNIQUE (sensor_id, cow_id, measured_at)
);

-- Point lookups: GET /cows/{id} (latest reading) and per-cow report windows.
CREATE INDEX IF NOT EXISTS ix_measurements_cow_unit_time ON measurements (cow_id, unit, measured_at);
-- Range scans: "all milk readings of one day" for the daily report.
CREATE INDEX IF NOT EXISTS ix_measurements_unit_time ON measurements (unit, measured_at);

COMMENT ON TABLE  measurements IS 'Every sensor reading as received; invalid ones are kept and flagged.';
COMMENT ON COLUMN measurements.quality IS
    'valid | missing (null) | negative (-1 sentinel) | zero | out_of_range. Only valid feeds reports.';
COMMENT ON COLUMN measurements.measured_at IS 'Epoch seconds (UTC) at which the sensor took the reading.';
