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

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dominionsc import (
    Commodity,
    DemandCharge,
    FlatUsageCharge,
    RatePlan,
    Season,
    TieredUsageCharge,
    TimeOfUseCharge,
)

_LOGGER = logging.getLogger(__name__)

_UTILITY_TZ = ZoneInfo("America/New_York")

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    CONF_GAS_COST_MODE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
)
from .rates import (
    GAS_RATE_PLAN_REGISTRY,
    HISTORICAL_RATE_REGISTRY,
    RATE_PLAN_REGISTRY,
    _HistoricalTieredRate,
)

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


def _calculate_historical_tiered_cost(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_wh_before: float,
    hist: _HistoricalTieredRate,
) -> float:
    """
    Calculate cost for a single Wh interval using a superseded tariff period.

    Used for intervals that fall between ``hist.effective_from`` and
    ``hist.effective_to`` — i.e. periods where the library's current
    ``RatePlan`` was not yet in effect and the old rates apply.

    Args:
        interval_wh:          Wh consumed in this interval.
        interval_dt:          Start timestamp; its month determines season.
        cumulative_wh_before: Wh consumed before this interval in the cycle.
        hist:                 The :class:`~.rates._HistoricalTieredRate` whose
                              ``effective_from``/``effective_to`` bracket this
                              interval's date.

    Returns:
        Cost in dollars.

    """
    is_summer = 5 <= interval_dt.month <= 9
    if is_summer:
        boundary = hist.summer_boundary_wh
        rate_under = hist.summer_under_per_wh
        rate_over = hist.summer_over_per_wh
    else:
        boundary = hist.winter_boundary_wh
        rate_under = hist.winter_under_per_wh
        rate_over = hist.winter_over_per_wh

    cumulative_after = cumulative_wh_before + interval_wh

    if cumulative_after <= boundary:
        return interval_wh * rate_under
    if cumulative_wh_before >= boundary:
        return interval_wh * rate_over

    wh_under = boundary - cumulative_wh_before
    wh_over = interval_wh - wh_under
    return wh_under * rate_under + wh_over * rate_over


def _rate_plan_is_tiered(rate_plan: RatePlan) -> bool:
    """Return True if *rate_plan* contains a ``TieredUsageCharge``."""
    return any(isinstance(c, TieredUsageCharge) for c in rate_plan.charges)


def _rate_plan_is_tou(rate_plan: RatePlan) -> bool:
    """Return True if *rate_plan* contains a ``TimeOfUseCharge``."""
    return any(isinstance(c, TimeOfUseCharge) for c in rate_plan.charges)


def _rate_plan_has_demand(rate_plan: RatePlan) -> bool:
    """Return True if *rate_plan* contains a ``DemandCharge``."""
    return any(isinstance(c, DemandCharge) for c in rate_plan.charges)


def _rate_plan_is_flat(rate_plan: RatePlan) -> bool:
    """Return True if *rate_plan* contains a ``FlatUsageCharge`` (Rate 2 or gas plans)."""
    return any(isinstance(c, FlatUsageCharge) for c in rate_plan.charges)


def _calculate_flat_cost(
    interval_usage: float,
    rate_plan: RatePlan,
) -> float:
    """
    Calculate cost for a single interval under a ``FlatUsageCharge``.

    Handles both gas rates (Rate 32S, Rate 32V) and flat electric rates (Rate 2).
    The ``interval_usage`` argument is interpreted differently by commodity:

    - **Gas** (``Commodity.GAS``): ``interval_usage`` is in **ft³**. Converted to
      therms by dividing by 100 before multiplying by the $/therm charge.
    - **Electric** (``Commodity.ELECTRICITY``): ``interval_usage`` is in **Wh**.
      Converted to kWh by dividing by 1000 before multiplying by the $/kWh charge.

    Args:
        interval_usage: Wh consumed (electric) or ft³ consumed (gas) in this interval.
        rate_plan:      Library :class:`~dominionsc.RatePlan` containing the
                        ``FlatUsageCharge`` to apply.

    Returns:
        Cost in dollars, or 0.0 if no ``FlatUsageCharge`` is found.

    """
    for charge in rate_plan.charges:
        if not isinstance(charge, FlatUsageCharge):
            continue
        if rate_plan.commodity == Commodity.GAS:
            # interval_usage is in ft³; 1 therm = 100 ft³
            therms = interval_usage / 100.0
            return therms * float(charge.price_per_unit)
        else:
            # Electric flat rate (Rate 2): interval_usage is in Wh
            kwh = interval_usage / 1000.0
            return kwh * float(charge.price_per_unit)
    return 0.0


def _resolve_gas_cost_config(
    options: dict[str, Any],
) -> tuple[str, RatePlan | None]:
    """
    Unpack gas cost configuration from an options dict with safe defaults.

    Analogous to :func:`_resolve_cost_config` but for gas accounts.

    Args:
        options: The config entry's ``options`` dict (``entry.options``).

    Returns:
        A 2-tuple:
        - ``gas_cost_mode`` (str): one of ``COST_MODE_NONE``, ``COST_MODE_RATE_32S``,
          or ``COST_MODE_RATE_32V``. Defaults to ``COST_MODE_NONE`` when no mode
          is stored (no gas cost calculation by default).
        - ``gas_rate_plan`` (:class:`dominionsc.RatePlan` | ``None``):
          the library rate plan for the selected gas mode, or ``None`` for
          ``COST_MODE_NONE`` or an unrecognised key.

    """
    gas_cost_mode = options.get(CONF_GAS_COST_MODE, COST_MODE_NONE)
    gas_rate_plan: RatePlan | None = GAS_RATE_PLAN_REGISTRY.get(gas_cost_mode)
    return gas_cost_mode, gas_rate_plan


def _calculate_tou_cost(
    interval_wh: float,
    interval_dt: datetime,
    rate_plan: RatePlan,
) -> float:
    """
    Calculate cost for a single Wh interval under a ``TimeOfUseCharge``.

    Converts the interval timestamp to America/New_York local time, then
    matches the local clock time against the season's TOU period windows.
    The first matching non-fallback window wins; if no window matches the
    fallback period's price is used.

    Demand charges (``DemandCharge``) on Rate 7 cannot be computed from
    per-interval data and are silently skipped. A DEBUG log is emitted once
    per call so that this omission is visible in trace-level logs.

    Args:
        interval_wh:  Wh consumed in this interval.
        interval_dt:  Start timestamp, must be timezone-aware for correct
                      local-time conversion. Naive datetimes fall back to
                      the system timezone.
        rate_plan:    Library :class:`~dominionsc.RatePlan` containing the
                      ``TimeOfUseCharge`` to apply.

    Returns:
        Cost in dollars, or 0.0 if no ``TimeOfUseCharge`` is found.

    """
    if _rate_plan_has_demand(rate_plan):
        _LOGGER.debug(
            "Rate plan '%s' includes a demand charge that cannot be "
            "calculated from interval data — skipping demand component.",
            rate_plan.code,
        )

    for charge in rate_plan.charges:
        if not isinstance(charge, TimeOfUseCharge):
            continue

        season = Season.SUMMER if 5 <= interval_dt.month <= 9 else Season.WINTER
        periods = charge.periods_by_season[season]

        local_dt = interval_dt.astimezone(_UTILITY_TZ)
        local_time = local_dt.time().replace(second=0, microsecond=0)

        fallback_price = None
        for period in periods:
            if period.fallback:
                fallback_price = period.price_per_unit
                continue
            for window in period.windows:
                if window.start <= window.end:
                    # Normal (non-crossing) window: [start, end)
                    matched = window.start <= local_time < window.end
                else:
                    # Midnight-crossing window: [start, 24:00) ∪ [00:00, end)
                    matched = local_time >= window.start or local_time < window.end
                if matched:
                    return interval_wh * float(period.price_per_unit) / 1000

        if fallback_price is not None:
            return interval_wh * float(fallback_price) / 1000

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
        3. Known rate plan    → checks ``HISTORICAL_RATE_REGISTRY`` first; if
           the interval falls within a superseded tariff period, delegates to
           :func:`_calculate_historical_tiered_cost`. Otherwise, if the interval
           is on or after ``rate_plan.effective_from``, dispatches to
           :func:`_calculate_tiered_cost` (tiered plans),
           :func:`_calculate_tou_cost` (TOU plans), or
           :func:`_calculate_flat_cost` (flat plans — Rate 2 electric, Rate 32S/32V
           gas). For gas plans ``interval_wh`` carries ft³, not Wh; the unit
           conversion is handled inside :func:`_calculate_flat_cost`.
           Intervals before all known rate periods return 0.0.
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
        interval_date = interval_dt.date()

        # Check historical rate periods first. For each superseded tariff entry
        # that brackets this interval's date, delegate to the historical
        # calculation. The list is ordered ascending by effective_from so the
        # first match wins (no overlap between historical periods).
        for hist in HISTORICAL_RATE_REGISTRY.get(cost_mode, []):
            if hist.effective_from <= interval_date <= hist.effective_to:
                return _calculate_historical_tiered_cost(
                    interval_wh, interval_dt, cumulative_wh_before, hist
                )

        # No historical period matched — use the current library plan if the
        # interval is on or after its effective date.
        if interval_date >= rate_plan.effective_from:
            if _rate_plan_is_tiered(rate_plan):
                return _calculate_tiered_cost(
                    interval_wh,
                    interval_dt,
                    cumulative_wh_before,
                    rate_plan,
                )
            if _rate_plan_is_tou(rate_plan):
                return _calculate_tou_cost(interval_wh, interval_dt, rate_plan)
            if _rate_plan_is_flat(rate_plan):
                return _calculate_flat_cost(interval_wh, rate_plan)

        # Interval predates all known rate periods → no cost.
        return 0.0

    # Unknown mode or None plan — produce no cost rather than raising.
    return 0.0
