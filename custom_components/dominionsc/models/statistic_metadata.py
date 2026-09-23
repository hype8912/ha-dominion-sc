"""Metadata describing a single HA long-term statistic series."""

from dataclasses import dataclass
from string import Template


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
                         behavior (all registers summed together) which is
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
