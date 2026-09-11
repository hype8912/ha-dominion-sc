"""
Data models for the dominionsc coordinator.

These dataclasses were previously defined inline in coordinator.py. They
carry no Home Assistant dependency and are separated here so the coordinator
module can focus on lifecycle and orchestration.

There are two layers of data:

1. **Statistic metadata** (:class:`DominionSCStatisticMetadata`) — describes
   *what* a statistic series represents (account type, unit, HA statistic IDs).
   Created once per account (or per register for multi-register solar accounts)
   and passed down through the statistics-insertion call chain.

2. **Coordinator data** (:class:`DominionSCData`) — the snapshot returned by
   the coordinator after each poll. Sensor entities read from this via their
   ``native_value`` property. It contains per-account data
   (:class:`DominionSCAccountData`) and the shared billing forecast.

Backward compatibility: coordinator.py re-exports these names, so existing
imports of ``from ...coordinator import DominionSCStatisticMetadata`` (etc.)
continue to resolve. See docs/REFACTOR_PLAN.md Phase 1.
"""

from dataclasses import dataclass
from datetime import datetime
from string import Template

from dominionsc import Forecast


@dataclass
class DominionSCStatisticMetadata:
    """
    Metadata bundle describing a single HA long-term statistic series.

    One instance is created for each combination of (account, register) and
    passed through the statistics-insertion helpers so they all operate on the
    same statistic IDs, units, and display name without needing to re-derive
    them at each layer.

    Attributes:
        account:         Account type identifier as returned by the Dominion API.
                         Typical values: ``"ELECTRIC"``, ``"GAS"``.
        consumption_id:  Full HA statistic ID for energy consumption, e.g.
                         ``"dominionsc:123_main_st_electric_energy_consumption"``.
                         This ID is what the HA Energy Dashboard uses.
        cost_id:         Full HA statistic ID for cost in USD, e.g.
                         ``"dominionsc:123_main_st_electric_energy_cost"``.
                         ``None`` when the cost mode is ``COST_MODE_NONE`` or
                         no cost mode is configured for this account type.
                         Set for both ELECTRIC and GAS accounts when a rate
                         plan is selected.
        name_prefix:     A :class:`~string.Template` that produces human-readable
                         names for both the consumption and cost statistics.
                         Call ``name_prefix.substitute(stat_type="consumption")``
                         or ``name_prefix.substitute(stat_type="cost")``.
        unit_class:      HA unit class string (e.g. ``"energy"``, ``"volume"``).
                         Used in ``StatisticMetaData`` passed to the recorder.
        unit:            HA unit of measurement (e.g. ``"Wh"``, ``"ft³"``).
        usage_point_id:  ESPI UsagePoint ID identifying the physical meter
                         register. ``None`` uses the legacy merged-register
                         behaviour (all registers summed together) which is
                         the correct choice for accounts with a single meter.
                         A net-metered solar account has one metadata object
                         per register (grid delivery, solar export), each with
                         its own ``usage_point_id``. See REFACTOR_PLAN.md §5.

    """

    account: str
    consumption_id: str
    cost_id: str | None
    name_prefix: Template
    unit_class: str
    unit: str
    # Default None = legacy/sole-register path. Set only for multi-register accounts.
    usage_point_id: str | None = None


@dataclass
class DominionSCAccountData:
    """
    Per-account snapshot returned by the coordinator after each poll.

    One instance exists per account type (ELECTRIC, GAS). Sensor entities
    that are scoped to a specific account read from the matching instance.

    Attributes:
        account:      Account type (``"ELECTRIC"`` or ``"GAS"``).
        last_changed: Timestamp of the most recent usage interval that was
                      processed and inserted into statistics. ``None`` if no
                      statistics have been inserted yet (e.g. data not
                      available from the API).

    """

    account: str
    last_changed: datetime | None


@dataclass
class DominionSCData:
    """
    Full coordinator data snapshot, refreshed on every poll cycle.

    This is the object returned by
    :meth:`~.coordinator.DominionSCCoordinator._async_update_data` and stored
    in ``coordinator.data``. Sensor entities receive a reference to this via
    the ``CoordinatorEntity`` base class and read the fields they need.

    Attributes:
        accounts:               Mapping of account type → per-account data.
                                Keys are strings like ``"ELECTRIC"``, ``"GAS"``.
        forecast:               Current billing-cycle forecast from the Dominion
                                API (cost to date, projected cost, cycle dates).
                                ``None`` when the API call fails or the account
                                does not have forecast data.
        service_addr_account_no: The service-address / account number string from
                                the API. Used as the display name for the device
                                and to build stable statistic IDs.
        last_updated:           UTC timestamp of when this data was fetched.
                                Exposed as the ``last_updated`` diagnostic sensor.

    """

    accounts: dict[str, "DominionSCAccountData"]
    forecast: Forecast | None
    service_addr_account_no: str
    last_updated: datetime
    gas_cost_to_date: float | None = None
