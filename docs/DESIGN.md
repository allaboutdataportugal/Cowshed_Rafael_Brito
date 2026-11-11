# Cowshed: how it was built and why

This document explains how the solution is built, the assumptions behind it, and the
reasons for each choice. It follows the order of the assignment. It ends with what
changes when the system serves every farm in Europe.

---

## 1. What was asked and what was delivered

| Requirement | Where it is answered |
|---|---|
| `POST /cows/{id}` creates a cow | `src/cowshed/api.py`, `create_cow` |
| Endpoint(s) to receive sensor data | `POST /sensors/{id}` (register a sensor) and `POST /sensors/{id}/measurements` (one reading) |
| `GET /cows/{id}` returns the cow and its latest reading | `get_cow`: latest reading overall plus latest per measure |
| Loop over the datasets, one request per record | `src/cowshed/simulator.py`, `cowshed simulate` |
| Report: milk per cow per day; current weight and 30-day average; possibly ill cows | `src/cowshed/reporting.py`, `cowshed report`, `GET /reports/daily` |
| Report is text based, daily, and works for any date | `render_text` / `render_json`; `cowshed schedule`; `--date` |
| Data persisted across restarts | PostgreSQL on a named Docker volume (SQLite file for development). Test: `tests/test_persistence.py` |
| Null and negative values handled, with justified assumptions | §3 |
| Type hints enforced at runtime, data validated | §6 |
| Best practices and unit tests | 45 tests, `ruff`, `mypy --strict`, layered code, CI running Docker Compose and Terraform end to end |
| Scalability to all farms in Europe | §11 |
| How the daily process would look in reality | §7.4 and §11 |

---

## 2. Architecture

```
                 ┌──────────────────────┐   one HTTP request per reading
 parquet files ─►│ simulator (async,    │──────────────────────────────┐
                 │ bounded concurrency) │                              │
                 └──────────────────────┘                              ▼
                                                     ┌────────────────────────────┐
  farmer / tools ──── GET /cows/{id}  ──────────────►│ API (FastAPI, N workers)   │
                 ◄─── GET /reports/daily ────────────│  schemas  -> validation    │
                                                     │  repository -> SQL         │
                                                     └──────────────┬─────────────┘
                                                                    │
                                ┌───────────────────────────────────▼──────────────┐
                                │ PostgreSQL  cows · sensors · measurements        │
                                │             views for BI (sql/init/02_views.sql) │
                                └───────────────────────────────────▲──────────────┘
                                                                    │
                 ┌──────────────────────┐   every day at 01:00      │
 reports/ ◄──────│ scheduler            │───────────────────────────┘
  date=YYYY-MM-DD│ (report for D-1)     │
  /*.txt, *.json └──────────────────────┘
```

The code is split into layers. Each layer depends only on the ones below it:

* **`schemas.py`** is the HTTP contract (Pydantic). Nothing invalid gets past it.
* **`domain.py`** holds the business vocabulary: units, quality rules and illness thresholds. It does no I/O, so it is easy to test.
* **`repository.py`** is the only module that speaks SQL. All aggregation runs in the database, so the API never loads raw readings into memory to compute a report.
* **`reporting.py` / `jobs.py`** turn query results into a report and publish it.
* **`api.py` / `cli.py` / `simulator.py`** are thin entry points.

The same Docker image runs the API, the scheduler and the simulator. Only the start
command differs, so the three can never drift apart.

---

## 3. Data findings and assumptions

Before writing code I profiled the three datasets:

| Fact | Value |
|---|---|
| Cows / sensors / readings | 128 / 200 / 563,627 |
| Period | 2020-07-11 → 2024-07-11 |
| Sensor units | `L` (milk) 106 sensors, `kg` (weight) 94 sensors |
| Cadence | each cow is read every 4 hours; each slot is either milk or weight |
| Milk per reading | valid values between 4.50 and 5.00 L (≈ 15 L per day per cow) |
| Weight | ≈ 550 kg, standard deviation within a cow usually 1–3 kg |
| Orphans or duplicates | none (every `sensor_id` and `cow_id` exists; no repeated sensor/cow/timestamp) |
| Duplicate cow names | yes: "Mary #8", "Nicole #4" and "Jennifer #7" each appear twice |

Reading values that cannot be real:

| Value | Milk | Weight | Interpretation |
|---|---|---|---|
| `null` | 655 | 2,738 | the sensor fired but produced no value |
| exactly `-1` | 678 | 2,747 | the only negative number in the data: a sensor error sentinel |
| `0` | 1,391 | 5,627 | sensor fault, see below |

**The decisive finding: all 13,836 invalid readings come from exactly 5 sensors**
(`74932a2e`, `3459d3dd`, `91440d7d`, `98962937`, `a8d52b06`). Each of them returns
*only* nulls, `-1` and zeros: 100 % invalid over four years. The other 195 sensors
never produce a single invalid value. So the bad values are broken hardware, not
cow behaviour.

**Decision: store every reading as received, tag it with a `quality`, and use only
`valid` readings in figures.**

* **Why not reject it with a 4xx?** The sensor would retry forever, or drop the record, and we would lose the evidence that the sensor is faulty. That evidence is exactly how the 5 broken sensors were found. The report's data quality section lists them every day (for example `74932a2e (5/5)`), so the farmer knows which devices to replace. The view `v_sensor_daily_quality` tracks the same over time.
* **Why not convert it to 0?** A `null` milk reading summed as 0 would understate the milk yield and could raise false illness alarms.
* **Why are zeros invalid, even for milk?**
  * Every zero comes from one of the 5 always-broken sensors; healthy sensors never report 0.
  * A 0 kg cow is impossible.
  * Valid milk readings are never below 4.5 L. That is how this herd's milking equipment behaves; no value appears between 0 and 4.5.
  * For *both* units the number of zeros is 2.05 × the number of `-1` values. The same ratio in both units points to the same fault mechanism.
  * A cow that really is not milked (for example in her dry period) produces *no milking event*, not an event of 0 L.
* **Upper bound.** A reading above a physical ceiling is tagged `out_of_range`: 60 L for one milking, 1,500 kg for a cow. The ceilings are generous on purpose; they only catch nonsense values.
* **Missing figures.** If a cow has no valid milk reading on a day, the report shows `n/a` and lists her under data quality. It does not show 0 L.

These rules live in one function, `domain.classify_value`, so they can be changed in
one place. Because the raw value is kept, past readings can be re-tagged later
without having to receive the data again.

---

## 4. API design

* **Resources are addressed by the client's UUID** (`POST /cows/{id}`), as the assignment asks. Ids are validated as UUIDs.
* **A measurement is posted under its sensor** (`/sensors/{id}/measurements`). In a real deployment, each device would authenticate as itself (API key or mTLS) and could post only to its own path. The sensor also tells us the unit, so a reading cannot be filed under the wrong measure.
* **Ingestion is idempotent.**
  * The key `(sensor_id, cow_id, timestamp)` is unique.
  * Re-sending the same reading returns `200` with the stored record. A new reading returns `201`.
  * The same key with a *different* value returns `409`, and nothing is silently overwritten.
  * The result: retries after timeouts are safe, and the simulator can be re-run.
* **Timestamps must include a timezone.** They are stored as UTC epoch seconds. `12:00+02:00` and `10:00Z` are recognised as the same reading.
* **`GET /cows/{id}`** returns `latest_measurement` (any kind, including flagged readings, because that is what was literally received last) and `latest_by_measure` (the last milk and the last weight). The farmer usually wants both.
* **Status codes:**
  * `201` created, `200` idempotent replay;
  * `404` unknown cow or sensor;
  * `409` conflict;
  * `422` invalid input. A custom handler keeps the error JSON valid even when the input was `NaN`. Without it the response would have been a 500.

---

## 5. Persistence

* **PostgreSQL in the deployed stack.**
  * It provides transactional durability: a committed row is on disk.
  * Constraints (FK, UNIQUE, CHECK) protect the data even from bugs in the application.
  * Several API processes can write at the same time.
  * The data sits on the named volume `cowshed-pgdata`, so container restarts, upgrades and `terraform apply` keep it.
* **SQLite for development and tests.** `pip install` and run, nothing else needed. It uses WAL mode with `synchronous=FULL` by default, so a committed reading survives a crash. The code uses SQLAlchemy Core and portable SQL, and the test suite runs on SQLite. The full replay was also run against PostgreSQL.
* **Schema choices** (`sql/init/01_schema.sql`, mirrored in `db.py`):
  * `measured_at` is stored as epoch seconds (`BIGINT`). It cannot be ambiguous about timezone, and it compares and indexes cheaply on any engine.
  * `unit` is copied onto each measurement. Reports filter "all milk of one day" without a join on the largest table.
  * Two indexes: `(cow_id, unit, measured_at)` for per-cow lookups (the latest reading, per-cow windows) and `(unit, measured_at)` for one-day scans.
  * `quality` has a CHECK constraint, so the column cannot hold an unknown label.
* **Analytical views** (`sql/init/02_views.sql`): `v_daily_milk_per_cow`, `v_cow_weight_now`, `v_sensor_daily_quality`. Analysts and BI tools get the same definitions as the application without reading Python.

---

## 6. Validation and runtime type enforcement

1. **The HTTP boundary (Pydantic v2).**
   * Every field has a type and constraints. Unknown fields are rejected (`extra="forbid"`).
   * A reading `value` is a strict float. `"4.7"` and `true` are rejected rather than coerced, because silent coercion hides sensor bugs. `NaN` and `Infinity` are rejected too.
   * A timestamp must include a timezone and may not be more than 5 minutes in the future (clock skew allowance). A birthdate may not be in the future.
2. **Inside the application (typeguard).** The public functions of the domain, repository, reporting, jobs and simulator modules carry `@typechecked`, so every call checks its type hints at runtime. A test checks that a wrong type raises `TypeCheckError`.
3. **Static checks.** `mypy --strict` passes, and `ruff` enforces style and common bug patterns.

---

## 7. The daily report

### 7.1 Definitions

All figures cover one calendar day `D`.

* **The day** is `[D 00:00, D+1 00:00)` in `COWSHED_REPORT_TIMEZONE` (default UTC). A farm in the Netherlands sets `Europe/Amsterdam`. Daylight-saving days are 23 h or 25 h long; there is a test for this.
* **Milk per cow** is the sum of the cow's valid milk readings in the day. The report also shows the number of milkings and the number of invalid readings, so an unusually low figure can be checked against missing data.
* **Current weight** is the last valid weight reading *up to the end of D*, with the time it was taken. It is the weight "as of that day", and a report for a past date does not change when newer data arrives.
* **Average weight over the last 30 days** is the mean of valid weights in `[D+1 − 30 days, D+1)`.
* **Cows included** are those seen by any sensor in those 30 days. A cow that left the farm drops out instead of showing empty rows forever.
* **Names** are not unique, so each cow is shown as `Name [first 8 characters of the id]`.

### 7.2 Reproducible for any date

A report is a pure function of (data, date, timezone). Running it again for the same
date gives the same figures, which `test_report_is_reproducible_for_a_past_date`
checks. The only exception is readings that arrive late for that date: in that case
the new report is more correct.

### 7.3 Output

There are three ways to get the report:

* a text report, readable as is (four sections: milk, weight, possible illness, data quality);
* the same content as JSON, for other systems;
* `GET /reports/daily?date=...&format=text|json`, which serves both on demand.

`sql/queries/daily_report.sql` reproduces the report in plain SQL, so anyone can
cross-check the figures.

### 7.4 Scheduling: here and in reality

* **Here.** The scheduler container (`cowshed schedule --at 01:00`) produces yesterday's report every night into `reports/date=YYYY-MM-DD/`.
  * Files are written atomically (temporary file, then rename), so a reader never sees half a report.
  * Re-running for a date overwrites that date's files, so a backfill is just `cowshed report --date ... --publish reports`.
* **In reality** this belongs in a workflow orchestrator (Airflow, Dagster, or a managed cloud scheduler), with:
  * one run per farm per day, triggered after the farm's local midnight plus a margin for late readings;
  * retries, alerting on failure and SLAs ("the report exists by 06:00 local time");
  * re-processing of the last *N* days, because sensors that were offline upload their backlog later. The report for D-1 must be refreshed when D-1's data changes;
  * delivery where the farmer looks: an app notification, an e-mail, a dashboard. Illness alerts go out as soon as they are detected, not once a day.

---

## 8. "Which cows might be ill?"

**Method.** Each cow is compared with herself, not with the herd. Some cows are simply lighter or give less milk, and comparing them with the herd would raise false alarms.

* *recent* = mean of valid readings in the last 3 days;
* *baseline* = mean of valid readings from day −30 to day −7.

The baseline stops a week before the report date on purpose. A gradual decline then cannot hide inside its own baseline.

* Flag **weight loss ≥ 5 %**. Normal day-to-day variation within a cow is about 0.5 %.
* Flag a **milk drop per milking ≥ 15 %**. The value per milking is used, not the daily total, so a day with fewer milkings does not look like a drop.
* At least 3 valid readings are required in each window. Without them there is no conclusion.

All thresholds live in `IllnessThresholds` and can be configured.

**What the data shows** (report for 2024-07-10, `docs/sample_reports/`):

| Cow | Early June | Baseline (days −30..−7) | Last 3 days | Change flagged | Lowest |
|---|---|---|---|---|---|
| Joanne #6 `33eb52d5` | 546.9 kg | 513.1 kg | 457.1 kg | −10.9 % | 453.4 kg |
| Stacy #9 `fa5625d5` | 548.6 kg | 514.7 kg | 459.1 kg | −10.8 % | 452.9 kg |
| Anne #4 `4b54f862` | 546.9 kg | 521.1 kg | 475.0 kg | −8.8 % | 471.2 kg |

* All three were stable at about 547 kg in early June 2024. They started losing weight around 12–13 June and lost about 75–95 kg (−14 % to −17 %) in four weeks.
* Over the same windows, no other cow changed by more than **0.5 %**, so the 5 % threshold leaves a wide safety margin.
* Their milk yield did not change, and neither did the rest of the herd's (milk readings in this dataset carry no trend), so weight is the signal here.
* Rapid unexplained weight loss is a classic sign of illness in dairy cattle, for example ketosis, mastitis or lameness that keeps a cow from eating.
* Running the detector day by day over June–July 2024, the first alerts fire on **21 June** (Stacy #9), **22 June** (Joanne #6) and **25 June** (Anne #4). That is more than two weeks before the cows reached their lowest weight, with **no false positive** for any other cow in that period.

**Limitations.** This is a screening rule, not a diagnosis:

* It tells the farmer which cows to check first.
* With more sensor types (rumination, activity, body temperature, milk conductivity), the same comparison of each cow with her own baseline extends naturally.
* With labelled outcomes it could become a trained model.

---

## 9. The simulator

* It reads the parquet files with `pyarrow` (no pandas needed) and sends **one HTTP request per record**, as the assignment asks:
  1. first the cows and the sensors (they must exist);
  2. then the readings, in chronological order, as the sensors produced them.
* `NaN` and null values are sent as JSON `null`. The simulator does not clean anything: cleaning is the API's job.
* A pool of async workers keeps a bounded number of requests in flight (`--concurrency`), with keep-alive connections.
* Retries use exponential backoff on network errors, `429` and `5xx`.
* A summary is printed at the end: created, duplicates, rejected, failed.
* Re-running it is safe because ingestion is idempotent; there is a test for this.

---

## 10. Infrastructure as code (Terraform)

The client asked for a reproducible setup that runs **locally**. Terraform's
`kreuzwerker/docker` provider manages the local Docker engine the same way a cloud
provider manages cloud resources:

* **Declarative and repeatable.** `terraform apply` produces the same stack on any machine. `terraform destroy` removes it cleanly.
* **No hand-written secrets.** The database password comes from `random_password` and is only marked `sensitive` in the outputs.
* **Incremental image rebuilds.** The image is rebuilt only when `src/`, `pyproject.toml` or the `Dockerfile` change (the trigger is a hash of those files).
* **Validated variables.** Report time, worker count and similar inputs are checked, and seeding has a precondition that the data folder exists.
* **Startup order through health checks.** The API waits for a healthy database, and the scheduler and the seed wait for a healthy API.
* **The same layout as production.** The Docker resources map one to one onto cloud resources. On AWS, for example, the database becomes RDS PostgreSQL, the API an ECS/Kubernetes service behind a load balancer, the scheduler a scheduled task, and `reports/` an object store. The application code does not change, only the provider and a few resources.

`docker-compose.yml` describes the same stack for people who do not use Terraform.

---

## 11. Scaling to every farm in Europe

**Order of magnitude.**

* The EU has about **20 million dairy cows**.
* At this dataset's rate (one reading every 4 h, 6 per day) that is 120 million readings a day, or about **1,400 per second on average**. Milking-time peaks are several times higher.
* Modern farms add collars and boluses that report every few minutes, which can push the load **10–100× higher**.

That is a stream-processing problem, not a single database. What changes:

| Concern | Now (one farm) | All of Europe |
|---|---|---|
| **Ingestion** | one HTTP request per reading | devices publish over MQTT/HTTPS to a stateless gateway (autoscaled). It authenticates each device, validates, and writes to a **durable log** (Kafka / Kinesis / Event Hubs) partitioned by `farm_id`. It accepts batches, because each farm hub forwards many readings per request. |
| **Back-pressure and spikes** | none needed | the log absorbs peaks and consumers catch up; the API never waits for the database |
| **Storage** | one PostgreSQL | **hot:** time-series storage (TimescaleDB or ClickHouse) partitioned by time and farm, with the last months indexed. **Cold:** raw immutable events as Parquet in an object store (Iceberg/Delta), the system of record, so anything can be recomputed. |
| **Multi-tenancy** | single farm | `farm_id` on every key, row-level isolation, per-farm quotas. Regional deployments for GDPR and data residency. |
| **Reports** | computed on demand | streaming jobs keep **daily per-cow aggregates** up to date incrementally. The report becomes a cheap read, so "any date" means reading one aggregate row per cow instead of scanning raw readings. |
| **Late or out-of-order data** | report re-run by hand | event-time processing with watermarks. Aggregates for the affected days are recomputed automatically, and the reports for those days are regenerated. |
| **Timezones** | one setting | each farm keeps its own timezone, and its day and report schedule follow it |
| **Illness detection** | in the daily batch | a streaming rule (or model) per cow, with alerts within minutes; thresholds per breed and lactation stage |
| **Idempotency** | DB unique key | an event id at the gateway, plus deduplication in the consumers (an at-least-once log needs it) |
| **Operations** | logs | metrics (ingest lag, rejected ratio per sensor), tracing, data quality monitors, alerting |

What does **not** change, because it was built in from the start:

* the API contract and validation rules;
* the quality tags and the "keep everything, flag the bad" policy;
* idempotent ingestion;
* reports as pure functions of data and date;
* stateless application containers.

---

## 12. Trade-offs and next steps

* **One request per reading, measured on a regular laptop.** Ingestion runs at about 70–110 readings/s end to end (Docker Desktop on Windows), so replaying all 563,627 readings takes roughly 1.5–2.5 hours. The limit is the laptop's CPU, which runs the simulator, the API and the database at the same time; the database stays mostly idle. `--since 2024-06-01` replays just the weeks the 2024-07-10 report needs, in about 5 minutes. The two optimisations already applied:
  * each simulator worker has its own HTTP connection, because httpx's shared pool was CPU-bound;
  * `sniffio` is a declared dependency, because without it httpcore re-scans `sys.path` on every request.

  For more throughput, the next steps are a batch endpoint (one farm hub sends many readings per request), more API replicas, and a queue in front of the database (§11).
* **Verified end to end.**
  * 182,639 readings went through the HTTP API with 0 rejected and 0 failed.
  * The stored rows are identical (counts and sums by unit and quality) to a direct load of the same files.
  * The Python report and `sql/queries/daily_report.sql` give the same figures.
* **No authentication in this version.** The next step is per-device API keys or mTLS for sensors, and OAuth for people.
* **Schema migrations.** `create_all` and the SQL init are enough for a first version; Alembic would manage changes after that.
* **Illness thresholds** were chosen from this herd's natural variability. With more history they should be calibrated per breed and per lactation stage, and checked against confirmed cases.
