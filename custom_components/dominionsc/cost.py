"""
Pure cost-calculation helpers.

These functions compute per-interval cost from usage data and integration
options. They carry no Home Assistant dependency and are separated from the
coordinator so they can be reasoned about and tested in isolation.

Call chain
----------
:func:`_resolve_cost_config` is called once per statistics operation to
unpack the coordinator's options dict into typed values, avoiding repeated
``dict.get()`` calls with defaults scattered across the codebase.

:func:`_calculate_cost_for_wh` is then called once per usage interval inside
:func:`~.aggregation.aggregate_hourly_data` to compute the cost for that
single interval.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _calculate_cost_for_wh`` (etc.)
continue to resolve. See docs/REFACTOR_PLAN.md Phase 2.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from dominionsc import RatePlan, Season, TieredUsageCharge

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
)
from .rates import RATE_PLAN_REGISTRY

if TYPE_CHECKING:
    pass


def _resolve_cost_config(
    options: dict[str, Any],
) -> tuple[str, float, RatePlan | None]:
    """
    Unpack cost configuration from an options dict with safe defaults.

    Centralises the options-→-typed-values conversion so every part of the
    codebase that needs cost config gets identical defaults.

    Args:
        options: The config entry's ``options`` dict (``entry.options``).

    Returns:
        A 3-tuple:
        - ``cost_mode`` (str): one of the ``COST_MODE_*`` constants. Defaults
          to ``COST_MODE_RATE_8`` when no mode is stored (first-run behaviour
          before the user has gone through the options flow).
        - ``fixed_rate`` (float): the user-supplied $/kWh rate. Defaults to
          ``DEFAULT_FIXED_RATE``. Only relevant when ``cost_mode`` is
          ``COST_MODE_FIXED``.
        - ``rate_plan`` (:class:`dominionsc.RatePlan` | ``None``):
          the library rate plan for tiered/flat modes, or ``None`` for
          ``COST_MODE_NONE`` and ``COST_MODE_FIXED``. When ``None`` is returned
          for an unknown tiered mode key, ``_calculate_cost_for_wh`` will return
          0.0 for all intervals (safe fallback).

    """
    cost_mode = options.get(CONF_COST_MODE, COST_MODE_RATE_8)
    fixed_rate: float = options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE)
    rate_plan: RatePlan | None = RATE_PLAN_REGISTRY.get(cost_mode)
    return cost_mode, fixed_rate, rate_plan


def _calculate_tiered_cost(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    rate_plan: RatePlan,
) -> float:
    """
    Calculate cost for a single Wh interval under a ``TieredUsageCharge``.

    Walks the rate plan's charges to find the ``TieredUsageCharge``, selects
    the correct season's tiers, and handles the case where cumulative usage
    straddles the tier boundary within this single interval.

    Args:
        interval_wh:       Wh consumed in this interval.
        interval_dt:       Start timestamp of the interval; its month determines
                           summer (May–Sep) vs. winter (Oct–Apr) season.
        cumulative_wh_before: Total Wh consumed before this interval in the
                           current billing cycle. Used for tier-boundary splits.
        rate_plan:         Library :class:`~dominionsc.RatePlan` containing the
                           ``TieredUsageCharge`` to apply.

    Returns:
        Cost in dollars, or 0.0 if no ``TieredUsageCharge`` is found.

    """
    for charge in rate_plan.charges:
        if not isinstance(charge, TieredUsageCharge):
            continue

        season = Season.SUMMER if 5 <= interval_dt.month <= 9 else Season.WINTER
        tiers = charge.tiers_by_season[season]
        # tiers[0] is the lower tier (upper_bound is the boundary in kWh);
        # tiers[1] is the upper tier (upper_bound is None = unbounded).
        boundary_wh = float(tiers[0].upper_bound) * 1000  # kWh → Wh
        rate_under = float(tiers[0].price_per_unit) / 1000  # $/kWh → $/Wh
        rate_over = float(tiers[1].price_per_unit) / 1000

        cumulative_after = cumulative_wh_before + interval_wh

        if cumulative_after <= boundary_wh:
            return interval_wh * rate_under
        if cumulative_wh_before >= boundary_wh:
            return interval_wh * rate_over

        # Interval straddles the boundary — split into two portions.
        wh_under = boundary_wh - cumulative_wh_before
        wh_over = interval_wh - wh_under
        return wh_under * rate_under + wh_over * rate_over

    return 0.0


def _calculate_cost_for_wh(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    cost_mode: str,
    fixed_rate: float,
    rate_plan: RatePlan | None,
) -> float:
    """
    Calculate the cost for a single Wh interval under the given cost mode.

    This is the innermost cost calculation function. It is called once for
    every usage interval (typically 15 minutes) that is being inserted into
    statistics. The caller is responsible for maintaining the
    ``cumulative_wh_before`` counter, which is reset to 0 at each billing
    cycle boundary for tiered-rate modes.

    Decision tree:
        1. ``COST_MODE_NONE``  → always returns 0.0.
        2. ``COST_MODE_FIXED`` → flat multiplication: Wh × (rate_$/kWh / 1000).
        3. Known rate plan    → delegates to :func:`_calculate_tiered_cost`.
           Intervals before ``rate_plan.effective_from`` return 0.0 to avoid
           applying a current tariff to historical data.
        4. Unknown mode (``rate_plan`` is ``None``) → 0.0 (safe fallback).

    Args:
        interval_wh:          Wh consumed in this single usage interval.
        interval_dt:          Start timestamp of the interval, used to:
                              (a) determine summer vs. winter season for tiered
                              rates, and (b) compare against the rate plan's
                              effective date.
        cumulative_wh_before: Total Wh consumed **before** this interval in
                              the current billing cycle. Required for correct
                              tier-boundary calculations. Pass 0.0 for
                              fixed-rate or no-cost modes (it is ignored).
        cost_mode:            One of the ``COST_MODE_*`` string constants from
                              :mod:`.const`.
        fixed_rate:           User-supplied rate in $/kWh. Only used when
                              ``cost_mode == COST_MODE_FIXED``.
        rate_plan:            :class:`~dominionsc.RatePlan` for tiered modes,
                              or ``None`` for fixed/none modes. Passing ``None``
                              for a tiered mode produces 0.0 cost.

    Returns:
        Cost in dollars for this interval. Always >= 0.0.

    """
    if cost_mode == COST_MODE_NONE:
        return 0.0

    if cost_mode == COST_MODE_FIXED:
        # Convert: Wh × ($/kWh ÷ 1000 Wh/kWh) = $
        return interval_wh * (fixed_rate / 1000)

    if rate_plan is not None:
        # Skip intervals that predate the rate plan's effective date so
        # extended backfills don't retroactively apply a rate that wasn't in
        # effect at the time (which would produce inaccurate cost estimates).
        if interval_dt.date() < rate_plan.effective_from:
            return 0.0
        return _calculate_tiered_cost(
            interval_wh,
            interval_dt,
            cumulative_wh_before,
            rate_plan,
        )

    # Unknown mode or None plan — produce no cost rather than raising.
    return 0.0
