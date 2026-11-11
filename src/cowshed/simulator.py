"""Sensor simulator: replays the provided parquet datasets against the API.

Each record is sent as its own HTTP request, as a real sensor would. Cows and sensors
are registered first, then measurements are replayed in chronological order.
A bounded pool of async workers keeps a configurable number of requests in flight.
Each worker owns its own HTTP client with one keep-alive connection: httpx's shared
connection pool scans every connection on each request, which made a single shared
client CPU-bound at roughly 80 requests/s.
Re-running the simulator is safe: existing cows/sensors (409) and already stored
readings (200) are counted, not treated as failures.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pyarrow.compute as pc
import pyarrow.parquet as pq
from typeguard import typechecked

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

ClientFactory = Callable[[], httpx.AsyncClient]


@dataclass
class SimulationStats:
    sent: int = 0
    created: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed: int = 0
    rejected_samples: list[str] = field(default_factory=list)

    def record(self, status_code: int, detail: str) -> None:
        self.sent += 1
        if status_code == 201:
            self.created += 1
        elif status_code in (200, 409):
            self.duplicates += 1
        elif status_code < 0:
            self.failed += 1
        else:
            self.rejected += 1
            if len(self.rejected_samples) < 10:
                self.rejected_samples.append(f"{status_code}: {detail}")


@dataclass
class SimulationResult:
    cows: SimulationStats
    sensors: SimulationStats
    measurements: SimulationStats


def _read(path: Path, columns: list[str]) -> Any:
    return pq.ParquetFile(path).read(columns=columns)


def _clean_value(value: float | None) -> float | None:
    if value is None or math.isnan(value):
        return None
    return float(value)


@typechecked
def cow_requests(data_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for row in _read(data_dir / "cows.parquet", ["id", "name", "birthdate"]).to_pylist():
        birthdate: date = row["birthdate"]
        yield f"/cows/{row['id']}", {"name": row["name"], "birthdate": birthdate.isoformat()}


@typechecked
def sensor_requests(data_dir: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    for row in _read(data_dir / "sensors.parquet", ["id", "unit"]).to_pylist():
        yield f"/sensors/{row['id']}", {"unit": row["unit"]}


@typechecked
def measurement_requests(
    data_dir: Path, limit: int | None = None, since: date | None = None
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Readings in chronological order, optionally only from ``since`` (UTC) and/or the first ``limit``."""
    table = _read(data_dir / "measurements.parquet", ["sensor_id", "cow_id", "timestamp", "value"])
    if since is not None:
        cutoff = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        table = table.filter(pc.greater_equal(table["timestamp"], cutoff))
    table = table.sort_by([("timestamp", "ascending")])  # replay in the order sensors produced them
    if limit is not None:
        table = table.slice(0, limit)
    for batch in table.to_batches(max_chunksize=10_000):
        for row in batch.to_pylist():
            moment = datetime.fromtimestamp(row["timestamp"], tz=timezone.utc)
            yield (
                f"/sensors/{row['sensor_id']}/measurements",
                {
                    "cow_id": row["cow_id"],
                    "timestamp": moment.isoformat(),
                    "value": _clean_value(row["value"]),
                },
            )


async def _post(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], retries: int
) -> tuple[int, str]:
    delay = 0.2
    for attempt in range(retries + 1):
        try:
            response = await client.post(url, json=payload)
        except httpx.TransportError as exc:
            detail = repr(exc)
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response.status_code, response.text[:300]
            detail = f"HTTP {response.status_code}"
        if attempt < retries:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    log.error("giving up on %s: %s", url, detail)
    return -1, detail


async def send_all(
    clients: list[httpx.AsyncClient],
    requests: Iterator[tuple[str, dict[str, Any]]],
    retries: int = 5,
    label: str = "records",
    progress_every: int = 50_000,
) -> SimulationStats:
    """Send every request individually; one worker per client, one request in flight each."""
    stats = SimulationStats()
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue(maxsize=len(clients) * 4)

    async def worker(client: httpx.AsyncClient) -> None:
        while (item := await queue.get()) is not None:
            code, detail = await _post(client, item[0], item[1], retries)
            stats.record(code, detail)
            if stats.sent % progress_every == 0:
                log.info("%s: %d sent", label, stats.sent)

    workers = [asyncio.create_task(worker(client)) for client in clients]
    for item in requests:
        await queue.put(item)
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers)
    log.info(
        "%s: sent=%d created=%d duplicates=%d rejected=%d failed=%d",
        label,
        stats.sent,
        stats.created,
        stats.duplicates,
        stats.rejected,
        stats.failed,
    )
    return stats


async def run_simulation(
    make_client: ClientFactory,
    data_dir: Path,
    concurrency: int = 8,
    limit: int | None = None,
    since: date | None = None,
) -> SimulationResult:
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    for name in ("cows", "sensors", "measurements"):
        if not (data_dir / f"{name}.parquet").is_file():
            raise FileNotFoundError(data_dir / f"{name}.parquet")
    async with AsyncExitStack() as stack:
        clients = [await stack.enter_async_context(make_client()) for _ in range(concurrency)]
        cows = await send_all(clients, cow_requests(data_dir), label="cows")
        sensors = await send_all(clients, sensor_requests(data_dir), label="sensors")
        readings = await send_all(clients, measurement_requests(data_dir, limit, since), label="measurements")
    return SimulationResult(cows, sensors, readings)
