from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cowshed.domain import IllnessThresholds
from cowshed.jobs import next_run, run_daily_report
from cowshed.reporting import build_daily_report, day_window, render_json, render_text
from cowshed.repository import Repository
from tests.conftest import COW_A, COW_B, MILK_SENSOR, WEIGHT_SENSOR, every_hours, utc

UTC = ZoneInfo("UTC")


def _add(
    repo: Repository, sensor: str, cow: str, moments: list[datetime], values: list[float | None]
) -> None:
    for moment, value in zip(moments, values, strict=True):
        repo.add_measurement(sensor, cow, moment, value)


def test_milk_per_day_sums_only_valid_readings(herd: Repository) -> None:
    moments = every_hours(utc(2024, 7, 10, 2), 4, 6)  # 02h .. 22h
    _add(herd, MILK_SENSOR, COW_A, moments, [4.5, 5.0, None, -1.0, 0.0, 4.5])
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 11, 2)], [4.9])  # next day, excluded

    report = build_daily_report(herd, date(2024, 7, 10), UTC)
    row = next(r for r in report.cows if r.cow_id == COW_A)
    assert row.milk_liters == 14.0
    assert (row.milk_readings, row.milk_invalid_readings) == (3, 3)
    assert report.farm_milk_liters == 14.0
    assert report.data_quality.readings_by_quality["milk"] == {
        "missing": 1,
        "negative": 1,
        "valid": 3,
        "zero": 1,
    }


def test_cow_with_only_invalid_milk_has_no_figure_rather_than_zero(herd: Repository) -> None:
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 10, 2)], [None])
    report = build_daily_report(herd, date(2024, 7, 10), UTC)
    row = next(r for r in report.cows if r.cow_id == COW_A)
    assert row.milk_liters is None
    assert report.data_quality.cows_without_valid_milk == [f"Bella [{COW_A[:8]}]"]


def test_day_boundaries_follow_the_report_timezone(herd: Repository) -> None:
    amsterdam = ZoneInfo("Europe/Amsterdam")  # UTC+2 in July
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 9, 22, 30), utc(2024, 7, 10, 21, 30)], [4.0, 5.0])
    # 22:30 UTC on the 9th is 00:30 on the 10th in Amsterdam; 21:30 UTC is 23:30 local.
    row = build_daily_report(herd, date(2024, 7, 10), amsterdam).cows[0]
    assert row.milk_liters == 9.0
    assert build_daily_report(herd, date(2024, 7, 10), UTC).cows[0].milk_liters == 5.0


def test_dst_day_is_23_hours(herd: Repository) -> None:
    start, end = day_window(date(2024, 3, 31), ZoneInfo("Europe/Amsterdam"))
    assert end.timestamp() - start.timestamp() == 23 * 3600


def test_current_weight_and_30_day_average(herd: Repository) -> None:
    _add(herd, WEIGHT_SENSOR, COW_A, [utc(2024, 6, 1, 10)], [700.0])  # older than 30 days
    _add(herd, WEIGHT_SENSOR, COW_A, [utc(2024, 7, 1, 10), utc(2024, 7, 5, 10)], [540.0, 560.0])
    _add(herd, WEIGHT_SENSOR, COW_A, [utc(2024, 7, 10, 10)], [-1.0])  # invalid, ignored
    _add(herd, WEIGHT_SENSOR, COW_A, [utc(2024, 7, 11, 10)], [999.0])  # after the report date

    row = build_daily_report(herd, date(2024, 7, 10), UTC).cows[0]
    assert row.current_weight_kg == 560.0
    assert row.current_weight_measured_at == datetime(2024, 7, 5, 10, tzinfo=timezone.utc)
    assert row.avg_weight_30d_kg == 550.0
    assert row.weight_readings_30d == 2


def test_report_is_reproducible_for_a_past_date(herd: Repository) -> None:
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 1, 10)], [4.5])
    before = build_daily_report(herd, date(2024, 7, 1), UTC)
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 2, 10)], [4.9])  # later data must not change it
    after = build_daily_report(herd, date(2024, 7, 1), UTC)
    assert before.cows == after.cows


def test_inactive_cows_are_left_out(herd: Repository) -> None:
    _add(herd, MILK_SENSOR, COW_B, [utc(2024, 1, 1, 10)], [4.5])  # last seen months earlier
    _add(herd, MILK_SENSOR, COW_A, [utc(2024, 7, 10, 10)], [4.5])
    assert [r.cow_id for r in build_daily_report(herd, date(2024, 7, 10), UTC).cows] == [COW_A]


@pytest.fixture
def herd_with_sick_cow(herd: Repository) -> Repository:
    """Both cows weigh ~550 kg for a month; Daisy then loses 15% in the last week."""
    moments = every_hours(utc(2024, 6, 1, 2), 8, 3 * 40)  # 40 days, 3 weighings a day
    _add(herd, WEIGHT_SENSOR, COW_A, moments, [550.0 + (i % 3) for i in range(len(moments))])
    end = utc(2024, 7, 11)
    daisy = [550.0 if m < end - timedelta(days=7) else 470.0 for m in moments]
    _add(herd, WEIGHT_SENSOR, COW_B, moments, daisy)
    _add(herd, MILK_SENSOR, COW_A, moments, [4.7] * len(moments))
    _add(herd, MILK_SENSOR, COW_B, moments, [4.7] * len(moments))
    return herd


def test_weight_loss_is_flagged_as_possible_illness(herd_with_sick_cow: Repository) -> None:
    report = build_daily_report(herd_with_sick_cow, date(2024, 7, 10), UTC)
    assert [s.cow_id for s in report.possible_illness] == [COW_B]
    assert report.possible_illness[0].reasons[0].startswith("weight down 14.5%")


def test_milk_drop_is_flagged(herd: Repository) -> None:
    moments = every_hours(utc(2024, 6, 1, 2), 8, 3 * 40)
    drop_from = utc(2024, 7, 8)
    _add(herd, MILK_SENSOR, COW_A, moments, [2.0 if m >= drop_from else 4.8 for m in moments])
    report = build_daily_report(herd, date(2024, 7, 10), UTC)
    assert report.possible_illness[0].reasons[0].startswith("milk yield per milking down")


def test_thresholds_are_configurable(herd_with_sick_cow: Repository) -> None:
    lenient = IllnessThresholds(weight_loss_pct=20.0)
    assert build_daily_report(herd_with_sick_cow, date(2024, 7, 10), UTC, lenient).possible_illness == []


def test_text_and_json_rendering(herd_with_sick_cow: Repository) -> None:
    report = build_daily_report(herd_with_sick_cow, date(2024, 7, 10), UTC)
    text = render_text(report)
    for section in ("MILK PRODUCTION PER COW", "WEIGHT PER COW", "POSSIBLE ILLNESS (1 cows)", "DATA QUALITY"):
        assert section in text
    assert f"Daisy [{COW_B[:8]}]" in text
    assert '"report_date": "2024-07-10"' in render_json(report)


def test_daily_job_writes_files_and_is_idempotent(herd_with_sick_cow: Repository, tmp_path: Path) -> None:
    out = tmp_path / "reports"
    first = run_daily_report(herd_with_sick_cow, date(2024, 7, 10), UTC, str(out))
    second = run_daily_report(herd_with_sick_cow, date(2024, 7, 10), UTC, str(out))
    assert first == second
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == [
        "cowshed_report_2024-07-10.json",
        "cowshed_report_2024-07-10.txt",
    ]


def test_next_run() -> None:
    tz = ZoneInfo("Europe/Amsterdam")
    at = datetime(2024, 7, 10, 0, 30, tzinfo=tz)
    assert next_run(at, datetime.min.time().replace(hour=1)) == datetime(2024, 7, 10, 1, 0, tzinfo=tz)
    later = datetime(2024, 7, 10, 1, 0, tzinfo=tz)
    assert next_run(later, datetime.min.time().replace(hour=1)) == datetime(2024, 7, 11, 1, 0, tzinfo=tz)
