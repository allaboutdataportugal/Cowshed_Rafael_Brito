"""Daily farm report: milk per cow, weight per cow, possible illness, data quality.

A report is a pure function of (database contents, report date, timezone), so it can
be generated for any past date and regenerated at any time with the same result.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from typeguard import typechecked

from cowshed.domain import IllnessThresholds, Unit
from cowshed.repository import Repository, WindowStats

WEIGHT_AVG_DAYS = 30


class CowDailyStats(BaseModel):
    cow_id: str
    name: str
    milk_liters: float | None
    milk_readings: int
    milk_invalid_readings: int
    current_weight_kg: float | None
    current_weight_measured_at: datetime | None
    avg_weight_30d_kg: float | None
    weight_readings_30d: int


class IllnessSignal(BaseModel):
    cow_id: str
    name: str
    reasons: list[str]


class SensorFaults(BaseModel):
    sensor_id: str
    invalid_readings: int
    total_readings: int


class DataQuality(BaseModel):
    readings_by_quality: dict[str, dict[str, int]]
    cows_without_valid_milk: list[str]
    faulty_sensors: list[SensorFaults]


class DailyReport(BaseModel):
    report_date: date
    timezone: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    cows: list[CowDailyStats]
    farm_milk_liters: float
    possible_illness: list[IllnessSignal]
    data_quality: DataQuality


@typechecked
def day_window(report_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """``[start, end)`` of a calendar day in ``tz``. DST days are 23h or 25h long."""
    start = datetime.combine(report_date, time.min, tzinfo=tz)
    end = datetime.combine(report_date + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def _pct_change(recent: WindowStats | None, baseline: WindowStats | None, min_readings: int) -> float | None:
    if recent is None or baseline is None:
        return None
    if recent.valid_readings < min_readings or baseline.valid_readings < min_readings:
        return None
    if recent.mean is None or not baseline.mean:
        return None
    return (recent.mean - baseline.mean) / baseline.mean * 100


@typechecked
def detect_illness(
    repo: Repository, end: datetime, thresholds: IllnessThresholds, names: dict[str, str]
) -> list[IllnessSignal]:
    """Flag cows whose recent weight or milk yield dropped clearly below their own baseline.

    Comparing each cow with herself (not with the herd) avoids false alarms for cows
    that are simply lighter or lower-yielding than average.
    """
    day = timedelta(days=1)
    recent = (end - thresholds.recent_days * day, end)
    baseline = (end - thresholds.baseline_from_days * day, end - thresholds.baseline_to_days * day)
    rules = (
        (Unit.KILOGRAMS, thresholds.weight_loss_pct, "weight", "kg"),
        (Unit.LITERS, thresholds.milk_drop_pct, "milk yield per milking", "L"),
    )
    reasons: dict[str, list[str]] = {}
    for unit, limit, label, symbol in rules:
        now_stats = repo.window_stats(unit, *recent)
        base_stats = repo.window_stats(unit, *baseline)
        for cow_id, stats in now_stats.items():
            base = base_stats.get(cow_id)
            change = _pct_change(stats, base, thresholds.min_readings)
            if change is not None and change <= -limit and base is not None:
                reasons.setdefault(cow_id, []).append(
                    f"{label} down {-change:.1f}%: {stats.mean:.1f} {symbol} (last {thresholds.recent_days}d)"
                    f" vs {base.mean:.1f} {symbol} (days -{thresholds.baseline_from_days}"
                    f" to -{thresholds.baseline_to_days})"
                )
    return sorted(
        (IllnessSignal(cow_id=c, name=names.get(c, "?"), reasons=r) for c, r in reasons.items()),
        key=lambda s: (s.name, s.cow_id),
    )


@typechecked
def build_daily_report(
    repo: Repository,
    report_date: date,
    tz: ZoneInfo,
    thresholds: IllnessThresholds | None = None,
) -> DailyReport:
    thresholds = thresholds or IllnessThresholds()
    start, end = day_window(report_date, tz)
    month_start = end - timedelta(days=WEIGHT_AVG_DAYS)

    names = {c.id: c.name for c in repo.list_cows()}
    # A cow is "on the farm" if any sensor saw her in the last 30 days; a cow that
    # left or died disappears from the report instead of showing up empty forever.
    active = repo.active_cow_ids(month_start, end)

    milk = repo.window_stats(Unit.LITERS, start, end)
    weight_30d = repo.window_stats(Unit.KILOGRAMS, month_start, end)
    current_weight = repo.latest_valid_values(Unit.KILOGRAMS, end)

    rows: list[CowDailyStats] = []
    for cow_id in active:
        m = milk.get(cow_id)
        w = weight_30d.get(cow_id)
        cw = current_weight.get(cow_id)
        rows.append(
            CowDailyStats(
                cow_id=cow_id,
                name=names.get(cow_id, "?"),
                milk_liters=None if m is None or m.total is None else round(m.total, 2),
                milk_readings=m.valid_readings if m else 0,
                milk_invalid_readings=m.invalid_readings if m else 0,
                current_weight_kg=round(cw.value, 2) if cw else None,
                current_weight_measured_at=datetime.fromtimestamp(cw.measured_at, tz) if cw else None,
                avg_weight_30d_kg=None if w is None or w.mean is None else round(w.mean, 2),
                weight_readings_30d=w.valid_readings if w else 0,
            )
        )
    rows.sort(key=lambda r: (r.name, r.cow_id))

    counts: dict[str, dict[str, int]] = {u.measure: {} for u in Unit}
    for (unit, quality), n in sorted(repo.quality_counts(start, end).items()):
        counts[unit.measure][quality.value] = n

    return DailyReport(
        report_date=report_date,
        timezone=str(tz),
        window_start=start,
        window_end=end,
        generated_at=datetime.now(timezone.utc),
        cows=rows,
        farm_milk_liters=round(sum(r.milk_liters or 0.0 for r in rows), 2),
        possible_illness=[s for s in detect_illness(repo, end, thresholds, names) if s.cow_id in active],
        data_quality=DataQuality(
            readings_by_quality=counts,
            cows_without_valid_milk=[_label(r.name, r.cow_id) for r in rows if r.milk_liters is None],
            faulty_sensors=[
                SensorFaults(sensor_id=s, invalid_readings=i, total_readings=t)
                for s, i, t in repo.faulty_sensors(start, end)
            ],
        ),
    )


# --- rendering ------------------------------------------------------------------
def _label(name: str, cow_id: str) -> str:
    # Names are not unique in the herd (e.g. two "Mary #8"), so always add a short id.
    return f"{name} [{cow_id[:8]}]"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        padded = (
            c.rjust(w) if i in right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )
        return "  ".join(padded).rstrip()

    return [fmt(headers), "  ".join("-" * w for w in widths), *(fmt(r) for r in rows)]


@typechecked
def render_text(report: DailyReport) -> str:
    title = f"COWSHED DAILY REPORT - {report.report_date.isoformat()}"
    out = [
        "=" * len(title),
        title,
        "=" * len(title),
        f"Day window : {report.window_start.isoformat()} -> {report.window_end.isoformat()}",
        f"Timezone   : {report.timezone}",
        f"Generated  : {report.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Cows       : {len(report.cows)} active (seen by a sensor in the last {WEIGHT_AVG_DAYS} days)",
        "",
        "1. MILK PRODUCTION PER COW",
        "   Sum of valid milk readings during the day. Invalid readings are excluded and counted.",
        "",
    ]
    milk_rows = [
        [_label(r.name, r.cow_id), _num(r.milk_liters, 2), str(r.milk_readings), str(r.milk_invalid_readings)]
        for r in report.cows
    ]
    out += ["   " + line for line in _table(["Cow", "Milk (L)", "Milkings", "Invalid"], milk_rows, {1, 2, 3})]
    out += ["", f"   Farm total: {_num(report.farm_milk_liters, 2)} L", ""]

    out += [
        "2. WEIGHT PER COW",
        "   Current = last valid reading up to the end of the day.",
        f"   Average = mean of valid readings over the last {WEIGHT_AVG_DAYS} days.",
        "",
    ]
    weight_rows = [
        [
            _label(r.name, r.cow_id),
            _num(r.current_weight_kg),
            r.current_weight_measured_at.strftime("%Y-%m-%d %H:%M")
            if r.current_weight_measured_at
            else "n/a",
            _num(r.avg_weight_30d_kg),
            str(r.weight_readings_30d),
        ]
        for r in report.cows
    ]
    out += [
        "   " + line
        for line in _table(
            ["Cow", "Current (kg)", "Measured at", "Avg 30d (kg)", "Readings 30d"], weight_rows, {1, 3, 4}
        )
    ]
    out += ["", f"3. POSSIBLE ILLNESS ({len(report.possible_illness)} cows)"]
    if not report.possible_illness:
        out.append("   None detected.")
    for signal in report.possible_illness:
        out.append(f"   ! {_label(signal.name, signal.cow_id)}")
        out += [f"       - {reason}" for reason in signal.reasons]

    dq = report.data_quality
    out += ["", "4. DATA QUALITY (readings received during the day)"]
    for measure, by_quality in dq.readings_by_quality.items():
        total = sum(by_quality.values())
        detail = ", ".join(f"{q}={n}" for q, n in by_quality.items()) or "no readings"
        out.append(f"   {measure:<7}: {total} readings ({detail})")
    if dq.cows_without_valid_milk:
        out.append(f"   Cows without any valid milk reading: {len(dq.cows_without_valid_milk)}")
    if dq.faulty_sensors:
        out.append(
            "   Sensors with invalid readings: "
            + ", ".join(
                f"{s.sensor_id[:8]} ({s.invalid_readings}/{s.total_readings})" for s in dq.faulty_sensors
            )
        )
    return "\n".join(out) + "\n"


def render_json(report: DailyReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2) + "\n"
