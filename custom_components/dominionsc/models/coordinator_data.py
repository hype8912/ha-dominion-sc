"""Full coordinator data snapshot."""

from dataclasses import dataclass
from datetime import datetime

from dominionsc import Forecast

from .account_data import DominionSCAccountData


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

    accounts: dict[str, DominionSCAccountData]
    forecast: Forecast | None
    service_addr_account_no: str
    last_updated: datetime
    gas_cost_to_date: float | None = None
