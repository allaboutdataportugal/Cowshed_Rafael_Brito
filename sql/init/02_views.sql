-- =============================================================================
-- Cowshed - analytical views (PostgreSQL)
-- Ready-made views for analysts / BI tools. Days are UTC calendar days here; the
-- application report can use any timezone (COWSHED_REPORT_TIMEZONE).
-- =============================================================================

-- Valid readings only, with a proper timestamp and the measure name.
CREATE OR REPLACE VIEW v_valid_measurements AS
SELECT m.id,
       m.cow_id,
       c.name                                   AS cow_name,
       m.sensor_id,
       CASE m.unit WHEN 'L' THEN 'milk' ELSE 'weight' END AS measure,
       m.unit,
       to_timestamp(m.measured_at)              AS measured_at,
       (to_timestamp(m.measured_at) AT TIME ZONE 'UTC')::date AS measured_date_utc,
       m.value
FROM measurements m
JOIN cows c ON c.id = m.cow_id
WHERE m.quality = 'valid';

-- View 1: milk production per cow per day.
CREATE OR REPLACE VIEW v_daily_milk_per_cow AS
SELECT cow_id,
       cow_name,
       measured_date_utc        AS day,
       round(sum(value)::numeric, 2) AS milk_liters,
       count(*)                 AS milkings
FROM v_valid_measurements
WHERE measure = 'milk'
GROUP BY cow_id, cow_name, measured_date_utc;

-- View 2: current weight and average weight over the last 30 days, per cow.
-- "Now" is the most recent reading in the database, so the view is also meaningful
-- on historical data; with live sensors it is effectively the current time.
CREATE OR REPLACE VIEW v_cow_weight_now AS
WITH ref AS (
    SELECT to_timestamp(max(measured_at)) AS as_of FROM measurements
), latest AS (
    SELECT DISTINCT ON (cow_id) cow_id, cow_name, value AS current_weight_kg, measured_at
    FROM v_valid_measurements
    WHERE measure = 'weight'
    ORDER BY cow_id, measured_at DESC, id
), last_30d AS (
    SELECT cow_id, round(avg(value)::numeric, 2) AS avg_weight_30d_kg, count(*) AS readings_30d
    FROM v_valid_measurements, ref
    WHERE measure = 'weight' AND measured_at > ref.as_of - interval '30 days'
    GROUP BY cow_id
)
SELECT l.cow_id, l.cow_name, l.current_weight_kg, l.measured_at AS current_weight_measured_at,
       a.avg_weight_30d_kg, coalesce(a.readings_30d, 0) AS readings_30d, ref.as_of
FROM latest l
CROSS JOIN ref
LEFT JOIN last_30d a USING (cow_id);

-- Sensor health: share of invalid readings per sensor and per day.
CREATE OR REPLACE VIEW v_sensor_daily_quality AS
SELECT sensor_id,
       (to_timestamp(measured_at) AT TIME ZONE 'UTC')::date AS day,
       count(*)                                        AS readings,
       count(*) FILTER (WHERE quality <> 'valid')      AS invalid_readings,
       round(100.0 * count(*) FILTER (WHERE quality <> 'valid') / count(*), 1) AS invalid_pct
FROM measurements
GROUP BY sensor_id, (to_timestamp(measured_at) AT TIME ZONE 'UTC')::date;
