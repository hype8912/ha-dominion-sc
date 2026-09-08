"""
Pure statistic-id construction.

Builds the Home Assistant long-term-statistic ids and display-name prefix for
a given account. Carries no Home Assistant dependency (only string handling).

Backward compatibility: coordinator.py re-exports this name, so existing
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
