"""
Adapter between HA cost-mode keys and the library's RatePlan objects.

The rate definitions now live in the ``dominion-sc-power`` library
(``dominionsc.rates``). This module is responsible only for:

- Mapping ``COST_MODE_*`` string keys → :class:`dominionsc.RatePlan` objects
  via :data:`RATE_PLAN_REGISTRY`.
- Building the UI selector dict used by :mod:`.config_flow` and
  :mod:`.options_flow` via :func:`build_cost_mode_choices`.

Adding a new rate plan
----------------------
1. Add a matching ``COST_MODE_*`` constant to :mod:`.const` whose value equals
   the library ``RatePlan.code`` (e.g. ``COST_MODE_RATE_9 = "rate_9"``).
2. Add the new constant to the ``RATE_PLAN_REGISTRY`` comprehension below.
3. Add an entry to :func:`build_cost_mode_choices`.
No other files need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dominionsc import RatePlan, get_rate_plan

from .const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_2,
    COST_MODE_RATE_5,
    COST_MODE_RATE_6,
    COST_MODE_RATE_7,
    COST_MODE_RATE_8,
)

@dataclass(frozen=True)
class _HistoricalTieredRate:
    """
    Internal representation of a superseded tariff period not in the library.

    Cost calculation in :mod:`.cost` uses these values to price historical
    intervals that were in effect before the library's current ``RatePlan``
    became effective. All $/Wh fields are already converted from the source
    $/kWh values so the cost engine can multiply directly against Wh usage.
    """

    effective_from: date
    effective_to: date
    summer_boundary_wh: float  # Wh at which the upper tier begins
    summer_under_per_wh: float  # $/Wh for usage at or below boundary (summer)
    summer_over_per_wh: float  # $/Wh for usage above boundary (summer)
    winter_boundary_wh: float  # Wh at which the upper tier begins
    winter_under_per_wh: float  # $/Wh for usage at or below boundary (winter)
    winter_over_per_wh: float  # $/Wh for usage above boundary (winter)


# Rate 8 tariff values that were in effect from 2025-07-23 to 2026-06-30.
# These are NOT in the dominion-sc-power library; they are preserved here so
# that historical cost statistics can be re-priced correctly on request.
_RATE_8_2025 = _HistoricalTieredRate(
    effective_from=date(2025, 7, 23),
    effective_to=date(2026, 6, 30),
    summer_boundary_wh=800_000,
    summer_under_per_wh=0.14599 / 1000,
    summer_over_per_wh=0.15983 / 1000,
    winter_boundary_wh=800_000,
    winter_under_per_wh=0.14599 / 1000,
    winter_over_per_wh=0.14045 / 1000,
)

# Rate 6 tariff values that were in effect from 2025-07-23 to 2026-06-30.
_RATE_6_2025 = _HistoricalTieredRate(
    effective_from=date(2025, 7, 23),
    effective_to=date(2026, 6, 30),
    summer_boundary_wh=800_000,
    summer_under_per_wh=0.14164 / 1000,
    summer_over_per_wh=0.15505 / 1000,
    winter_boundary_wh=800_000,
    winter_under_per_wh=0.14164 / 1000,
    winter_over_per_wh=0.13628 / 1000,
)

# Historical rate registry — keyed by cost-mode constant, value is an ordered
# list of _HistoricalTieredRate entries (ascending by effective_from).
# cost.py iterates this list to find which historical period covers a given
# interval date before falling through to the current library RatePlan.
HISTORICAL_RATE_REGISTRY: dict[str, list[_HistoricalTieredRate]] = {
    COST_MODE_RATE_8: [_RATE_8_2025],
    COST_MODE_RATE_6: [_RATE_6_2025],
}

# Maps each supported cost-mode key to the corresponding library RatePlan.
# Keys must equal the RatePlan.code field so get_rate_plan(key) resolves them.
# Only plans that are currently supported for cost calculation are included;
# unknown codes are silently omitted by the comprehension.
RATE_PLAN_REGISTRY: dict[str, RatePlan] = {
    mode: plan
    for mode in (
        COST_MODE_RATE_8,
        COST_MODE_RATE_6,
        COST_MODE_RATE_5,
        COST_MODE_RATE_7,
        COST_MODE_RATE_2,
    )
    if (plan := get_rate_plan(mode)) is not None
}


def build_cost_mode_choices() -> dict[str, str]:
    """
    Build the ordered cost-mode selector dict used by ConfigFlow and OptionsFlow.

    Returns a mapping of ``{cost_mode_key: display_label}`` in the order they
    should appear in the UI selector: None first, then all rate plans (in
    registry order), then Fixed Rate last.

    Returns:
        Ordered dict suitable for passing to a HA ``SelectSelector``.

    """
    plan_choices = {mode: plan.name for mode, plan in RATE_PLAN_REGISTRY.items()}
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        **plan_choices,
        COST_MODE_FIXED: "Fixed Rate (custom)",
    }
