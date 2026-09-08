"""Tests for dominionsc rates."""
from datetime import datetime
from custom_components.dominionsc.const import COST_MODE_FIXED, COST_MODE_RATE_6, COST_MODE_RATE_8, COST_MODE_NONE
from custom_components.dominionsc.rates import (
    Season, TieredRate, SeasonalTieredRates, RateSchedule,
    SC_RATE_8, SC_RATE_6, TIERED_RATE_REGISTRY,
    get_season, calculate_tiered_cost, calculate_sc_rate_interval_cost,
    build_cost_mode_choices,
)

def test_get_season_summer() -> None:
    assert get_season(5) is Season.SUMMER
    assert get_season(9) is Season.SUMMER

def test_get_season_winter() -> None:
    assert get_season(10) is Season.WINTER
    assert get_season(4) is Season.WINTER
    assert get_season(1) is Season.WINTER

def test_calculate_tiered_cost_all_under() -> None:
    rate = TieredRate(boundary_wh=1000, rate_under=0.1, rate_over=0.2)
    assert calculate_tiered_cost(500, 0, rate) == 50.0

def test_calculate_tiered_cost_all_over() -> None:
    rate = TieredRate(boundary_wh=1000, rate_under=0.1, rate_over=0.2)
    assert calculate_tiered_cost(500, 1200, rate) == 100.0

def test_calculate_tiered_cost_straddles() -> None:
    rate = TieredRate(boundary_wh=1000, rate_under=0.1, rate_over=0.2)
    assert calculate_tiered_cost(500, 800, rate) == (200*0.1 + 300*0.2)

def test_calculate_sc_rate_interval_cost_zero() -> None:
    assert calculate_sc_rate_interval_cost(0, datetime(2025,7,1), 0, SC_RATE_8) == 0.0

def test_calculate_sc_rate_interval_cost_summer() -> None:
    # July = summer tier
    assert calculate_sc_rate_interval_cost(100, datetime(2025,7,15), 0, SC_RATE_8) > 0

def test_calculate_sc_rate_interval_cost_winter() -> None:
    # January = winter tier
    assert calculate_sc_rate_interval_cost(100, datetime(2025,1,15), 0, SC_RATE_8) > 0

def test_build_cost_mode_choices() -> None:
    choices = build_cost_mode_choices()
    assert COST_MODE_NONE in choices
    assert COST_MODE_FIXED in choices
    assert COST_MODE_RATE_8 in choices

