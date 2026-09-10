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

from dominionsc import RatePlan, get_rate_plan

from .const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_6,
    COST_MODE_RATE_8,
)

# Maps each supported cost-mode key to the corresponding library RatePlan.
# Keys must equal the RatePlan.code field so get_rate_plan(key) resolves them.
# Only plans that are currently supported for cost calculation are included;
# unknown codes are silently omitted by the comprehension.
RATE_PLAN_REGISTRY: dict[str, RatePlan] = {
    mode: plan
    for mode in (COST_MODE_RATE_8, COST_MODE_RATE_6)
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
