"""
Data models for the dominionsc coordinator.

These dataclasses were previously defined inline in coordinator.py. They
carry no Home Assistant dependency and are separated here so the coordinator
module can focus on lifecycle and orchestration.

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
    """Metadata for creating statistics."""

    account: str
    consumption_id: str
    cost_id: str | None
    name_prefix: Template
    unit_class: str
    unit: str
    usage_point_id: str | None = None
    """ESPI UsagePoint id identifying which physical meter register this
    statistic tracks. ``None`` means "all registers merged" (the legacy,
    pre-register-aware behaviour, retained for backward compatibility).
    A net-metered solar account has one metadata per register (grid delivery,
    solar export), each with its own id. See docs/REFACTOR_PLAN.md Phase 5.
    """


@dataclass
class DominionSCAccountData:
    """Class to hold DominionSC account-specific data."""

    account: str
    last_changed: datetime | None


@dataclass
class DominionSCData:
    """Class to hold all DominionSC shared data and individual accounts."""

    accounts: dict[str, "DominionSCAccountData"]
    forecast: Forecast | None
    service_addr_account_no: str
    last_updated: datetime
