"""Data must survive a restart: a brand new app on the same database sees everything."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cowshed.api import create_app
from cowshed.config import Settings
from tests.conftest import COW_A, MILK_SENSOR


def test_data_survives_restart(db_url: str, tmp_path: Path) -> None:
    settings = Settings(database_url=db_url, report_dir=str(tmp_path))
    first = create_app(settings)
    with TestClient(first) as client:
        client.post(f"/cows/{COW_A}", json={"name": "Bella", "birthdate": "2020-01-01"})
        client.post(f"/sensors/{MILK_SENSOR}", json={"unit": "L"})
        client.post(
            f"/sensors/{MILK_SENSOR}/measurements",
            json={"cow_id": COW_A, "timestamp": "2024-07-01T10:00:00Z", "value": 4.7},
        )
    first.state.repo.engine.dispose()  # simulate the process going away

    with TestClient(create_app(settings)) as client:
        cow = client.get(f"/cows/{COW_A}").json()
        assert cow["latest_measurement"]["value"] == 4.7
        assert client.get(f"/sensors/{MILK_SENSOR}").status_code == 200
