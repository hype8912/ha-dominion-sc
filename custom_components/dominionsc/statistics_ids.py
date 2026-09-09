"""Pure statistic-id construction.

Builds the Home Assistant long-term-statistic ids and display-name prefix for
a given account. Carries no Home Assistant dependency (only string handling).

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import _build_statistic_ids`` continue to
resolve. See docs/REFACTOR_PLAN.md Phase 2.

NOTE: statistic ids are release-critical -- changing them orphans existing
installs' Energy Dashboard history. See docs/REFACTOR_PLAN.md Constraints.
"""

from string import Template

from .const import DOMAIN, clean_service_addr


def _build_statistic_ids(
    service_addr_account_no: str,
    account: str,
) -> tuple[str, str | None, Template]:
    """
    Construct the statistic IDs and name prefix for a given account.

    This is the LEGACY, single-statistic-per-account id scheme. It must never
    change: every existing install's Energy Dashboard history is keyed on the
    ids it produces.

    Returns:
        (consumption_statistic_id, cost_statistic_id, name_prefix)
        ``cost_statistic_id`` is ``None`` for non-ELECTRIC accounts.

    """
    clean_addr = clean_service_addr(service_addr_account_no)
    id_prefix = (f"{clean_addr}_{account}").lower().replace("-", "_")
    consumption_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
    cost_id = f"{DOMAIN}:{id_prefix}_energy_cost" if account == "ELECTRIC" else None
    name_prefix = Template(f"{account.title()} $stat_type {service_addr_account_no}")
    return consumption_id, cost_id, name_prefix


def _build_register_statistic_ids(
    service_addr_account_no: str,
    account: str,
    usage_point_id: str,
    is_sole_register: bool,
) -> tuple[str, str | None, Template]:
    """
    Construct statistic IDs for a single physical meter register.

    Statistic-id stability strategy (see docs/REFACTOR_PLAN.md Phase 5):

    - **Sole register** (one UsagePoint for this account -- the common case,
      including all gas and non-solar electric): delegates to
      ``_build_statistic_ids`` so the ids are *byte-identical* to the legacy
      scheme. Existing installs keep their Energy Dashboard history; nothing
      is orphaned.

    - **Multiple registers** (net-metered solar: grid delivery + solar export):
      each register gets its own id incorporating the stable UsagePoint id.
      Such accounts' pre-existing merged statistic was already corrupt
      (two registers with overlapping timestamps summed together), so there
      is no valid history to preserve.

    The library does not label registers as grid vs solar (flowDirection is
    unreliable for this utility), so the display name carries the trailing
    digits of the UsagePoint id for the user to recognise each meter. Users
    choose which entity to wire into the Energy Dashboard themselves.

    Returns:
        (consumption_statistic_id, cost_statistic_id, name_prefix)
        ``cost_statistic_id`` is ``None`` for non-ELECTRIC accounts.

    """
    if is_sole_register:
        return _build_statistic_ids(service_addr_account_no, account)

    clean_addr = clean_service_addr(service_addr_account_no)
    safe_up = usage_point_id.lower().replace("-", "_")
    id_prefix = (f"{clean_addr}_{account}_{safe_up}").lower().replace("-", "_")
    consumption_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
    cost_id = f"{DOMAIN}:{id_prefix}_energy_cost" if account == "ELECTRIC" else None
    # Trailing digits are enough for a human to tell two meters apart while
    # keeping the display name readable.
    short_up = usage_point_id[-6:]
    name_prefix = Template(
        f"{account.title()} $stat_type {service_addr_account_no} (meter {short_up})"
    )
    return consumption_id, cost_id, name_prefix
