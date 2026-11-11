"""Data access layer. The API, the reports and the scheduler all go through here."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import Engine, and_, case, func, insert, select
from sqlalchemy.exc import IntegrityError
from typeguard import typechecked

from cowshed.db import cows, measurements, sensors
from cowshed.domain import Quality, Unit, classify_value


class NotFoundError(LookupError):
    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id} not found")
        self.entity = entity
        self.entity_id = entity_id


class AlreadyExistsError(Exception):
    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id} already exists")
        self.entity = entity
        self.entity_id = entity_id


class ConflictError(Exception):
    """Same (sensor, cow, timestamp) was already stored with a different value."""


@dataclass(frozen=True)
class CowRecord:
    id: str
    name: str
    birthdate: date


@dataclass(frozen=True)
class SensorRecord:
    id: str
    unit: Unit


@dataclass(frozen=True)
class MeasurementRecord:
    id: int
    sensor_id: str
    cow_id: str
    unit: Unit
    measured_at: int
    value: float | None
    quality: Quality

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.measured_at, tz=timezone.utc)


@dataclass(frozen=True)
class WindowStats:
    """Aggregate of one cow's readings of one unit inside a time window."""

    total: float | None  # sum of valid values, None when there is no valid reading
    mean: float | None
    valid_readings: int
    invalid_readings: int


@dataclass(frozen=True)
class LatestValue:
    value: float
    measured_at: int


def _epoch(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(moment.timestamp())


@typechecked
class Repository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # Cows and sensors are never deleted and a sensor's unit never changes, so
        # caching what has been seen saves two lookups on every measurement insert.
        self._sensor_units: dict[str, Unit] = {}
        self._known_cows: set[str] = set()
        self._lock = threading.Lock()

    @property
    def engine(self) -> Engine:
        return self._engine

    # --- cows -----------------------------------------------------------------
    def create_cow(self, cow_id: str, name: str, birthdate: date) -> CowRecord:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(cows).values(
                        id=cow_id, name=name, birthdate=birthdate, created_at=datetime.now(timezone.utc)
                    )
                )
        except IntegrityError as exc:
            raise AlreadyExistsError("cow", cow_id) from exc
        with self._lock:
            self._known_cows.add(cow_id)
        return CowRecord(cow_id, name, birthdate)

    def get_cow(self, cow_id: str) -> CowRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(cows.c.id, cows.c.name, cows.c.birthdate).where(cows.c.id == cow_id)
            ).first()
        return CowRecord(row.id, row.name, row.birthdate) if row else None

    def list_cows(self) -> list[CowRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(cows.c.id, cows.c.name, cows.c.birthdate)).all()
        return [CowRecord(r.id, r.name, r.birthdate) for r in rows]

    def _cow_exists(self, cow_id: str) -> bool:
        if cow_id in self._known_cows:
            return True
        if self.get_cow(cow_id) is None:
            return False
        with self._lock:
            self._known_cows.add(cow_id)
        return True

    # --- sensors --------------------------------------------------------------
    def create_sensor(self, sensor_id: str, unit: Unit) -> SensorRecord:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(sensors).values(
                        id=sensor_id, unit=unit.value, created_at=datetime.now(timezone.utc)
                    )
                )
        except IntegrityError as exc:
            raise AlreadyExistsError("sensor", sensor_id) from exc
        with self._lock:
            self._sensor_units[sensor_id] = unit
        return SensorRecord(sensor_id, unit)

    def get_sensor(self, sensor_id: str) -> SensorRecord | None:
        unit = self._sensor_units.get(sensor_id)
        if unit is not None:
            return SensorRecord(sensor_id, unit)
        with self._engine.connect() as conn:
            row = conn.execute(select(sensors.c.id, sensors.c.unit).where(sensors.c.id == sensor_id)).first()
        if row is None:
            return None
        with self._lock:
            self._sensor_units[sensor_id] = Unit(row.unit)
        return SensorRecord(row.id, Unit(row.unit))

    # --- measurements ---------------------------------------------------------
    def add_measurement(
        self, sensor_id: str, cow_id: str, measured_at: datetime, value: float | None
    ) -> tuple[MeasurementRecord, bool]:
        """Store one reading. Returns ``(record, created)``.

        Re-sending an identical reading returns the stored one with ``created=False``
        (safe retries); the same key with a different value raises ``ConflictError``.
        """
        sensor = self.get_sensor(sensor_id)
        if sensor is None:
            raise NotFoundError("sensor", sensor_id)
        if not self._cow_exists(cow_id):
            raise NotFoundError("cow", cow_id)

        epoch = _epoch(measured_at)
        quality = classify_value(sensor.unit, value)
        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    insert(measurements).values(
                        sensor_id=sensor_id,
                        cow_id=cow_id,
                        unit=sensor.unit.value,
                        measured_at=epoch,
                        value=value,
                        quality=quality.value,
                        received_at=datetime.now(timezone.utc),
                    )
                )
                new_id = result.inserted_primary_key[0] if result.inserted_primary_key else None
        except IntegrityError:
            existing = self._find_measurement(sensor_id, cow_id, epoch)
            if existing is None:  # pragma: no cover - integrity error was not the unique key
                raise
            if existing.value != value:
                raise ConflictError(
                    f"reading {sensor_id}/{cow_id}@{measured_at.isoformat()} already stored "
                    f"with value {existing.value}"
                ) from None
            return existing, False
        assert new_id is not None
        return MeasurementRecord(int(new_id), sensor_id, cow_id, sensor.unit, epoch, value, quality), True

    def _find_measurement(self, sensor_id: str, cow_id: str, epoch: int) -> MeasurementRecord | None:
        query = select(measurements).where(
            measurements.c.sensor_id == sensor_id,
            measurements.c.cow_id == cow_id,
            measurements.c.measured_at == epoch,
        )
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        return self._to_measurement(row) if row else None

    def latest_measurement(self, cow_id: str, unit: Unit | None = None) -> MeasurementRecord | None:
        """Most recent reading of a cow (any quality), optionally for one unit."""
        query = select(measurements).where(measurements.c.cow_id == cow_id)
        if unit is not None:
            query = query.where(measurements.c.unit == unit.value)
        query = query.order_by(measurements.c.measured_at.desc(), measurements.c.id.desc()).limit(1)
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        return self._to_measurement(row) if row else None

    @staticmethod
    def _to_measurement(row: object) -> MeasurementRecord:
        return MeasurementRecord(
            id=int(row.id),  # type: ignore[attr-defined]
            sensor_id=row.sensor_id,  # type: ignore[attr-defined]
            cow_id=row.cow_id,  # type: ignore[attr-defined]
            unit=Unit(row.unit),  # type: ignore[attr-defined]
            measured_at=int(row.measured_at),  # type: ignore[attr-defined]
            value=row.value,  # type: ignore[attr-defined]
            quality=Quality(row.quality),  # type: ignore[attr-defined]
        )

    # --- report queries -------------------------------------------------------
    # Aggregation is pushed down to the database; only one row per cow comes back.
    def window_stats(self, unit: Unit, start: datetime, end: datetime) -> dict[str, WindowStats]:
        """Per-cow aggregate of readings with ``start <= measured_at < end``."""
        m = measurements.c
        is_valid = m.quality == Quality.VALID.value
        query = (
            select(
                m.cow_id,
                func.sum(case((is_valid, m.value), else_=None)).label("total"),
                func.avg(case((is_valid, m.value), else_=None)).label("mean"),
                func.sum(case((is_valid, 1), else_=0)).label("valid"),
                func.sum(case((is_valid, 0), else_=1)).label("invalid"),
            )
            .where(m.unit == unit.value, m.measured_at >= _epoch(start), m.measured_at < _epoch(end))
            .group_by(m.cow_id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query).all()
        return {
            r.cow_id: WindowStats(
                total=None if r.total is None else float(r.total),
                mean=None if r.mean is None else float(r.mean),
                valid_readings=int(r.valid),
                invalid_readings=int(r.invalid),
            )
            for r in rows
        }

    def latest_valid_values(self, unit: Unit, end: datetime) -> dict[str, LatestValue]:
        """Per cow, the last valid reading strictly before ``end``."""
        m = measurements.c
        cut = _epoch(end)
        last = (
            select(m.cow_id, func.max(m.measured_at).label("ts"))
            .where(m.unit == unit.value, m.quality == Quality.VALID.value, m.measured_at < cut)
            .group_by(m.cow_id)
            .subquery()
        )
        query = (
            select(m.cow_id, m.value, m.measured_at)
            .join(last, and_(m.cow_id == last.c.cow_id, m.measured_at == last.c.ts))
            .where(m.unit == unit.value, m.quality == Quality.VALID.value)
            .order_by(m.cow_id, m.id)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query).all()
        result: dict[str, LatestValue] = {}
        for r in rows:  # two sensors at the same instant: keep the first stored
            result.setdefault(r.cow_id, LatestValue(float(r.value), int(r.measured_at)))
        return result

    def active_cow_ids(self, start: datetime, end: datetime) -> set[str]:
        """Cows with at least one reading (any quality) in ``[start, end)``."""
        m = measurements.c
        query = select(m.cow_id).where(m.measured_at >= _epoch(start), m.measured_at < _epoch(end)).distinct()
        with self._engine.connect() as conn:
            return {r.cow_id for r in conn.execute(query)}

    def quality_counts(self, start: datetime, end: datetime) -> dict[tuple[Unit, Quality], int]:
        m = measurements.c
        query = (
            select(m.unit, m.quality, func.count().label("n"))
            .where(m.measured_at >= _epoch(start), m.measured_at < _epoch(end))
            .group_by(m.unit, m.quality)
        )
        with self._engine.connect() as conn:
            return {(Unit(r.unit), Quality(r.quality)): int(r.n) for r in conn.execute(query)}

    def faulty_sensors(self, start: datetime, end: datetime, limit: int = 5) -> list[tuple[str, int, int]]:
        """Sensors with the most invalid readings: ``(sensor_id, invalid, total)``."""
        m = measurements.c
        invalid = func.sum(case((m.quality == Quality.VALID.value, 0), else_=1))
        query = (
            select(m.sensor_id, invalid.label("invalid"), func.count().label("total"))
            .where(m.measured_at >= _epoch(start), m.measured_at < _epoch(end))
            .group_by(m.sensor_id)
            .having(invalid > 0)
            .order_by(invalid.desc(), m.sensor_id)
            .limit(limit)
        )
        with self._engine.connect() as conn:
            return [(r.sensor_id, int(r.invalid), int(r.total)) for r in conn.execute(query)]
