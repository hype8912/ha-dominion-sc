"""
Pure cost-calculation helpers.

These functions compute per-interval cost from usage data and integration
options. They carry no Home Assistant dependency and are separated from the
coordinator so they can be reasoned about and tested in isolation.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _calculate_cost_for_wh`` (etc.)
continue to resolve. See docs/REFACTOR_PLAN.md Phase 2.
"""

from datetime import datetime
from typing import Any

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
)
from .rates import TIERED_RATE_REGISTRY, RateSchedule, calculate_sc_rate_interval_cost


def _resolve_cost_config(
    options: dict[str, Any],
) -> tuple[str, float, RateSchedule | None]:
    """Return (cost_mode, fixed_rate, rate_schedule) with consistent defaults."""
    cost_mode = options.get(CONF_COST_MODE, COST_MODE_RATE_8)
    fixed_rate: float = options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE)
    rate_schedule: RateSchedule | None = TIERED_RATE_REGISTRY.get(cost_mode)
    return cost_mode, fixed_rate, rate_schedule


def _calculate_cost_for_wh(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    cost_mode: str,
    fixed_rate: float,
    rate_schedule: RateSchedule | None,
) -> float:
    """
    Calculate the cost for a single Wh interval under the given cost mode.

    Args:
        interval_wh:          Wh consumed in this interval.
        interval_dt:          Timestamp of the interval (used for season).
        cumulative_wh_before: Total Wh consumed before this interval in the
                              billing period (used for tier boundary tracking).
        cost_mode:            One of the COST_MODE_* constants.
        fixed_rate:           $/kWh fixed rate (only used when cost_mode is FIXED).
        rate_schedule:        RateSchedule instance (only used for tiered modes).
                              Pass ``None`` to produce 0.0 cost (e.g. outside the
                              billing period for tiered rates).

    Returns:
        Cost in dollars for this interval.

    """
    if cost_mode == COST_MODE_NONE:
        return 0.0
    if cost_mode == COST_MODE_FIXED:
        return interval_wh * (fixed_rate / 1000)
    if rate_schedule is not None:
        # Skip intervals that predate the rate schedule's effective date
        # so extended backfills don't apply current rates to old data.
        if interval_dt.date() < rate_schedule.effective_date:
            return 0.0
        return calculate_sc_rate_interval_cost(
            interval_wh,
            interval_dt,
            cumulative_wh_before,
            rate_schedule,
        )
    return 0.0
