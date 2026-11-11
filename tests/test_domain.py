from __future__ import annotations

import pytest
from typeguard import TypeCheckError

from cowshed.domain import IllnessThresholds, Quality, Unit, classify_value


@pytest.mark.parametrize(
    ("unit", "value", "expected"),
    [
        (Unit.LITERS, 4.75, Quality.VALID),
        (Unit.KILOGRAMS, 549.8, Quality.VALID),
        (Unit.LITERS, None, Quality.MISSING),
        (Unit.KILOGRAMS, None, Quality.MISSING),
        (Unit.LITERS, -1.0, Quality.NEGATIVE),
        (Unit.KILOGRAMS, -0.01, Quality.NEGATIVE),
        (Unit.LITERS, 0.0, Quality.ZERO),
        (Unit.KILOGRAMS, 0.0, Quality.ZERO),
        (Unit.LITERS, 60.0, Quality.VALID),
        (Unit.LITERS, 60.01, Quality.OUT_OF_RANGE),
        (Unit.KILOGRAMS, 1500.01, Quality.OUT_OF_RANGE),
    ],
)
def test_classify_value(unit: Unit, value: float | None, expected: Quality) -> None:
    assert classify_value(unit, value) is expected


def test_type_hints_are_enforced_at_runtime() -> None:
    with pytest.raises(TypeCheckError):
        classify_value("L", 4.5)  # type: ignore[arg-type]
    with pytest.raises(TypeCheckError):
        classify_value(Unit.LITERS, "4.5")  # type: ignore[arg-type]


def test_unit_measure_names() -> None:
    assert Unit.LITERS.measure == "milk"
    assert Unit.KILOGRAMS.measure == "weight"


def test_illness_thresholds_validate_windows() -> None:
    with pytest.raises(ValueError):
        IllnessThresholds(recent_days=10, baseline_to_days=7)
