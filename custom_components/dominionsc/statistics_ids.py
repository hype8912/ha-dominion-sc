"""
Pure statistic-ID construction.

Builds the Home Assistant long-term-statistic IDs and display-name
:class:`~string.Template` for a given account / meter register.

Carries no Home Assistant dependency — only string operations from the
standard library.

CRITICAL: Statistic IDs are **release-critical**
-------------------------------------------------
Every statistic ID produced by this module is used as the primary key for
data stored in the HA recorder's ``statistics`` table. Changing an existing
ID orphans all historical data for that series — the Energy Dashboard loses
its history and users see a gap from the install date backward.

Rules:
    - **Never rename** a function, change its formula, or alter how it handles
      its inputs for data that is already in the wild.
    - **Adding** a new function / code path for new installs is fine.
    - If a breaking change becomes unavoidable, coordinate a migration step
      that moves the old statistic rows to the new ID in the recorder.

See docs/REFACTOR_PLAN.md §Constraints for the full policy.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _build_statistic_ids`` continue to
resolve. See docs/REFACTOR_PLAN.md Phase 2.
"""

from string import Template

from .const import DOMAIN, clean_service_addr


def _build_statistic_ids(
    service_addr_account_no: str,
    account: str,
) -> tuple[str, str | None, Template]:
    """
    Construct the legacy (merged-register) statistic IDs for an account.

    This is the **original** ID scheme, in use since the first release. It
    treats every meter register for an account as a single merged stream.
    For accounts with exactly one physical meter (all gas accounts, and most
    electric accounts) this is correct. For net-metered solar accounts with two
    registers (grid delivery + solar export) the IDs would merge two separate
    data streams — that case is handled by :func:`_build_register_statistic_ids`.

    ID format:
        ``dominionsc:{clean_addr}_{account}_energy_consumption``
        ``dominionsc:{clean_addr}_{account}_energy_cost``

    Example for service address ``"123 Main St"`` and account ``"ELECTRIC"``::

        dominionsc:123_main_st_electric_energy_consumption
        dominionsc:123_main_st_electric_energy_cost

    WARNING: Do not change this function. Every existing install's Energy
    Dashboard history is keyed on the IDs it produces.

    Args:
        service_addr_account_no: Raw service-address / account-number string
                                 from the Dominion API (e.g. ``"1234567-8"``).
        account:                 Account type string (``"ELECTRIC"`` or
                                 ``"GAS"``).

    Returns:
        A 3-tuple:
        - ``consumption_statistic_id`` (str): HA statistic ID for energy use.
        - ``cost_statistic_id`` (str): HA statistic ID for cost in USD.
          The coordinator nullifies this value for accounts where no cost mode
          is active (e.g. ``CONF_GAS_COST_MODE == COST_MODE_NONE``).
        - ``name_prefix`` (Template): produces human-readable display names via
          ``name_prefix.substitute(stat_type="consumption")`` or
          ``name_prefix.substitute(stat_type="cost")``.

    """
    clean_addr = clean_service_addr(service_addr_account_no)
    id_prefix = (f"{clean_addr}_{account}").lower().replace("-", "_")
    consumption_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
    # Cost IDs are generated for both ELECTRIC and GAS accounts so that gas cost
    # statistics can be written when a gas rate plan is selected. The coordinator
    # nullifies the cost_id for accounts where no cost mode is active (i.e. when
    # CONF_GAS_COST_MODE == COST_MODE_NONE).
    cost_id = f"{DOMAIN}:{id_prefix}_energy_cost"
    name_prefix = Template(f"{account.title()} $stat_type {service_addr_account_no}")
    return consumption_id, cost_id, name_prefix


def _build_register_statistic_ids(
    service_addr_account_no: str,
    account: str,
    usage_point_id: str,
    is_sole_register: bool,
) -> tuple[str, str | None, Template]:
    """
    Construct statistic IDs for a single physical meter register (Phase 5).

    Called only for accounts that have been through register discovery (i.e.
    accounts that have **never** been backfilled before). The
    ``is_sole_register`` flag determines which ID scheme is chosen:

    **Sole-register path** (``is_sole_register=True``)
        Delegates to :func:`_build_statistic_ids` and returns byte-identical
        IDs to the legacy scheme. This is the common case — all gas accounts
        and most electric accounts have a single physical meter. Using the same
        IDs means that if a previously established install somehow reaches this
        code path (it structurally cannot, but as a belt-and-suspenders
        guarantee), its Energy Dashboard history would still be intact.

    **Multi-register path** (``is_sole_register=False``)
        Incorporates the ESPI ``usage_point_id`` into the ID so each physical
        register gets its own independent statistic series. This is used for
        net-metered solar accounts that have two registers: one for grid
        delivery (import) and one for solar export. The pre-existing merged
        statistic for such accounts was already corrupt (both registers summed
        into one stream), so there is no valid history to preserve and the new
        IDs are safe to use.

    Display names for multi-register accounts include the last 6 digits of the
    ``usage_point_id`` so the user can identify which statistic corresponds to
    which physical meter when wiring into the Energy Dashboard. The Dominion
    API does not reliably report flow direction (import vs. export), so it is
    left to the user to identify registers.

    ID format (multi-register)::

        dominionsc:{clean_addr}_{account}_{safe_up}_energy_consumption
        dominionsc:{clean_addr}_{account}_{safe_up}_energy_cost

    Args:
        service_addr_account_no: Raw service-address / account-number string.
        account:                 Account type (``"ELECTRIC"`` or ``"GAS"``).
        usage_point_id:          ESPI UsagePoint ID for this register, as
                                 returned by
                                 ``api.async_get_register_reads()``.
        is_sole_register:        ``True`` if this is the only register for the
                                 account (use legacy IDs), ``False`` if there
                                 are two or more registers (use per-register
                                 IDs).

    Returns:
        A 3-tuple ``(consumption_id, cost_id, name_prefix)`` — same structure
        as :func:`_build_statistic_ids`.

    """
    if is_sole_register:
        # Delegate to legacy scheme to guarantee ID stability for the common case.
        return _build_statistic_ids(service_addr_account_no, account)

    # Multi-register path: embed the usage_point_id to distinguish registers.
    clean_addr = clean_service_addr(service_addr_account_no)
    safe_up = usage_point_id.lower().replace("-", "_")
    id_prefix = (f"{clean_addr}_{account}_{safe_up}").lower().replace("-", "_")
    consumption_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
    cost_id = f"{DOMAIN}:{id_prefix}_energy_cost"

    # Use only the last 6 digits of the UsagePoint ID in the display name —
    # enough for a user to match it to a physical meter label while keeping the
    # name concise.
    short_up = usage_point_id[-6:]
    name_prefix = Template(
        f"{account.title()} $stat_type {service_addr_account_no} (meter {short_up})"
    )
    return consumption_id, cost_id, name_prefix
