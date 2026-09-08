"""Chunk 1 tests for coordinator: _build_statistic_ids + _resolve_cost_config."""

from string import Template

from custom_components.dominionsc.const import (
    COST_MODE_FIXED,
    COST_MODE_RATE_8,
    DOMAIN,
)
from custom_components.dominionsc.coordinator import (
    _build_statistic_ids,
    _resolve_cost_config,
)
from custom_components.dominionsc.rates import SC_RATE_8


def test_build_statistic_ids_electric() -> None:
    cid, cost_id, prefix = _build_statistic_ids("123 Oak St", "ELECTRIC")
    assert cid == f"{DOMAIN}:123_oak_st_electric_energy_consumption"
    assert cost_id == f"{DOMAIN}:123_oak_st_electric_energy_cost"
    assert isinstance(prefix, Template)
    assert "Electric" in prefix.template


def test_build_statistic_ids_gas() -> None:
    cid, cost_id, _prefix = _build_statistic_ids("456-789", "GAS")
    assert cid == f"{DOMAIN}:456_789_gas_energy_consumption"
    assert cost_id is None


def test_resolve_cost_config_default() -> None:
    mode, _rate, sched = _resolve_cost_config({})
    assert mode == COST_MODE_RATE_8
    assert sched is SC_RATE_8


def test_resolve_cost_config_fixed() -> None:
    mode, rate, sched = _resolve_cost_config(
        {"cost_mode": COST_MODE_FIXED, "fixed_rate": 0.15}
    )
    assert mode == COST_MODE_FIXED
    assert rate == 0.15
    assert sched is None
