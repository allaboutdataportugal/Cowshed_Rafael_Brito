"""Database schema and engine factory (SQLAlchemy Core).

SQLite is the default so the project runs with zero infrastructure; the schema is
portable, and pointing ``COWSHED_DATABASE_URL`` at PostgreSQL works unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import make_url

metadata = MetaData()

cows = Table(
    "cows",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("birthdate", Date, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

sensors = Table(
    "sensors",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("unit", String(8), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

measurements = Table(
    "measurements",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sensor_id", String(36), ForeignKey("sensors.id"), nullable=False),
    Column("cow_id", String(36), ForeignKey("cows.id"), nullable=False),
    # Denormalised from the sensor so reports never need a join on the hot table.
    Column("unit", String(8), nullable=False),
    # Epoch seconds (UTC). Portable across databases and free of timezone ambiguity.
    Column("measured_at", BigInteger, nullable=False),
    Column("value", Float, nullable=True),  # raw value as received, may be null
    Column("quality", String(16), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    # A sensor reports a cow at most once per instant: makes ingestion idempotent.
    UniqueConstraint("sensor_id", "cow_id", "measured_at", name="uq_measurement"),
    Index("ix_measurements_cow_unit_time", "cow_id", "unit", "measured_at"),
    Index("ix_measurements_unit_time", "unit", "measured_at"),
)


def create_db_engine(database_url: str, sqlite_synchronous: str = "FULL") -> Engine:
    """Create an engine. For SQLite: create the folder, enable WAL and foreign keys."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(database_url, pool_pre_ping=True)

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})
    synchronous = sqlite_synchronous.upper()

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
        cursor.execute(f"PRAGMA synchronous={synchronous}")  # FULL: committed = on disk
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create missing tables. Idempotent; a real deployment would use Alembic migrations."""
    metadata.create_all(engine)
