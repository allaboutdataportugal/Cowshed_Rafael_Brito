from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from tests.conftest import COW_A, MILK_SENSOR, WEIGHT_SENSOR

READING = {"cow_id": COW_A, "timestamp": "2024-07-01T10:00:00Z", "value": 4.7}


def _setup(client: TestClient) -> None:
    assert client.post(f"/cows/{COW_A}", json={"name": "Bella", "birthdate": "2020-01-01"}).status_code == 201
    assert client.post(f"/sensors/{MILK_SENSOR}", json={"unit": "L"}).status_code == 201
    assert client.post(f"/sensors/{WEIGHT_SENSOR}", json={"unit": "kg"}).status_code == 201


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


# --- cows ---------------------------------------------------------------------
def test_create_and_get_cow(client: TestClient) -> None:
    response = client.post(f"/cows/{COW_A}", json={"name": "  Bella ", "birthdate": "2020-01-01"})
    assert response.status_code == 201
    assert response.json()["name"] == "Bella"

    body = client.get(f"/cows/{COW_A}").json()
    assert body == {
        "id": COW_A,
        "name": "Bella",
        "birthdate": "2020-01-01",
        "latest_measurement": None,
        "latest_by_measure": {},
    }


def test_create_cow_twice_conflicts(client: TestClient) -> None:
    payload = {"name": "Bella", "birthdate": "2020-01-01"}
    client.post(f"/cows/{COW_A}", json=payload)
    assert client.post(f"/cows/{COW_A}", json=payload).status_code == 409


def test_get_unknown_cow_is_404(client: TestClient) -> None:
    assert client.get(f"/cows/{COW_A}").status_code == 404


def test_cow_validation(client: TestClient) -> None:
    tomorrow = (date.today() + timedelta(days=2)).isoformat()
    bad = [
        ("not-a-uuid", {"name": "Bella", "birthdate": "2020-01-01"}),
        (COW_A, {"name": "", "birthdate": "2020-01-01"}),
        (COW_A, {"name": 123, "birthdate": "2020-01-01"}),
        (COW_A, {"name": "Bella", "birthdate": tomorrow}),
        (COW_A, {"name": "Bella", "birthdate": "2020-13-01"}),
        (COW_A, {"name": "Bella"}),
        (COW_A, {"name": "Bella", "birthdate": "2020-01-01", "colour": "brown"}),
    ]
    for cow_id, payload in bad:
        assert client.post(f"/cows/{cow_id}", json=payload).status_code == 422, payload


# --- sensors ------------------------------------------------------------------
def test_sensor_lifecycle(client: TestClient) -> None:
    assert client.post(f"/sensors/{MILK_SENSOR}", json={"unit": "L"}).status_code == 201
    assert client.get(f"/sensors/{MILK_SENSOR}").json() == {"id": MILK_SENSOR, "unit": "L"}
    assert client.post(f"/sensors/{MILK_SENSOR}", json={"unit": "L"}).status_code == 409
    assert client.post(f"/sensors/{WEIGHT_SENSOR}", json={"unit": "lbs"}).status_code == 422
    assert client.get(f"/sensors/{WEIGHT_SENSOR}").status_code == 404


# --- measurements -------------------------------------------------------------
def test_measurement_is_stored_and_returned_by_get_cow(client: TestClient) -> None:
    _setup(client)
    created = client.post(f"/sensors/{MILK_SENSOR}/measurements", json=READING)
    assert created.status_code == 201
    assert created.json()["quality"] == "valid"
    client.post(
        f"/sensors/{WEIGHT_SENSOR}/measurements",
        json={"cow_id": COW_A, "timestamp": "2024-07-01T06:00:00Z", "value": 550.0},
    )

    cow = client.get(f"/cows/{COW_A}").json()
    assert cow["latest_measurement"]["value"] == 4.7
    assert cow["latest_measurement"]["measure"] == "milk"
    assert cow["latest_measurement"]["timestamp"] == "2024-07-01T10:00:00Z"
    assert cow["latest_by_measure"]["weight"]["value"] == 550.0


def test_null_and_negative_values_are_stored_and_flagged(client: TestClient) -> None:
    _setup(client)
    cases = [(None, "missing"), (-1, "negative"), (0, "zero"), (9999.0, "out_of_range")]
    for hour, (value, quality) in enumerate(cases):
        payload = {"cow_id": COW_A, "timestamp": f"2024-07-01T0{hour}:00:00Z", "value": value}
        response = client.post(f"/sensors/{MILK_SENSOR}/measurements", json=payload)
        assert response.status_code == 201
        assert response.json()["quality"] == quality


def test_measurement_retry_is_idempotent(client: TestClient) -> None:
    _setup(client)
    first = client.post(f"/sensors/{MILK_SENSOR}/measurements", json=READING)
    again = client.post(f"/sensors/{MILK_SENSOR}/measurements", json=READING)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]

    changed = client.post(f"/sensors/{MILK_SENSOR}/measurements", json={**READING, "value": 3.0})
    assert changed.status_code == 409


def test_same_instant_in_another_timezone_is_the_same_reading(client: TestClient) -> None:
    _setup(client)
    client.post(f"/sensors/{MILK_SENSOR}/measurements", json=READING)
    shifted = {**READING, "timestamp": "2024-07-01T12:00:00+02:00"}
    assert client.post(f"/sensors/{MILK_SENSOR}/measurements", json=shifted).status_code == 200


def test_measurement_unknown_references_are_404(client: TestClient) -> None:
    _setup(client)
    unknown = "99999999-9999-4999-8999-999999999999"
    assert client.post(f"/sensors/{unknown}/measurements", json=READING).status_code == 404
    assert (
        client.post(f"/sensors/{MILK_SENSOR}/measurements", json={**READING, "cow_id": unknown}).status_code
        == 404
    )


def test_measurement_validation(client: TestClient) -> None:
    _setup(client)
    bad = [
        {**READING, "value": "4.7"},  # no string coercion
        {**READING, "value": True},
        {**READING, "timestamp": "2024-07-01T10:00:00"},  # naive timestamp
        {**READING, "timestamp": "2999-01-01T00:00:00Z"},  # future
        {**READING, "cow_id": "nope"},
        {"cow_id": COW_A, "timestamp": "2024-07-01T10:00:00Z"},  # value key is required (may be null)
        {**READING, "unexpected": 1},
    ]
    for payload in bad:
        response = client.post(f"/sensors/{MILK_SENSOR}/measurements", json=payload)
        assert response.status_code == 422, payload


def test_nan_is_rejected(client: TestClient) -> None:
    _setup(client)
    body = f'{{"cow_id": "{COW_A}", "timestamp": "2024-07-01T10:00:00Z", "value": NaN}}'
    response = client.post(
        f"/sensors/{MILK_SENSOR}/measurements", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422


# --- reports ------------------------------------------------------------------
def test_daily_report_endpoint(client: TestClient) -> None:
    _setup(client)
    client.post(f"/sensors/{MILK_SENSOR}/measurements", json=READING)
    report = client.get("/reports/daily", params={"date": "2024-07-01"}).json()
    assert report["report_date"] == "2024-07-01"
    assert report["cows"][0]["milk_liters"] == 4.7

    text = client.get("/reports/daily", params={"date": "2024-07-01", "format": "text"})
    assert text.headers["content-type"].startswith("text/plain")
    assert "MILK PRODUCTION PER COW" in text.text
    assert client.get("/reports/daily", params={"format": "pdf"}).status_code == 422
