"""
Adapter between HA cost-mode keys and the library's RatePlan objects.

The rate definitions live in the ``dominion-sc-power`` library
(``dominionsc.rates``). This module is responsible only for:

- Exposing the library's residential catalogs keyed by ``COST_MODE_*`` string
  via :data:`RATE_PLAN_REGISTRY` (electric) and :data:`GAS_RATE_PLAN_REGISTRY`
  (gas). The library ``RatePlan.code`` values are the cost-mode keys, so
  a plan added to the library appears here without any change.
- Building the UI selector dicts used by :mod:`.config_flow` and
  :mod:`.options_flow` via :func:`build_cost_mode_choices` and
  :func:`build_gas_cost_mode_choices`. Labels come from ``RatePlan.name``.

A ``COST_MODE_*`` constant in :mod:`.const` is only needed for plans the
integration refers to by name in code (e.g. the Rate 8 default).
"""

from __future__ import annotations

from dominionsc import RESIDENTIAL_ELECTRIC_RATE_PLANS, RESIDENTIAL_GAS_RATE_PLANS, RatePlan

from .const import (
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_1,
    COST_MODE_RATE_2,
    COST_MODE_RATE_5,
    COST_MODE_RATE_6,
    COST_MODE_RATE_7,
    COST_MODE_RATE_8,
)

# Electric plans available for cost calculation, keyed by cost-mode string.
RATE_PLAN_REGISTRY: dict[str, RatePlan] = dict(RESIDENTIAL_ELECTRIC_RATE_PLANS)

# Gas plans available for cost calculation, keyed by cost-mode string.
GAS_RATE_PLAN_REGISTRY: dict[str, RatePlan] = dict(RESIDENTIAL_GAS_RATE_PLANS)

# Plans shown first in the selector, most common first; any other library plan
# follows in catalog order.
_ELECTRIC_DISPLAY_ORDER: tuple[str, ...] = (
    COST_MODE_RATE_8,
    COST_MODE_RATE_6,
    COST_MODE_RATE_5,
    COST_MODE_RATE_7,
    COST_MODE_RATE_2,
)

# Integration-specific notes appended to the library plan name.
_LABEL_SUFFIXES: dict[str, str] = {
    COST_MODE_RATE_7: " (energy cost only)",  # demand charge is not tracked
    COST_MODE_RATE_1: " (closed to new customers)",
}


def _plan_label(code: str, plan: RatePlan) -> str:
    """Return the selector label for *plan*: library name plus any HA-specific note."""
    return f"{plan.name}{_LABEL_SUFFIXES.get(code, '')}"


def build_cost_mode_choices() -> dict[str, str]:
    """
    Build the ordered cost-mode selector dict used by ConfigFlow and OptionsFlow.

    Returns a mapping of ``{cost_mode_key: display_label}`` in the order they
    should appear in the UI selector: None first, then rate plans (common
    plans first, the rest in library order), then Fixed Rate last.

    Returns:
        Ordered dict suitable for passing to a HA ``SelectSelector``.

    """
    preferred: list[str] = [code for code in _ELECTRIC_DISPLAY_ORDER if code in RATE_PLAN_REGISTRY]
    remaining: list[str] = [code for code in RATE_PLAN_REGISTRY if code not in preferred]
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        **{code: _plan_label(code, RATE_PLAN_REGISTRY[code]) for code in (*preferred, *remaining)},
        COST_MODE_FIXED: "Fixed Rate (custom $/kWh)",
    }


def build_gas_cost_mode_choices() -> dict[str, str]:
    """
    Build the ordered gas cost-mode selector dict used by ConfigFlow and OptionsFlow.

    Returns a mapping of ``{cost_mode_key: display_label}`` for gas rate plans,
    with COST_MODE_NONE first. Only shown when the account has a GAS meter.

    Returns:
        Ordered dict suitable for passing to a HA ``SelectSelector``.

    """
    return {
        COST_MODE_NONE: "None (no cost calculation)",
        **{code: _plan_label(code, plan) for code, plan in GAS_RATE_PLAN_REGISTRY.items()},
    }
