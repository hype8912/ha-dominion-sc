"""Per-account snapshot produced by the coordinator."""

from dataclasses import dataclass
from datetime import datetime


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
