"""Tests for dominionsc rates adapter."""

from dominionsc import RATE_6, RATE_8, RatePlan

from custom_components.dominionsc.const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_6,
    COST_MODE_RATE_8,
)
from custom_components.dominionsc.rates import RATE_PLAN_REGISTRY, build_cost_mode_choices


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
    assert set(RATE_PLAN_REGISTRY.keys()) == {COST_MODE_RATE_8, COST_MODE_RATE_6}


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
