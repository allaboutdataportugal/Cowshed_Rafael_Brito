-- =============================================================================
-- Cowshed - the daily report in plain SQL (PostgreSQL / psql)
-- Same logic as src/cowshed/reporting.py, for ad-hoc use and for cross-checking
-- the application output. Usage:
--
--   psql -v report_date=2024-07-10 -v tz=UTC -f sql/queries/daily_report.sql
--
-- (with Docker:  docker exec -i cowshed-db psql -U cowshed -d cowshed \
--                  -v report_date=2024-07-10 -v tz=UTC < sql/queries/daily_report.sql)
-- =============================================================================

\set ON_ERROR_STOP on
\pset footer off

-- Day window [day_start, day_end) in the requested timezone, as epoch seconds.
DROP TABLE IF EXISTS pg_temp.report_window;
CREATE TEMP TABLE report_window AS
SELECT extract(epoch FROM (:'report_date'::date)::timestamp AT TIME ZONE :'tz')::bigint                       AS day_start,
       extract(epoch FROM ((:'report_date'::date + 1)::timestamp) AT TIME ZONE :'tz')::bigint                 AS day_end,
       extract(epoch FROM ((:'report_date'::date + 1)::timestamp AT TIME ZONE :'tz') - interval '30 days')::bigint AS month_start;

\echo
\echo '1. MILK PRODUCTION PER COW (valid readings of the day)'
SELECT c.name || ' [' || left(c.id, 8) || ']'                           AS cow,
       round(sum(m.value) FILTER (WHERE m.quality = 'valid')::numeric, 2) AS milk_liters,
       count(*) FILTER (WHERE m.quality = 'valid')                       AS milkings,
       count(*) FILTER (WHERE m.quality <> 'valid')                      AS invalid
FROM measurements m
JOIN cows c ON c.id = m.cow_id
CROSS JOIN report_window w
WHERE m.unit = 'L' AND m.measured_at >= w.day_start AND m.measured_at < w.day_end
GROUP BY c.id, c.name
ORDER BY c.name, c.id;

\echo
\echo '2. WEIGHT PER COW (current = last valid reading up to the end of the day)'
WITH w AS (SELECT * FROM report_window),
latest AS (
    SELECT DISTINCT ON (m.cow_id) m.cow_id, m.value, m.measured_at
    FROM measurements m, w
    WHERE m.unit = 'kg' AND m.quality = 'valid' AND m.measured_at < w.day_end
    ORDER BY m.cow_id, m.measured_at DESC, m.id
),
avg30 AS (
    SELECT m.cow_id, avg(m.value) AS avg_kg, count(*) AS readings
    FROM measurements m, w
    WHERE m.unit = 'kg' AND m.quality = 'valid'
      AND m.measured_at >= w.month_start AND m.measured_at < w.day_end
    GROUP BY m.cow_id
)
SELECT c.name || ' [' || left(c.id, 8) || ']'              AS cow,
       round(l.value::numeric, 1)                          AS current_kg,
       to_timestamp(l.measured_at) AT TIME ZONE :'tz'      AS measured_at,
       round(a.avg_kg::numeric, 1)                         AS avg_30d_kg,
       a.readings                                          AS readings_30d
FROM avg30 a
JOIN cows c ON c.id = a.cow_id
LEFT JOIN latest l ON l.cow_id = a.cow_id
ORDER BY c.name, c.id;

\echo
\echo '3. POSSIBLE ILLNESS (mean of last 3 days vs days -30..-7; weight -5% or milk -15%)'
WITH w AS (SELECT * FROM report_window),
stats AS (
    SELECT m.cow_id, m.unit,
           avg(m.value) FILTER (WHERE m.measured_at >= w.day_end - 3 * 86400)                     AS recent,
           count(*)     FILTER (WHERE m.measured_at >= w.day_end - 3 * 86400)                     AS n_recent,
           avg(m.value) FILTER (WHERE m.measured_at <  w.day_end - 7 * 86400)                     AS baseline,
           count(*)     FILTER (WHERE m.measured_at <  w.day_end - 7 * 86400)                     AS n_baseline
    FROM measurements m, w
    WHERE m.quality = 'valid' AND m.measured_at >= w.day_end - 30 * 86400 AND m.measured_at < w.day_end
    GROUP BY m.cow_id, m.unit
)
SELECT c.name || ' [' || left(c.id, 8) || ']'                       AS cow,
       CASE s.unit WHEN 'kg' THEN 'weight' ELSE 'milk per milking' END AS signal,
       round(s.baseline::numeric, 2)                                AS baseline,
       round(s.recent::numeric, 2)                                  AS recent,
       round((100 * (s.recent - s.baseline) / s.baseline)::numeric, 1) AS change_pct
FROM stats s
JOIN cows c ON c.id = s.cow_id
WHERE s.n_recent >= 3 AND s.n_baseline >= 3
  AND 100 * (s.recent - s.baseline) / s.baseline <= CASE s.unit WHEN 'kg' THEN -5 ELSE -15 END
ORDER BY change_pct;

\echo
\echo '4. DATA QUALITY (readings received during the day)'
SELECT CASE m.unit WHEN 'L' THEN 'milk' ELSE 'weight' END AS measure, m.quality, count(*) AS readings
FROM measurements m, report_window w
WHERE m.measured_at >= w.day_start AND m.measured_at < w.day_end
GROUP BY 1, 2
ORDER BY 1, 2;
