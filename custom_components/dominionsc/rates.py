"""
Rate schedule definitions and cost calculation for Dominion Energy SC.

This module is a **pure computation layer** — it has no dependency on Home
Assistant or the coordinator. It can be imported and tested in isolation.

Concepts
--------
**Tiered pricing**
    Dominion's residential rates use two tiers within each billing cycle.
    The first 800 kWh of consumption is billed at a lower rate; everything
    above 800 kWh is billed at a higher rate (summer) or lower rate (winter).
    Because the API delivers usage in ~15-minute intervals, a single interval
    may straddle the 800 kWh boundary. :func:`calculate_tiered_cost` handles
    this by splitting the interval into a "below-boundary" portion and an
    "above-boundary" portion and pricing each separately.

**Seasonal rates**
    Both Rate 8 and Rate 6 have different upper-tier rates for summer
    (May-September) and winter (October-April). The lower-tier rate is the
    same year-round for both schedules.

**Effective date**
    Each :class:`RateSchedule` carries an ``effective_date``. When calculating
    costs for historical data (extended backfill), intervals that predate the
    effective date are priced at $0 rather than applying a rate that was not
    yet in effect. See :func:`~.cost._calculate_cost_for_wh`.

Adding a new rate schedule
--------------------------
1. Define a new :class:`RateSchedule` constant (e.g. ``SC_RATE_9``).
2. Add it to :data:`TIERED_RATE_REGISTRY` alongside its ``COST_MODE_*`` key.
3. Add the matching ``COST_MODE_*`` string constant to :mod:`.const`.
No other files need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .const import COST_MODE_FIXED, COST_MODE_NONE, COST_MODE_RATE_6, COST_MODE_RATE_8


class Season(Enum):
    """
    Billing season used to select the correct tiered rate.

    Dominion Energy SC uses two seasons:

    - **SUMMER**: May through September (months 5-9). The upper tier is
      charged at a *higher* rate than winter to reflect peak cooling demand.
    - **WINTER**: October through April (months 10-12, 1-4). The upper tier
      is charged at a *lower* rate than summer.
    """

    SUMMER = "summer"  # May-September (months 5-9)
    WINTER = "winter"  # October-April (months 10-12, 1-4)


@dataclass(frozen=True)
class TieredRate:
    """
    A two-tier rate structure defined by a Wh usage boundary.

    All monetary values are in **$/Wh** (not $/kWh) so they can be multiplied
    directly against Wh consumption values. The published tariff rates in $/kWh
    are divided by 1000 when constructing these instances.

    Attributes:
        boundary_wh:  The cumulative usage threshold in Wh. Usage at or below
                      this value in a billing cycle is billed at ``rate_under``;
                      usage above is billed at ``rate_over``.
                      Example: ``800 * 1000`` = 800 kWh.
        rate_under:   Rate in $/Wh applied to usage up to ``boundary_wh``.
        rate_over:    Rate in $/Wh applied to usage exceeding ``boundary_wh``.

    """

    boundary_wh: float
    rate_under: float  # $/Wh for cumulative usage <= boundary_wh
    rate_over: float  # $/Wh for cumulative usage > boundary_wh


@dataclass(frozen=True)
class SeasonalTieredRates:
    """
    Paired summer and winter :class:`TieredRate` instances for one tariff.

    Attributes:
        summer: Tiered rate structure to use for May-September billing months.
        winter: Tiered rate structure to use for October-April billing months.

    """

    summer: TieredRate
    winter: TieredRate


@dataclass(frozen=True)
class RateSchedule:
    """
    Complete rate schedule for a single Dominion Energy SC tariff.

    Attributes:
        name:                   Human-readable tariff name shown in the UI.
        effective_date:         Date from which this rate schedule applies.
                                Intervals before this date are priced at $0
                                during extended backfill operations.
        basic_facilities_charge: Monthly fixed customer charge in USD. This is
                                **not** used in per-interval cost calculations
                                because the integration tracks usage-based costs
                                only. It is stored here for reference.
        rates:                  Seasonal tiered rate pairs (summer + winter).

    """

    name: str
    effective_date: date
    basic_facilities_charge: float  # Monthly fixed charge in USD (informational)
    rates: SeasonalTieredRates


# ---------------------------------------------------------------------------
# Rate schedule definitions
# ---------------------------------------------------------------------------

# Dominion Energy South Carolina Rate 8 - Residential Service
# Effective for Bills Rendered On and After July 23, 2025
# Source: Dominion Energy SC filed tariff, SC PSC Docket No. 2025-xxx
#
# Rate structure:
#   Basic Facilities Charge: $9.00/month
#   Energy Charge (Summer, May-Sept): first 800 kWh @ $0.14599/kWh,
#                                     over 800 kWh  @ $0.15983/kWh
#   Energy Charge (Winter, Oct-Apr):  first 800 kWh @ $0.14599/kWh,
#                                     over 800 kWh  @ $0.14045/kWh
SC_RATE_8 = RateSchedule(
    name="Rate 8 - Residential Service",
    effective_date=date(2025, 7, 23),
    basic_facilities_charge=9.00,
    rates=SeasonalTieredRates(
        summer=TieredRate(
            boundary_wh=800 * 1000,  # 800 kWh expressed in Wh
            rate_under=0.14599 / 1000,  # $0.14599/kWh → $/Wh
            rate_over=0.15983 / 1000,  # $0.15983/kWh → $/Wh
        ),
        winter=TieredRate(
            boundary_wh=800 * 1000,
            rate_under=0.14599 / 1000,
            rate_over=0.14045 / 1000,  # Winter upper tier is cheaper than summer
        ),
    ),
)


# Dominion Energy South Carolina Rate 6 - Energy Saver / Conservation Rate
# Effective for Bills Rendered On and After July 23, 2025
# Source: Dominion Energy SC filed tariff, SC PSC Docket No. 2025-xxx
#
# Rate structure:
#   Basic Facilities Charge: $9.00/month
#   Energy Charge (Summer, May-Sept): first 800 kWh @ $0.14164/kWh,
#                                     over 800 kWh  @ $0.15505/kWh
#   Energy Charge (Winter, Oct-Apr):  first 800 kWh @ $0.14164/kWh,
#                                     over 800 kWh  @ $0.13628/kWh
SC_RATE_6 = RateSchedule(
    name="Rate 6 - Energy Saver / Conservation Rate",
    effective_date=date(2025, 7, 23),
    basic_facilities_charge=9.00,
    rates=SeasonalTieredRates(
        summer=TieredRate(
            boundary_wh=800 * 1000,
            rate_under=0.14164 / 1000,  # $0.14164/kWh → $/Wh
            rate_over=0.15505 / 1000,
        ),
        winter=TieredRate(
            boundary_wh=800 * 1000,
            rate_under=0.14164 / 1000,
            rate_over=0.13628 / 1000,
        ),
    ),
)

# ---------------------------------------------------------------------------
# Rate registry
# ---------------------------------------------------------------------------
# Maps each COST_MODE_* key (from const.py) to its RateSchedule.
#
# To add a new tiered rate schedule:
#   1. Define a new RateSchedule constant above (e.g. SC_RATE_9).
#   2. Add a new COST_MODE_* constant to const.py (e.g. COST_MODE_RATE_9 = "rate_9").
#   3. Add a new entry here: COST_MODE_RATE_9: SC_RATE_9.
# No other files need to change.

TIERED_RATE_REGISTRY: dict[str, RateSchedule] = {
    COST_MODE_RATE_8: SC_RATE_8,
    COST_MODE_RATE_6: SC_RATE_6,
}


def get_season(month: int) -> Season:
    """
    Determine billing season from a calendar month number (1-12).

    Args:
        month: Calendar month as an integer (1 = January, 12 = December).

    Returns:
        :attr:`Season.SUMMER` for May-September (months 5-9),
        :attr:`Season.WINTER` for all other months.

    """
    if 5 <= month <= 9:
        return Season.SUMMER
    return Season.WINTER


def calculate_tiered_cost(
    interval_wh: float,
    cumulative_before: float,
    tiered_rate: TieredRate,
) -> float:
    """
    Calculate cost for a single usage interval using two-tier pricing.

    Correctly handles the case where the cumulative usage counter crosses the
    tier boundary *within* this interval — i.e. part of the interval is billed
    at the lower tier and part at the higher tier.

    Example with boundary = 800 kWh (800,000 Wh):
        - cumulative_before = 790,000 Wh (already used 790 kWh this cycle)
        - interval_wh       =  20,000 Wh (used 20 kWh in this interval)
        - cumulative_after  = 810,000 Wh → straddles the boundary
        - wh_under  = 10,000 Wh (billed at rate_under)
        - wh_over   = 10,000 Wh (billed at rate_over)

    Args:
        interval_wh:      Wh consumed in this interval.
        cumulative_before: Total Wh consumed *before* this interval in the
                           current billing cycle. Used to determine whether
                           the interval straddles the tier boundary.
        tiered_rate:      The :class:`TieredRate` to apply (summer or winter).

    Returns:
        Cost in dollars for this interval.

    """
    boundary = tiered_rate.boundary_wh
    cumulative_after = cumulative_before + interval_wh

    if cumulative_after <= boundary:
        # Entire interval falls below (or exactly at) the tier boundary —
        # all usage is in the lower tier.
        return interval_wh * tiered_rate.rate_under

    if cumulative_before >= boundary:
        # The cycle already exceeded the boundary before this interval —
        # all usage is in the upper tier.
        return interval_wh * tiered_rate.rate_over

    # The interval straddles the boundary. Split into two portions:
    wh_under = boundary - cumulative_before  # Wh still in the lower tier
    wh_over = interval_wh - wh_under  # Wh in the upper tier
    return wh_under * tiered_rate.rate_under + wh_over * tiered_rate.rate_over


def calculate_sc_rate_interval_cost(
    interval_wh: float,
    interval_dt: datetime,
    cumulative_before: float,
    schedule: RateSchedule,
) -> float:
    """
    Calculate the cost for a single usage interval under a given SC rate schedule.

    Determines the correct season from ``interval_dt``, selects the matching
    :class:`TieredRate`, and delegates to :func:`calculate_tiered_cost`.

    Note: this function does *not* enforce the schedule's ``effective_date``.
    That check is the responsibility of :func:`~.cost._calculate_cost_for_wh`
    so that historical data before the rate was in effect is priced at $0.

    Args:
        interval_wh:      Wh consumed in this interval.
        interval_dt:      Timestamp of the interval's start. Its month is used
                          to determine summer vs. winter season.
        cumulative_before: Total Wh consumed before this interval in the current
                           billing cycle (for tier-boundary tracking).
        schedule:         The :class:`RateSchedule` to apply.

    Returns:
        Cost in dollars for this interval, or ``0.0`` if ``interval_wh <= 0``.

    """
    if interval_wh <= 0:
        return 0.0

    season = get_season(interval_dt.month)

    # Select the season-specific tiered rate.
    tiered_rate = (
        schedule.rates.summer if season == Season.SUMMER else schedule.rates.winter
    )

    return calculate_tiered_cost(interval_wh, cumulative_before, tiered_rate)


def build_cost_mode_choices() -> dict[str, str]:
    """
    Build the ordered cost-mode selector dict used by ConfigFlow and OptionsFlow.

    Returns a mapping of ``{cost_mode_key: display_label}`` in the order they
    should appear in the UI selector: None first, then all tiered schedules
    (in registry order), then Fixed Rate last.

    Returns:
        Ordered dict suitable for passing to :class:`voluptuous.In` or an HA
        ``SelectSelector``.

    """
    tiered_choices = {
        mode: schedule.name for mode, schedule in TIERED_RATE_REGISTRY.items()
    }
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        **tiered_choices,
        COST_MODE_FIXED: "Fixed Rate (custom)",
    }
