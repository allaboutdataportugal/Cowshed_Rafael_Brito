"""Generate a small synthetic dataset with the same schema as the provided parquet files.

Useful for CI and for trying the project without the original data:
    python scripts/make_sample_data.py ./sample_data

Three cows over 45 days ending 2024-07-10, one reading every 4 hours (alternating milk
and weight), a few null / -1 / 0 readings, and one cow ("Sick #1") whose weight drops
about 15% in the last two weeks, so the report must flag her.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

COWS = [
    ("c0000000-0000-4000-8000-000000000001", "Healthy #1", date(2020, 3, 1)),
    ("c0000000-0000-4000-8000-000000000002", "Healthy #2", date(2021, 5, 9)),
    ("c0000000-0000-4000-8000-000000000003", "Sick #1", date(2020, 11, 23)),
]
MILK = "5e000000-0000-4000-8000-00000000000a"
WEIGHT = "5e000000-0000-4000-8000-00000000000b"
BROKEN = "5e000000-0000-4000-8000-00000000000c"  # a weight sensor that only returns garbage
END = datetime(2024, 7, 11, tzinfo=timezone.utc)
DAYS = 45


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "id": [c[0] for c in COWS],
                "name": [c[1] for c in COWS],
                "birthdate": pa.array([c[2] for c in COWS], pa.date32()),
            }
        ),
        out / "cows.parquet",
    )
    pq.write_table(
        pa.table({"id": [MILK, WEIGHT, BROKEN], "unit": ["L", "kg", "kg"]}), out / "sensors.parquet"
    )

    sensor_ids, cow_ids, stamps, values = [], [], [], []
    start = END - timedelta(days=DAYS)
    for n, (cow_id, name, _) in enumerate(COWS):
        moment = start + timedelta(hours=2 + n)
        i = 0
        while moment < END:
            days_left = (END - moment).total_seconds() / 86400
            if i % 2 == 0:
                sensor, value = MILK, 4.5 + ((i * 7 + n) % 50) / 100
            else:
                loss = max(0.0, (14 - days_left) / 14) * 0.15 if name.startswith("Sick") else 0.0
                sensor, value = WEIGHT, round(550 * (1 - loss) + (i % 5) * 0.3, 2)
            if i % 37 == 0:
                sensor, value = BROKEN, [None, -1.0, 0.0][i % 3]
            sensor_ids.append(sensor)
            cow_ids.append(cow_id)
            stamps.append(moment.timestamp())
            values.append(value)
            moment += timedelta(hours=4)
            i += 1

    pq.write_table(
        pa.table(
            {
                "sensor_id": sensor_ids,
                "cow_id": cow_ids,
                "timestamp": pa.array(stamps, pa.float64()),
                "value": pa.array(values, pa.float64()),
            }
        ),
        out / "measurements.parquet",
    )
    print(f"wrote {len(COWS)} cows, 3 sensors, {len(stamps)} measurements to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data"))
