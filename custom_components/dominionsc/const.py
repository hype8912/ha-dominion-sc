"""
Constants for the dominionsc integration.

All integration-wide constants are defined here to provide a single source of
truth and to avoid circular imports. Groupings:

**Identity**
    :data:`DOMAIN`, :data:`COMMON_NAME`

**Config-entry data keys** (stored in ``entry.data``, set during config flow)
    :data:`CONF_LOGIN_DATA`

**Config-entry options keys** (stored in ``entry.options``, user-changeable)
    :data:`CONF_COST_MODE`, :data:`CONF_FIXED_RATE`,
    :data:`CONF_EXTENDED_BACKFILL`, :data:`CONF_EXTENDED_COST_BACKFILL`,
    :data:`CONF_GAS_COST_MODE`

**Cost mode identifiers** (values for ``CONF_COST_MODE`` and ``CONF_GAS_COST_MODE``)
    :data:`COST_MODE_NONE`, :data:`COST_MODE_FIXED`,
    :data:`COST_MODE_RATE_2`, :data:`COST_MODE_RATE_5`,
    :data:`COST_MODE_RATE_6`, :data:`COST_MODE_RATE_7`,
    :data:`COST_MODE_RATE_8`, :data:`COST_MODE_RATE_32S`,
    :data:`COST_MODE_RATE_32V`

**Tuning parameters**
    :data:`DEFAULT_FIXED_RATE`, :data:`EXTENDED_BACKFILL_DAYS`,
    :data:`LOOKBACK_DAYS`

Adding a new cost mode
----------------------
1. Add a new ``COST_MODE_*`` constant here.
2. Add it to :data:`~.rates.RATE_PLAN_REGISTRY` (electric) or
   :data:`~.rates.GAS_RATE_PLAN_REGISTRY` (gas).
3. Add an entry to :func:`~.rates.build_cost_mode_choices` or
   :func:`~.rates.build_gas_cost_mode_choices`.
No other files need to change.
"""

import re
from typing import Final

# ---------------------------------------------------------------------------
# Integration identity
# ---------------------------------------------------------------------------

# Unique identifier for this integration, used as:
#   - the key in hass.data[DOMAIN]
#   - the prefix for all statistic IDs (e.g. "dominionsc:addr_electric_consumption")
#   - the config-entry domain
DOMAIN = "dominionsc"

# Human-readable display name shown in the UI.
COMMON_NAME = "Dominion Energy SC"

# ---------------------------------------------------------------------------
# Config-entry DATA keys  (entry.data — credentials, not user-configurable)
# ---------------------------------------------------------------------------

# Serialised TFA session token returned by the ``dominionsc`` library after a
# successful two-factor authentication. Persisting it allows subsequent logins
# to skip the TFA challenge entirely (the library handles the cookie exchange).
# Set to ``None`` on the first login (before TFA has been completed).
CONF_LOGIN_DATA = "login_data"

# ---------------------------------------------------------------------------
# Config-entry OPTIONS keys  (entry.options — user-configurable via options flow)
# ---------------------------------------------------------------------------

# Which cost-calculation mode to use for electric. One of the COST_MODE_* constants below.
CONF_COST_MODE: Final = "cost_mode"

# Which cost-calculation mode to use for gas. One of COST_MODE_NONE, COST_MODE_RATE_32S,
# or COST_MODE_RATE_32V. Defaults to COST_MODE_NONE (no gas cost calculation).
# Only relevant when the account has a GAS meter.
CONF_GAS_COST_MODE: Final = "gas_cost_mode"

# User-supplied flat rate in $/kWh. Only read when CONF_COST_MODE == COST_MODE_FIXED.
CONF_FIXED_RATE: Final = "fixed_rate"

# If True, seed up to EXTENDED_BACKFILL_DAYS of consumption history on first setup.
# Default is False (only backfill since the start of the current billing cycle).
CONF_EXTENDED_BACKFILL: Final = "extended_backfill"

# If True, also calculate cost statistics over the extended consumption window.
# Requires CONF_EXTENDED_BACKFILL == True (cost is derived from consumption data).
# Default is False (cost only calculated from the current billing cycle start).
CONF_EXTENDED_COST_BACKFILL: Final = "extended_cost_backfill"

# ---------------------------------------------------------------------------
# Cost mode identifiers
# ---------------------------------------------------------------------------

# No cost calculation — only energy consumption statistics are produced.
COST_MODE_NONE: Final = "none"

# User-defined flat rate in $/kWh (value from CONF_FIXED_RATE).
COST_MODE_FIXED: Final = "fixed"

# Dominion Energy SC Rate Schedule 8 — Residential Service.
# Tiered (first 800 kWh vs. over 800 kWh) with seasonal summer/winter rates.
# See rates.SC_RATE_8 for the exact values.
COST_MODE_RATE_8: Final = "rate_8"

# Dominion Energy SC Rate Schedule 6 — Energy Saver / Conservation Rate.
# Tiered with seasonal summer/winter rates. See rates.SC_RATE_6 for details.
COST_MODE_RATE_6: Final = "rate_6"

# Dominion Energy SC Rate Schedule 2 — Low Use Residential Service.
# Flat rate electric plan for customers with limited monthly usage.
COST_MODE_RATE_2: Final = "rate_2"

# Dominion Energy SC Rate Schedule 5 — Time of Use.
# TOU pricing: on-peak, super-off-peak, and off-peak periods (no demand charge).
COST_MODE_RATE_5: Final = "rate_5"

# Dominion Energy SC Rate Schedule 7 — Time-of-Use Demand.
# TOU pricing identical to Rate 5 plus an on-peak billing demand charge.
# The demand charge is NOT tracked in long-term statistics (requires billing-
# period maximum demand, not summable interval data).
COST_MODE_RATE_7: Final = "rate_7"

# Dominion Energy SC Rate Schedule 32S — Gas Residential Standard Service.
# Flat rate gas plan ($/therm). Usage is measured in ft³; conversion: 1 therm = 100 ft³.
COST_MODE_RATE_32S: Final = "rate_32s"

# Dominion Energy SC Rate Schedule 32V — Gas Residential Value Service.
# Flat rate gas plan ($/therm). Requires average summer usage ≥ 10 therms/month.
COST_MODE_RATE_32V: Final = "rate_32v"

# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

# Default fixed rate when the user selects COST_MODE_FIXED but has not yet
# entered a custom value. 0.14164 $/kWh matches the Rate 6 first-tier rate.
# Note: internally cost calculations receive this value in $/kWh and convert
# to $/Wh by dividing by 1000 (see cost._calculate_cost_for_wh).
DEFAULT_FIXED_RATE: Final = 0.14164  # $/kWh

# ---------------------------------------------------------------------------
# Backfill / lookback tuning
# ---------------------------------------------------------------------------

# Maximum number of days to backfill when the user enables extended backfill.
# Fetching too far back is slow and the API may not have data older than a year.
EXTENDED_BACKFILL_DAYS: Final = 365

# Number of days to re-examine on each incremental update in addition to new
# days. This "lookback window" catches late-arriving API data (the Dominion API
# sometimes delivers an interval a day or two after the fact).
LOOKBACK_DAYS: Final = 5


# ---------------------------------------------------------------------------
# Rate schema versioning
# ---------------------------------------------------------------------------

# Key stored in entry.data to track which rate-value schema the user's
# statistics were last calculated against. Used to detect when tariff values
# have changed and prompt the user to recalculate historical cost data.
CONF_LAST_RATE_SCHEMA_VERSION: Final = "last_rate_schema_version"

# Increment this whenever tariff rates are corrected (not just added).
# Existing users whose stored version is lower will see a persistent
# notification prompting them to recalculate historical cost statistics.
CURRENT_RATE_SCHEMA_VERSION: Final = 2

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def clean_service_addr(service_addr_account_no: str) -> str:
    """
    Normalise a service-address / account number into a safe identifier fragment.

    Used to build statistic IDs and device identifiers that must be safe for
    use in URLs, file paths, and HA's entity registry. The same transformation
    is applied every time, so the output is stable for a given input.

    Transformation rules:
    - Any run of non-word characters (anything that is not ``[a-zA-Z0-9_]``)
      is replaced with a single underscore.
    - A leading digit is prefixed with an underscore (Python identifiers and
      many HA IDs cannot start with a digit).
    - Leading/trailing underscores are stripped.
    - The result is lowercased.

    Examples::

        clean_service_addr("123 Main St")  → "123_main_st"
        clean_service_addr("1234567-8")    → "1234567_8"
        clean_service_addr("ABC-123")      → "abc_123"

    Args:
        service_addr_account_no: Raw service address or account number string
                                 as returned by the Dominion API.

    Returns:
        A lowercase, underscore-separated identifier with no leading digits or
        special characters.

    Warning:
        **Do not change this function.** The output is embedded in every
        statistic ID stored in the HA recorder. Changing it would orphan all
        existing Energy Dashboard history for existing installs.

    """
    return re.sub(r"[\W]+|^(?=\d)", "_", service_addr_account_no).strip("_").lower()
