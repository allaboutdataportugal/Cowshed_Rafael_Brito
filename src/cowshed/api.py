"""HTTP API (FastAPI)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from cowshed import __version__
from cowshed.config import Settings
from cowshed.db import create_db_engine, init_db
from cowshed.domain import Unit
from cowshed.reporting import DailyReport, build_daily_report, render_text
from cowshed.repository import AlreadyExistsError, ConflictError, NotFoundError, Repository
from cowshed.schemas import (
    CowCreate,
    CowOut,
    ErrorOut,
    MeasurementCreate,
    MeasurementOut,
    SensorCreate,
    SensorOut,
)


def get_repo(request: Request) -> Repository:
    repo: Repository = request.app.state.repo
    return repo


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _json_safe(value: Any) -> Any:
    """NaN/Infinity are not valid JSON: echo them back as strings in error messages."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [{k: _json_safe(v) for k, v in error.items() if k != "ctx"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


RepoDep = Annotated[Repository, Depends(get_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = create_db_engine(settings.database_url, settings.sqlite_synchronous)
    init_db(engine)

    app = FastAPI(
        title="Cowshed API",
        version=__version__,
        description="Register cows and sensors, ingest sensor readings and produce daily farm reports.",
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.state.settings = settings
    app.state.repo = Repository(engine)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- cows -------------------------------------------------------------------
    @app.post(
        "/cows/{cow_id}",
        status_code=status.HTTP_201_CREATED,
        response_model=CowOut,
        responses={409: {"model": ErrorOut}},
        tags=["cows"],
    )
    def create_cow(cow_id: UUID, body: CowCreate, repo: RepoDep) -> CowOut:
        try:
            cow = repo.create_cow(str(cow_id), body.name, body.birthdate)
        except AlreadyExistsError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return CowOut(
            id=cow_id, name=cow.name, birthdate=cow.birthdate, latest_measurement=None, latest_by_measure={}
        )

    @app.get("/cows/{cow_id}", response_model=CowOut, responses={404: {"model": ErrorOut}}, tags=["cows"])
    def get_cow(cow_id: UUID, repo: RepoDep) -> CowOut:
        cow = repo.get_cow(str(cow_id))
        if cow is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"cow {cow_id} not found")
        latest = repo.latest_measurement(cow.id)
        by_measure = {}
        for unit in Unit:
            record = repo.latest_measurement(cow.id, unit)
            if record is not None:
                by_measure[unit.measure] = MeasurementOut.from_record(record)
        return CowOut(
            id=cow_id,
            name=cow.name,
            birthdate=cow.birthdate,
            latest_measurement=MeasurementOut.from_record(latest) if latest else None,
            latest_by_measure=by_measure,
        )

    # --- sensors ----------------------------------------------------------------
    @app.post(
        "/sensors/{sensor_id}",
        status_code=status.HTTP_201_CREATED,
        response_model=SensorOut,
        responses={409: {"model": ErrorOut}},
        tags=["sensors"],
    )
    def create_sensor(sensor_id: UUID, body: SensorCreate, repo: RepoDep) -> SensorOut:
        try:
            repo.create_sensor(str(sensor_id), body.unit)
        except AlreadyExistsError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return SensorOut(id=sensor_id, unit=body.unit)

    @app.get(
        "/sensors/{sensor_id}",
        response_model=SensorOut,
        responses={404: {"model": ErrorOut}},
        tags=["sensors"],
    )
    def get_sensor(sensor_id: UUID, repo: RepoDep) -> SensorOut:
        sensor = repo.get_sensor(str(sensor_id))
        if sensor is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"sensor {sensor_id} not found")
        return SensorOut(id=sensor_id, unit=sensor.unit)

    @app.post(
        "/sensors/{sensor_id}/measurements",
        status_code=status.HTTP_201_CREATED,
        response_model=MeasurementOut,
        responses={
            200: {
                "model": MeasurementOut,
                "description": "Identical reading already stored (idempotent retry).",
            },
            404: {"model": ErrorOut},
            409: {"model": ErrorOut, "description": "Same sensor/cow/timestamp stored with another value."},
        },
        tags=["measurements"],
    )
    def add_measurement(
        sensor_id: UUID, body: MeasurementCreate, response: Response, repo: RepoDep
    ) -> MeasurementOut:
        try:
            record, created = repo.add_measurement(
                str(sensor_id), str(body.cow_id), body.timestamp, body.value
            )
        except NotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not created:
            response.status_code = status.HTTP_200_OK
        return MeasurementOut.from_record(record)

    # --- reports ----------------------------------------------------------------
    @app.get("/reports/daily", response_model=DailyReport, tags=["reports"])
    def daily_report(
        repo: RepoDep,
        settings: SettingsDep,
        report_date: Annotated[date | None, Query(alias="date", description="Defaults to yesterday.")] = None,
        fmt: Annotated[str, Query(alias="format", pattern="^(json|text)$")] = "json",
    ) -> DailyReport | PlainTextResponse:
        tz = settings.tz
        day = report_date or (datetime.now(tz).date() - timedelta(days=1))
        report = build_daily_report(repo, day, tz, settings.illness)
        if fmt == "text":
            return PlainTextResponse(render_text(report))
        return report

    return app
