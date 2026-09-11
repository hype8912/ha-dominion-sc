"""Tests for dominionsc rates adapter."""

from datetime import date

from dominionsc import RATE_2, RATE_5, RATE_6, RATE_7, RATE_8, RatePlan

from custom_components.dominionsc.const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_2,
    COST_MODE_RATE_5,
    COST_MODE_RATE_6,
    COST_MODE_RATE_7,
    COST_MODE_RATE_8,
)
from custom_components.dominionsc.rates import (
    HISTORICAL_RATE_REGISTRY,
    RATE_PLAN_REGISTRY,
    _RATE_6_2025,
    _RATE_8_2025,
    _HistoricalTieredRate,
    build_cost_mode_choices,
)


def test_rate_plan_registry_contains_rate_8() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_8)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_8


def test_rate_plan_registry_contains_rate_6() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_6)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_6


def test_rate_plan_registry_only_has_supported_modes() -> None:
    assert set(RATE_PLAN_REGISTRY.keys()) == {
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
    }


def test_build_cost_mode_choices_contains_expected_keys() -> None:
    choices = build_cost_mode_choices()
    assert COST_MODE_NONE in choices
    assert COST_MODE_FIXED in choices
    assert COST_MODE_RATE_8 in choices
    assert COST_MODE_RATE_6 in choices


def test_build_cost_mode_choices_ordering() -> None:
    choices = build_cost_mode_choices()
    keys = list(choices)
    assert keys[0] == COST_MODE_NONE
    assert keys[-1] == COST_MODE_FIXED


def test_build_cost_mode_choices_uses_library_plan_name() -> None:
    choices = build_cost_mode_choices()
    assert choices[COST_MODE_RATE_8] == RATE_8.name
    assert choices[COST_MODE_RATE_6] == RATE_6.name


# ---------------------------------------------------------------------------
# Phase 2: Historical rate registry
# ---------------------------------------------------------------------------


def test_historical_rate_registry_keys_match_cost_modes() -> None:
    assert set(HISTORICAL_RATE_REGISTRY.keys()) == {COST_MODE_RATE_8, COST_MODE_RATE_6}


def test_historical_rate_registry_values_are_lists_of_historical_rate() -> None:
    for entries in HISTORICAL_RATE_REGISTRY.values():
        assert isinstance(entries, list)
        assert all(isinstance(e, _HistoricalTieredRate) for e in entries)


def test_rate_8_2025_correct_dates() -> None:
    assert _RATE_8_2025.effective_from == date(2025, 7, 23)
    assert _RATE_8_2025.effective_to == date(2026, 6, 30)


def test_rate_8_2025_summer_rates() -> None:
    # Summer: first 800 kWh @ $0.14599, over @ $0.15983
    assert abs(_RATE_8_2025.summer_boundary_wh - 800_000) < 1e-9
    assert abs(_RATE_8_2025.summer_under_per_wh - 0.14599 / 1000) < 1e-12
    assert abs(_RATE_8_2025.summer_over_per_wh - 0.15983 / 1000) < 1e-12


def test_rate_8_2025_winter_rates() -> None:
    assert abs(_RATE_8_2025.winter_boundary_wh - 800_000) < 1e-9
    assert abs(_RATE_8_2025.winter_under_per_wh - 0.14599 / 1000) < 1e-12
    assert abs(_RATE_8_2025.winter_over_per_wh - 0.14045 / 1000) < 1e-12


def test_rate_6_2025_correct_dates() -> None:
    assert _RATE_6_2025.effective_from == date(2025, 7, 23)
    assert _RATE_6_2025.effective_to == date(2026, 6, 30)


def test_rate_6_2025_summer_rates() -> None:
    assert abs(_RATE_6_2025.summer_boundary_wh - 800_000) < 1e-9
    assert abs(_RATE_6_2025.summer_under_per_wh - 0.14164 / 1000) < 1e-12
    assert abs(_RATE_6_2025.summer_over_per_wh - 0.15505 / 1000) < 1e-12


def test_rate_6_2025_winter_rates() -> None:
    assert abs(_RATE_6_2025.winter_boundary_wh - 800_000) < 1e-9
    assert abs(_RATE_6_2025.winter_under_per_wh - 0.14164 / 1000) < 1e-12
    assert abs(_RATE_6_2025.winter_over_per_wh - 0.13628 / 1000) < 1e-12


def test_historical_registry_rate_8_refers_to_rate_8_2025() -> None:
    assert HISTORICAL_RATE_REGISTRY[COST_MODE_RATE_8] == [_RATE_8_2025]


def test_historical_registry_rate_6_refers_to_rate_6_2025() -> None:
    assert HISTORICAL_RATE_REGISTRY[COST_MODE_RATE_6] == [_RATE_6_2025]


# ---------------------------------------------------------------------------
# Phase 3: TOU and flat rate plan registry entries
# ---------------------------------------------------------------------------


def test_rate_plan_registry_contains_rate_5() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_5)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_5


def test_rate_plan_registry_contains_rate_7() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_7)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_7


def test_rate_plan_registry_contains_rate_2() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_2)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_2


def test_rate_plan_registry_has_all_five_modes() -> None:
    assert set(RATE_PLAN_REGISTRY.keys()) == {
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
    }


def test_build_cost_mode_choices_contains_new_rate_plans() -> None:
    choices = build_cost_mode_choices()
    assert COST_MODE_RATE_5 in choices
    assert COST_MODE_RATE_7 in choices
    assert COST_MODE_RATE_2 in choices


def test_build_cost_mode_choices_uses_library_name_for_new_plans() -> None:
    choices = build_cost_mode_choices()
    assert choices[COST_MODE_RATE_5] == RATE_5.name
    assert choices[COST_MODE_RATE_7] == RATE_7.name
    assert choices[COST_MODE_RATE_2] == RATE_2.name
