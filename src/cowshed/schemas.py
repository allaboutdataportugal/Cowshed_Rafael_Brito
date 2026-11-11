"""Pydantic models: the validated contract of the HTTP API.

Inputs reject silent coercion where it hides bugs (``"4.5"`` or ``true`` as a
reading value, a number as a name) and use ``extra="forbid"`` so typos in field
names are rejected instead of ignored. Dates, UUIDs and enums are still parsed from
their JSON string form.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, Strict, StringConstraints, field_validator

from cowshed.domain import Quality, Unit
from cowshed.repository import MeasurementRecord

MAX_CLOCK_SKEW = timedelta(minutes=5)

_INPUT = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CowCreate(BaseModel):
    model_config = _INPUT

    name: Annotated[str, Strict(), StringConstraints(min_length=1, max_length=100)]
    birthdate: date

    @field_validator("birthdate")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > datetime.now(timezone.utc).date():
            raise ValueError("birthdate cannot be in the future")
        return value


class SensorCreate(BaseModel):
    model_config = _INPUT

    unit: Unit


class MeasurementCreate(BaseModel):
    """One reading pushed by a sensor.

    ``value`` may be ``null`` or out of range: such readings are stored and flagged,
    never silently dropped. Structurally broken input (wrong types, NaN, naive or
    future timestamps) is rejected with 422.
    """

    model_config = _INPUT

    cow_id: UUID
    timestamp: AwareDatetime
    value: Annotated[float, Strict(), Field(allow_inf_nan=False)] | None

    @field_validator("timestamp")
    @classmethod
    def _not_in_future(cls, value: datetime) -> datetime:
        if value > datetime.now(timezone.utc) + MAX_CLOCK_SKEW:
            raise ValueError("timestamp is in the future")
        return value


class SensorOut(BaseModel):
    id: UUID
    unit: Unit


class MeasurementOut(BaseModel):
    id: int
    sensor_id: UUID
    cow_id: UUID
    unit: Unit
    measure: str
    timestamp: datetime
    value: float | None
    quality: Quality

    @classmethod
    def from_record(cls, record: MeasurementRecord) -> MeasurementOut:
        return cls(
            id=record.id,
            sensor_id=UUID(record.sensor_id),
            cow_id=UUID(record.cow_id),
            unit=record.unit,
            measure=record.unit.measure,
            timestamp=record.timestamp,
            value=record.value,
            quality=record.quality,
        )


class CowOut(BaseModel):
    id: UUID
    name: str
    birthdate: date
    latest_measurement: MeasurementOut | None = Field(
        description="Most recent reading of any kind (including flagged ones)."
    )
    latest_by_measure: dict[str, MeasurementOut] = Field(
        description="Most recent reading per measure ('milk', 'weight')."
    )


class ErrorOut(BaseModel):
    detail: str
