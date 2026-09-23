"""
Data models for the dominionsc coordinator.

These dataclasses were previously defined inline in coordinator.py. They
carry no Home Assistant dependency and are separated here (one per module)
so the coordinator module can focus on lifecycle and orchestration.

There are two layers of data:

1. **Statistic metadata** (:class:`DominionSCStatisticMetadata`) — describes
   *what* a statistic series represents (account type, unit, HA statistic IDs).
   Created once per account (or per register for multi-register solar accounts)
   and passed down through the statistics-insertion call chain.

2. **Coordinator data** (:class:`DominionSCData`) — the snapshot returned by
   the coordinator after each poll. Sensor entities read from this via their
   ``native_value`` property. It contains per-account data
   (:class:`DominionSCAccountData`) and the shared billing forecast.

Backward compatibility: this package re-exports all three names, and
coordinator.py re-exports them too, so ``from ...models import X`` and
``from ...coordinator import X`` both continue to resolve. See
docs/REFACTOR_PLAN.md Phase 1.
"""

from .account_data import DominionSCAccountData
from .coordinator_data import DominionSCData
from .statistic_metadata import DominionSCStatisticMetadata

__all__: list[str] = [
    "DominionSCAccountData",
    "DominionSCData",
    "DominionSCStatisticMetadata",
]
