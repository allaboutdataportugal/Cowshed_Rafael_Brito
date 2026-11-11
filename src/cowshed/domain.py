"""Domain vocabulary and the rules for judging a sensor reading.

Every reading is stored as received (nothing is thrown away) and tagged with a
``Quality``. Only ``VALID`` readings take part in aggregates and reports.

Assumptions, based on profiling the provided datasets (563,627 readings):

* ``null`` -> ``MISSING``: the sensor fired but produced no value (3,393 readings).
* negative -> ``NEGATIVE``: milk volume and body weight cannot be negative. The only
  negative value in the data is exactly ``-1`` (3,425 readings, both units), which is a
  typical "sensor error" sentinel.
* ``0`` -> ``ZERO``: a 0 kg cow is impossible. For milk, valid readings are never below
  4.5 L, and zeros appear at exactly the same rate relative to ``-1`` for both units
  (~2.05x), so they share the fault signature rather than meaning "no milk". A cow
  that is genuinely not milked (dry period) produces no milking event at all.
* above a physical ceiling -> ``OUT_OF_RANGE``: no single reading can exceed
  ``PLAUSIBLE_MAX`` (generous ceilings: a record cow gives < 100 L/day; the heaviest
  dairy breeds stay well below 1,500 kg).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from typeguard import typechecked


class Unit(StrEnum):
    """Unit a sensor reports in. It identifies what the sensor measures."""

    LITERS = "L"
    KILOGRAMS = "kg"

    @property
    def measure(self) -> str:
        return "milk" if self is Unit.LITERS else "weight"


class Quality(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    NEGATIVE = "negative"
    ZERO = "zero"
    OUT_OF_RANGE = "out_of_range"


PLAUSIBLE_MAX: dict[Unit, float] = {
    Unit.LITERS: 60.0,  # per single milking event
    Unit.KILOGRAMS: 1500.0,
}


@typechecked
def classify_value(unit: Unit, value: float | None) -> Quality:
    """Return the quality tag of a reading. See the module docstring for the rules."""
    if value is None:
        return Quality.MISSING
    if value < 0:
        return Quality.NEGATIVE
    if value == 0:
        return Quality.ZERO
    if value > PLAUSIBLE_MAX[unit]:
        return Quality.OUT_OF_RANGE
    return Quality.VALID


@dataclass(frozen=True)
class IllnessThresholds:
    """Rules for the "possibly ill" signal.

    The mean of valid readings in a *recent* window is compared with a *baseline*
    window that ends a week earlier, so a gradual decline is not hidden inside its
    own baseline. Windows are relative to the end of the report date.
    """

    recent_days: int = 3
    baseline_from_days: int = 30
    baseline_to_days: int = 7
    weight_loss_pct: float = 5.0
    milk_drop_pct: float = 15.0
    min_readings: int = 3

    def __post_init__(self) -> None:
        if not 0 < self.recent_days <= self.baseline_to_days < self.baseline_from_days:
            raise ValueError("expected 0 < recent_days <= baseline_to_days < baseline_from_days")
