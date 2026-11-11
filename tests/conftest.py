from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cowshed.api import create_app
from cowshed.config import Settings
from cowshed.db import create_db_engine, init_db
from cowshed.domain import Unit
from cowshed.repository import Repository

COW_A = "11111111-1111-4111-8111-111111111111"
COW_B = "22222222-2222-4222-8222-222222222222"
MILK_SENSOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WEIGHT_SENSOR = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)  # type: ignore[misc]


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'cowshed.db').as_posix()}"


@pytest.fixture
def repo(db_url: str) -> Repository:
    engine = create_db_engine(db_url)
    init_db(engine)
    return Repository(engine)


@pytest.fixture
def herd(repo: Repository) -> Repository:
    """Two cows, one milk sensor, one weight sensor."""
    repo.create_cow(COW_A, "Bella", date(2020, 1, 1))
    repo.create_cow(COW_B, "Daisy", date(2021, 6, 1))
    repo.create_sensor(MILK_SENSOR, Unit.LITERS)
    repo.create_sensor(WEIGHT_SENSOR, Unit.KILOGRAMS)
    return repo


@pytest.fixture
def client(db_url: str, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(database_url=db_url, report_dir=str(tmp_path / "reports")))
    with TestClient(app) as test_client:
        yield test_client


def every_hours(start: datetime, hours: int, count: int) -> list[datetime]:
    return [start + timedelta(hours=hours * i) for i in range(count)]
