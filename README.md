# Cowshed

[![CI](https://github.com/allaboutdataportugal/Cowshed/actions/workflows/ci.yml/badge.svg)](https://github.com/allaboutdataportugal/Cowshed/actions/workflows/ci.yml)

Sensor ingestion API and daily farm reports for Ingrid's cowshed.

* **API** (FastAPI) to register cows and sensors and to receive milk and weight readings.
* **Simulator** that replays the provided parquet datasets one record per HTTP request.
* **Daily report** that can be produced for any date, in text or JSON. It covers:
  * milk per cow per day;
  * current weight and the average weight over the last 30 days;
  * **cows that may be ill**;
  * data quality.
* **Persistence** in PostgreSQL. SQLite is the zero-setup option for development and tests.
* **Infrastructure as code**: Terraform (Docker provider) brings up the whole stack locally. A `docker-compose.yml` is provided as a lighter alternative.

The reasoning behind every decision (data assumptions, illness rules, scaling to all
farms in Europe) is in **[docs/DESIGN.md](docs/DESIGN.md)**.

```
cowshed/
├── src/cowshed/          application code
│   ├── api.py            HTTP endpoints
│   ├── schemas.py        request/response contracts (Pydantic validation)
│   ├── domain.py         units, quality rules for null/negative/zero values, illness thresholds
│   ├── db.py             schema (SQLAlchemy Core) + engine factory
│   ├── repository.py     all database access; aggregations pushed down to SQL
│   ├── reporting.py      daily report + illness detection + text/JSON rendering
│   ├── jobs.py           daily report job (atomic file output, idempotent)
│   ├── simulator.py      parquet -> one HTTP request per record
│   └── cli.py            `cowshed serve | simulate | report | schedule`
├── sql/
│   ├── init/             PostgreSQL schema + analytical views (loaded on first start)
│   └── queries/          the daily report as plain SQL (psql)
├── terraform/            local infrastructure: Postgres, API, scheduler, optional seed
├── docker-compose.yml    same stack without Terraform
├── Dockerfile
├── tests/                45 unit/integration tests (pytest)
├── docs/
│   ├── DESIGN.md         how it was built and why
│   └── sample_reports/   reports generated from the provided datasets
├── scripts/
│   ├── demo.ps1              end-to-end demo on Windows without Docker
│   └── make_sample_data.py   small synthetic dataset (same schema) for CI and quick trials
└── .github/workflows/    CI: lint, types, tests, Docker Compose and Terraform end to end
```

Every push runs the whole chain in GitHub Actions. On a clean Linux machine it:

* builds the image;
* `terraform apply`s the stack and seeds it through the API;
* checks the API, the report, the plain-SQL report and persistence across a restart;
* runs `terraform destroy`.

---

## 1. Run it

### Option A: Terraform (recommended; the full stack in Docker)

Requirements: Docker Desktop (or dockerd) and Terraform >= 1.5.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set cow_data_dir, timezone, ...
terraform init
terraform apply
```

What `apply` creates:

| Resource | Role |
|---|---|
| `cowshed-net` | private Docker network |
| `cowshed-pgdata` | named volume: the database survives restarts and re-applies |
| `cowshed-db` | PostgreSQL 16, schema + views from `sql/init/` |
| `cowshed:local` | application image, rebuilt only when `src/`, `pyproject.toml` or `Dockerfile` change |
| `cowshed-api` | API on `http://localhost:8000` (docs at `/docs`), N worker processes |
| `cowshed-scheduler` | writes yesterday's report every day at `report_time` into `reports/` |
| `cowshed-seed` | only when `run_seed = true`: replays the parquet datasets into the API once |

```bash
terraform output                         # URLs, report folder, psql command
docker logs -f cowshed-seed              # follow the replay
terraform destroy                        # remove everything (including the data volume)
```

> Options A and B publish the same ports (8000, 5432). Run one at a time
> (`docker compose down` before `terraform apply`, or change `api_port` / `db_port`).

### Option B: Docker Compose

```bash
docker compose up -d --build                                   # db + api + scheduler
COW_DATA_DIR=/path/to/cow_data docker compose --profile seed run --rm seed
```

### Option C: plain Python (no Docker, SQLite)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cowshed serve --port 8000                           # terminal 1
cowshed simulate --data-dir /path/to/cow_data       # terminal 2
cowshed report --date 2024-07-10                    # text report on stdout
```

On Windows, `scripts\demo.ps1 -DataDir C:\path\to\cow_data` does all of the above. By
default it replays June–July 2024 (`--since 2024-06-01`, about 5 minutes), which is
enough for the 2024-07-10 report and its illness alerts.

No parquet files at hand? `python scripts/make_sample_data.py sample_data` writes a
small dataset with the same schema, including one cow that loses weight.

> **Linux hosts.** The scheduler writes the reports into a bind-mounted folder. Run it
> as your own user: `COWSHED_UID=$(id -u) docker compose up -d`, or
> `terraform apply -var reports_uid=$(id -u)`. Docker Desktop (Windows/macOS) needs nothing.

> **Replay time on a laptop.** One HTTP request per reading runs at about 70–110
> readings/s on a regular laptop, so all 563,627 readings take 1.5–2.5 h. For a live
> demo, replay only the recent weeks: `cowshed simulate --since 2024-06-01 ...` (~31k
> readings, about 5 minutes); `--limit N` caps the number of readings. See [DESIGN.md §12](docs/DESIGN.md#12-trade-offs-and-next-steps).
>
> **Durability vs speed with SQLite.** By default every committed reading is fsynced
> (`COWSHED_SQLITE_SYNCHRONOUS=FULL`). For a one-off bulk replay you can set `NORMAL`,
> which is about 10x faster. With `NORMAL` a restart of the application still loses
> nothing; only a power cut can lose the last few commits. PostgreSQL (options A and B)
> does not have this trade-off.

---

## 2. API

Interactive documentation: `http://localhost:8000/docs`.

| Method & path | Purpose | Responses |
|---|---|---|
| `POST /cows/{id}` | create a cow: `{"name": "Bella", "birthdate": "2020-01-09"}` | 201, 409 exists, 422 invalid |
| `GET /cows/{id}` | cow details + latest reading (overall and per measure) | 200, 404 |
| `POST /sensors/{id}` | register a sensor: `{"unit": "L"}` (milk) or `{"unit": "kg"}` (weight) | 201, 409, 422 |
| `GET /sensors/{id}` | sensor details | 200, 404 |
| `POST /sensors/{id}/measurements` | one reading: `{"cow_id": "...", "timestamp": "2024-07-10T10:00:00Z", "value": 4.71}` | 201 stored, 200 identical retry, 404 unknown cow/sensor, 409 same key with another value, 422 invalid |
| `GET /reports/daily?date=YYYY-MM-DD&format=json\|text` | daily report (default: yesterday) | 200, 422 |
| `GET /health` | liveness | 200 |

Example:

```bash
curl -X POST localhost:8000/cows/c821a6b7-8dd0-4b4e-9835-1c0c57264ba4 \
     -H 'content-type: application/json' -d '{"name": "Lindsey #4", "birthdate": "2020-01-09"}'
curl localhost:8000/cows/c821a6b7-8dd0-4b4e-9835-1c0c57264ba4
```

```json
{
  "id": "c821a6b7-8dd0-4b4e-9835-1c0c57264ba4",
  "name": "Lindsey #4",
  "birthdate": "2020-01-09",
  "latest_measurement": {"id": 563544, "sensor_id": "…", "unit": "kg", "measure": "weight",
                         "timestamp": "2024-07-11T10:00:00Z", "value": 551.3, "quality": "valid"},
  "latest_by_measure": {"milk": {"…": "…"}, "weight": {"…": "…"}}
}
```

**Validation** is enforced at runtime at two levels:

1. **Pydantic** at the HTTP boundary. It rejects:
   * a malformed UUID;
   * a future birthdate or timestamp;
   * a timestamp without timezone;
   * a value that is a string, a boolean, `NaN` or `Infinity`;
   * unknown fields.
2. **typeguard** `@typechecked` on the domain, repository, reporting and job functions. Their type hints are checked on every call. The code also passes `mypy --strict`.

**Null and negative values are stored, not rejected.** They get a `quality` flag and are kept out of every figure. [DESIGN.md §3](docs/DESIGN.md#3-data-findings-and-assumptions) explains the reasons.

---

## 3. Reports

```bash
cowshed report --date 2024-07-10                   # text to stdout
cowshed report --date 2024-07-10 --format json
cowshed report --date 2024-07-10 --publish reports  # writes reports/date=2024-07-10/*.txt|json
cowshed schedule --at 01:00                        # every day: yesterday's report
```

* **Day boundaries** follow `COWSHED_REPORT_TIMEZONE` (default `UTC`), and daylight-saving days are handled.
* A report depends only on the data and the date. A past date can be (re)generated at any time, and re-running the job overwrites that day's files.

Excerpt from [`docs/sample_reports/`](docs/sample_reports/), generated from the provided datasets:

```
3. POSSIBLE ILLNESS (3 cows)
   ! Anne #4 [4b54f862]
       - weight down 8.8%: 475.0 kg (last 3d) vs 521.1 kg (days -30 to -7)
   ! Joanne #6 [33eb52d5]
       - weight down 10.9%: 457.1 kg (last 3d) vs 513.1 kg (days -30 to -7)
   ! Stacy #9 [fa5625d5]
       - weight down 10.8%: 459.1 kg (last 3d) vs 514.7 kg (days -30 to -7)

4. DATA QUALITY (readings received during the day)
   milk   : 401 readings (missing=2, valid=399)
   weight : 367 readings (missing=1, negative=6, valid=350, zero=10)
   Sensors with invalid readings: 74932a2e (5/5), a8d52b06 (5/5), 91440d7d (4/4), ...
```

Two findings from the data:

* **Three cows have lost about 15 % of their body weight since mid-June 2024.** Every other cow is stable within 0.5 %.
* **Five sensors are broken.** They produce 100 % invalid readings (all the nulls, `-1` values and zeros in the dataset), while the other 195 sensors never produce an invalid one.

The same report is also available as plain SQL: `sql/queries/daily_report.sql`.

---

## 4. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `COWSHED_DATABASE_URL` | `sqlite:///./data/cowshed.db` | any SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host/db` |
| `COWSHED_REPORT_TIMEZONE` | `UTC` | IANA timezone of the farm's calendar day |
| `COWSHED_REPORT_DIR` | `./reports` | output folder of `cowshed schedule` |
| `COWSHED_SQLITE_SYNCHRONOUS` | `FULL` | SQLite fsync mode (`FULL` / `NORMAL`) |

## 5. Quality checks

```bash
pytest              # 45 tests: API, validation, persistence across restart, reports, simulator
ruff check src tests
mypy                # strict
```
