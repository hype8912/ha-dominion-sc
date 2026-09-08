"""Chunk 2 tests for coordinator: cost calc + billing gap."""

from datetime import date, datetime

from custom_components.dominionsc.const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
)
from custom_components.dominionsc.coordinator import (
    _billing_cycle_get_gap,
    _calculate_cost_for_wh,
)
from custom_components.dominionsc.rates import SC_RATE_8


def test_cost_none() -> None:
    assert (
        _calculate_cost_for_wh(100, datetime(2025, 7, 1), 0, COST_MODE_NONE, 0.0, None)
        == 0.0
    )


def test_cost_fixed() -> None:
    assert (
        _calculate_cost_for_wh(
            1000, datetime(2025, 7, 1), 0, COST_MODE_FIXED, 0.15, None
        )
        == 0.15
    )


def test_cost_tiered_before_effective() -> None:
    assert (
        _calculate_cost_for_wh(
            100, datetime(2025, 1, 1), 0, COST_MODE_RATE_8, 0, SC_RATE_8
        )
        == 0.0
    )


def test_cost_tiered_after_effective() -> None:
    assert (
        _calculate_cost_for_wh(
            100, datetime(2025, 8, 1), 0, COST_MODE_RATE_8, 0, SC_RATE_8
        )
        > 0
    )


def test_billing_gap_january() -> None:
    assert _billing_cycle_get_gap(date(2025, 1, 1)) == 30


def test_billing_gap_april_near_leap() -> None:
    assert _billing_cycle_get_gap(date(2024, 4, 1)) == 31  # leap year
    assert _billing_cycle_get_gap(date(2025, 4, 1)) == 30  # non-leap


def test_billing_gap_june() -> None:
    assert _billing_cycle_get_gap(date(2025, 6, 1)) == 31
