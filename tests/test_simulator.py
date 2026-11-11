"""The simulator replays parquet files record by record against the real ASGI app."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import FastAPI

from cowshed.api import create_app
from cowshed.config import Settings
from cowshed.simulator import SimulationResult, measurement_requests, run_simulation
from tests.conftest import COW_A, MILK_SENSOR, WEIGHT_SENSOR

T0 = 1_720_000_800.0  # 2024-07-03T10:00:00Z


def _write_datasets(folder: Path) -> Path:
    folder.mkdir()
    pq.write_table(
        pa.table({"id": [COW_A], "name": ["Bella"], "birthdate": pa.array([date(2020, 1, 1)], pa.date32())}),
        folder / "cows.parquet",
    )
    pq.write_table(
        pa.table({"id": [MILK_SENSOR, WEIGHT_SENSOR], "unit": ["L", "kg"]}), folder / "sensors.parquet"
    )
    pq.write_table(
        pa.table(
            {
                "sensor_id": [WEIGHT_SENSOR, MILK_SENSOR, MILK_SENSOR, MILK_SENSOR],
                "cow_id": [COW_A] * 4,
                "timestamp": [T0 + 3600, T0, T0 + 7200, T0 + 10800],  # deliberately out of order
                "value": [550.0, 4.7, None, float("nan")],
            }
        ),
        folder / "measurements.parquet",
    )
    return folder


def _run(app: FastAPI, data_dir: Path) -> SimulationResult:
    async def go() -> SimulationResult:
        def make_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

        return await run_simulation(make_client, data_dir, concurrency=2)

    return asyncio.run(go())


def test_measurements_are_replayed_in_time_order_with_nulls(tmp_path: Path) -> None:
    data = _write_datasets(tmp_path / "data")
    requests = list(measurement_requests(data))
    assert [r[1]["timestamp"] for r in requests] == sorted(r[1]["timestamp"] for r in requests)
    assert [r[1]["value"] for r in requests] == [4.7, 550.0, None, None]  # NaN becomes null
    assert requests[0][0] == f"/sensors/{MILK_SENSOR}/measurements"


def test_simulation_end_to_end_and_rerun_is_safe(tmp_path: Path, db_url: str) -> None:
    data = _write_datasets(tmp_path / "data")
    app = create_app(Settings(database_url=db_url, report_dir=str(tmp_path)))

    first = _run(app, data)
    assert (first.cows.created, first.sensors.created, first.measurements.created) == (1, 2, 4)
    assert first.measurements.rejected == first.measurements.failed == 0

    second = _run(app, data)  # nothing is duplicated, nothing fails
    assert (second.cows.duplicates, second.sensors.duplicates, second.measurements.duplicates) == (1, 2, 4)
    assert second.measurements.created == 0

    latest = app.state.repo.latest_measurement(COW_A)
    assert latest is not None and latest.quality.value == "missing"
