"""Tests for dominionsc rates adapter."""

from datetime import datetime

import pytest
from dominionsc import (
    RATE_1,
    RATE_2,
    RATE_5,
    RATE_6,
    RATE_7,
    RATE_8,
    RATE_32S,
    RATE_32V,
    RESIDENTIAL_ELECTRIC_RATE_PLANS,
    RESIDENTIAL_GAS_RATE_PLANS,
    RatePlan,
)

from custom_components.dominionsc.const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_1,
    COST_MODE_RATE_2,
    COST_MODE_RATE_5,
    COST_MODE_RATE_6,
    COST_MODE_RATE_7,
    COST_MODE_RATE_8,
    COST_MODE_RATE_32S,
    COST_MODE_RATE_32V,
)
from custom_components.dominionsc.cost import _calculate_cost_for_wh
from custom_components.dominionsc.rates import (
    GAS_RATE_PLAN_REGISTRY,
    RATE_PLAN_REGISTRY,
    build_cost_mode_choices,
    build_gas_cost_mode_choices,
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


def test_rate_plan_registry_contains_rate_1() -> None:
    plan = RATE_PLAN_REGISTRY.get(COST_MODE_RATE_1)
    assert plan is not None
    assert plan is RATE_1


def test_rate_plan_registry_only_has_supported_modes() -> None:
    assert set(RATE_PLAN_REGISTRY.keys()) == {
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
        COST_MODE_RATE_1,
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
    assert "Rate 8" in choices[COST_MODE_RATE_8]
    assert "Rate 6" in choices[COST_MODE_RATE_6]


def test_build_cost_mode_choices_labels_come_from_library_plan_names() -> None:
    """Labels are the library plan names, plus HA notes for Rate 7 and Rate 1."""
    choices = build_cost_mode_choices()
    assert choices[COST_MODE_NONE] == "None (no cost calculation)"
    assert choices[COST_MODE_RATE_8] == RATE_8.name
    assert choices[COST_MODE_RATE_6] == RATE_6.name
    assert choices[COST_MODE_RATE_5] == RATE_5.name
    assert choices[COST_MODE_RATE_2] == RATE_2.name
    assert choices[COST_MODE_RATE_7] == f"{RATE_7.name} (energy cost only)"
    assert choices[COST_MODE_RATE_1] == f"{RATE_1.name} (closed to new customers)"
    assert choices[COST_MODE_FIXED] == "Fixed Rate (custom $/kWh)"


def test_build_cost_mode_choices_plan_order() -> None:
    assert list(build_cost_mode_choices()) == [
        COST_MODE_NONE,
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
        COST_MODE_RATE_1,
        COST_MODE_FIXED,
    ]


def test_registries_mirror_library_catalogs() -> None:
    assert dict(RESIDENTIAL_ELECTRIC_RATE_PLANS) == RATE_PLAN_REGISTRY
    assert dict(RESIDENTIAL_GAS_RATE_PLANS) == GAS_RATE_PLAN_REGISTRY


# ---------------------------------------------------------------------------
# Superseded tariff periods (data lives in the dominion-sc-power library)
# ---------------------------------------------------------------------------

# (cost mode, current plan, summer under/over, winter under/over) in $/kWh for
# the 2025-07-23..2026-06-30 tariff period.
_PRIOR_PERIOD_RATES = [
    (COST_MODE_RATE_8, RATE_8, 0.14599, 0.15983, 0.14599, 0.14045),
    (COST_MODE_RATE_6, RATE_6, 0.14164, 0.15505, 0.14164, 0.13628),
]


@pytest.mark.parametrize(("mode", "plan", "s_under", "s_over", "w_under", "w_over"), _PRIOR_PERIOD_RATES)
def test_cost_engine_prices_prior_period_from_library_history(
    mode: str, plan: RatePlan, s_under: float, s_over: float, w_under: float, w_over: float
) -> None:
    """Intervals in the superseded period are priced with the library's archived plan."""
    boundary_wh = 800_000
    summer = datetime(2025, 8, 15)
    winter = datetime(2026, 1, 15)

    assert _calculate_cost_for_wh(100, summer, 0, mode, 0, plan) == pytest.approx(100 * s_under / 1000)
    assert _calculate_cost_for_wh(100, summer, boundary_wh, mode, 0, plan) == pytest.approx(100 * s_over / 1000)
    assert _calculate_cost_for_wh(100, winter, 0, mode, 0, plan) == pytest.approx(100 * w_under / 1000)
    assert _calculate_cost_for_wh(100, winter, boundary_wh, mode, 0, plan) == pytest.approx(100 * w_over / 1000)
    # An interval straddling the boundary is split across both tiers.
    straddle = _calculate_cost_for_wh(100, summer, boundary_wh - 40, mode, 0, plan)
    assert straddle == pytest.approx((40 * s_under + 60 * s_over) / 1000)


@pytest.mark.parametrize(("mode", "plan"), [(COST_MODE_RATE_8, RATE_8), (COST_MODE_RATE_6, RATE_6)])
def test_cost_engine_period_boundaries(mode: str, plan: RatePlan) -> None:
    """Cost is zero before the earliest period and switches to current rates on 2026-07-01."""
    assert _calculate_cost_for_wh(100, datetime(2025, 7, 22), 0, mode, 0, plan) == 0.0
    assert _calculate_cost_for_wh(100, datetime(2025, 7, 23), 0, mode, 0, plan) > 0.0
    prior = _calculate_cost_for_wh(100, datetime(2026, 6, 30), 0, mode, 0, plan)
    current = _calculate_cost_for_wh(100, datetime(2026, 7, 1), 0, mode, 0, plan)
    assert prior != current


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


def test_rate_plan_registry_has_all_six_modes() -> None:
    assert set(RATE_PLAN_REGISTRY.keys()) == {
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
        COST_MODE_RATE_1,
    }


def test_build_cost_mode_choices_contains_new_rate_plans() -> None:
    choices = build_cost_mode_choices()
    assert COST_MODE_RATE_5 in choices
    assert COST_MODE_RATE_7 in choices
    assert COST_MODE_RATE_2 in choices


def test_build_cost_mode_choices_uses_library_name_for_new_plans() -> None:
    choices = build_cost_mode_choices()
    assert "Rate 5" in choices[COST_MODE_RATE_5]
    assert "Rate 7" in choices[COST_MODE_RATE_7]
    assert "Rate 2" in choices[COST_MODE_RATE_2]


# ---------------------------------------------------------------------------
# Phase 4: Gas rate plan registry and choices
# ---------------------------------------------------------------------------


def test_gas_rate_plan_registry_contains_rate_32s() -> None:
    plan = GAS_RATE_PLAN_REGISTRY.get(COST_MODE_RATE_32S)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_32S


def test_gas_rate_plan_registry_contains_rate_32v() -> None:
    plan = GAS_RATE_PLAN_REGISTRY.get(COST_MODE_RATE_32V)
    assert plan is not None
    assert isinstance(plan, RatePlan)
    assert plan is RATE_32V


def test_gas_rate_plan_registry_has_exactly_two_modes() -> None:
    assert set(GAS_RATE_PLAN_REGISTRY.keys()) == {
        COST_MODE_RATE_32S,
        COST_MODE_RATE_32V,
    }


def test_build_gas_cost_mode_choices_contains_expected_keys() -> None:
    choices = build_gas_cost_mode_choices()
    assert COST_MODE_NONE in choices
    assert COST_MODE_RATE_32S in choices
    assert COST_MODE_RATE_32V in choices


def test_build_gas_cost_mode_choices_none_is_first() -> None:
    choices = build_gas_cost_mode_choices()
    assert next(iter(choices)) == COST_MODE_NONE


def test_build_gas_cost_mode_choices_uses_library_plan_names() -> None:
    choices = build_gas_cost_mode_choices()
    assert "Rate 32S" in choices[COST_MODE_RATE_32S]
    assert "Rate 32V" in choices[COST_MODE_RATE_32V]


def test_build_gas_cost_mode_choices_excludes_electric_modes() -> None:
    """Gas choices must have EXACTLY 3 keys: none, rate_32s, rate_32v."""
    choices = build_gas_cost_mode_choices()
    assert set(choices.keys()) == {
        COST_MODE_NONE,
        COST_MODE_RATE_32S,
        COST_MODE_RATE_32V,
    }
    # Explicitly confirm electric-only modes are absent
    assert COST_MODE_RATE_8 not in choices
    assert COST_MODE_RATE_5 not in choices
    assert COST_MODE_FIXED not in choices


# ---------------------------------------------------------------------------
# Phase 6: build_cost_mode_choices explicit labels
# ---------------------------------------------------------------------------


def test_build_cost_mode_choices_has_eight_options() -> None:
    choices = build_cost_mode_choices()
    assert len(choices) == 8


def test_build_cost_mode_choices_rate7_has_energy_cost_only_note() -> None:
    choices = build_cost_mode_choices()
    assert "energy cost only" in choices[COST_MODE_RATE_7]


def test_build_cost_mode_choices_fixed_rate_label() -> None:
    choices = build_cost_mode_choices()
    assert "Fixed Rate" in choices[COST_MODE_FIXED]


def test_build_gas_cost_mode_choices_has_three_options() -> None:
    choices = build_gas_cost_mode_choices()
    assert len(choices) == 3


def test_build_gas_cost_mode_choices_exact_labels() -> None:
    """Gas labels are the library plan names."""
    choices = build_gas_cost_mode_choices()
    assert choices[COST_MODE_NONE] == "None (no cost calculation)"
    assert choices[COST_MODE_RATE_32S] == RATE_32S.name
    assert choices[COST_MODE_RATE_32V] == RATE_32V.name


def test_build_gas_cost_mode_choices_order() -> None:
    """Gas choices must appear in order: none, rate_32s, rate_32v."""
    choices = build_gas_cost_mode_choices()
    keys = list(choices)
    assert keys == [COST_MODE_NONE, COST_MODE_RATE_32S, COST_MODE_RATE_32V]


def test_build_cost_mode_choices_full_ordering() -> None:
    """Electric choices must appear in defined UI order: none, rate_8, rate_6, rate_5, rate_7, rate_2, rate_1, fixed."""
    choices = build_cost_mode_choices()
    keys = list(choices)
    assert keys == [
        COST_MODE_NONE,
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
        COST_MODE_RATE_1,
        COST_MODE_FIXED,
    ]
