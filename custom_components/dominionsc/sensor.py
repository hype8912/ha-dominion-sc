"""
Sensor platform for the dominionsc integration.

This module defines the HA sensor entities that appear in the UI. Sensors
expose billing and account metadata fetched by the coordinator.

Important: the primary data delivered by this integration is **not** through
sensors. Hourly energy consumption and cost data goes directly into the HA
recorder as long-term statistics (see the coordinator) and surfaces in the
Energy Dashboard. Sensors are supplementary, diagnostic entities that show
things like billing cycle cost-to-date and the timestamp of the most recent
data interval.

Sensor types
------------
**Account sensors** (one set per account — ELECTRIC, GAS)
    - ``last_changed``: timestamp of the most recent interval inserted into
      statistics. Useful for checking data freshness.

**Billing sensors** (one set per service address, only when forecast is available)
    - ``cost_to_date``:    Dominion's reported current-cycle spend so far.
    - ``forecasted_cost``: Projected end-of-cycle spend.
    - ``typical_cost``:    Historical average for comparison.
    - ``start_date``:      Billing cycle start (disabled by default in UI).
    - ``end_date``:        Billing cycle end (disabled by default in UI).
    - ``last_updated``:    Timestamp of the coordinator's most recent poll.

All sensors are ``EntityCategory.DIAGNOSTIC`` so they appear in the
Diagnostics section rather than cluttering the main device page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, clean_service_addr
from .coordinator import (
    DominionSCAccountData,
    DominionSCConfigEntry,
    DominionSCCoordinator,
    DominionSCData,
)

# All data updates go through the shared coordinator; sensors do not need to
# make independent API calls, so parallel updates provide no benefit.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DominionSCEntityDescription(SensorEntityDescription):
    """
    Extended sensor description that includes a value-extraction function.

    Inherits all standard HA ``SensorEntityDescription`` fields (key,
    translation_key, device_class, unit, etc.) and adds ``value_fn`` so each
    sensor can declare inline how to extract its value from the coordinator's
    data snapshot.

    Attributes:
        value_fn: A callable that accepts either a
                  :class:`~.models.DominionSCAccountData` (for per-account
                  sensors) or a :class:`~.models.DominionSCData` (for billing
                  sensors) and returns the sensor's current state value.
                  Returning ``None`` marks the sensor as unavailable.

    """

    value_fn: Callable[
        [DominionSCAccountData | DominionSCData], str | float | date | datetime | None
    ]


# ---------------------------------------------------------------------------
# Per-account sensor descriptors
# ---------------------------------------------------------------------------
# One set of these is registered for each account type (ELECTRIC, GAS) found
# on the service address. The ``value_fn`` receives the account's
# DominionSCAccountData instance.

ACCOUNT_SENSORS: tuple[DominionSCEntityDescription, ...] = (
    DominionSCEntityDescription(
        key="last_changed",
        translation_key="last_changed",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # ``last_changed`` is the end_time of the most recent usage interval
        # that was successfully inserted into statistics. ``None`` means no
        # statistics have been inserted yet (waiting for API data).
        value_fn=lambda data: data.last_changed,
    ),
)

# ---------------------------------------------------------------------------
# Billing sensor descriptors (shared across all accounts for a service address)
# ---------------------------------------------------------------------------
# These are only registered when the coordinator's forecast is not None.
# The ``value_fn`` receives the full DominionSCData instance.

BILLING_SENSORS: tuple[DominionSCEntityDescription, ...] = (
    DominionSCEntityDescription(
        key="cost_to_date",
        translation_key="cost_to_date",
        device_class=SensorDeviceClass.MONETARY,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        # Cost accumulated so far in the current billing cycle (from Dominion API).
        value_fn=lambda data: data.forecast.cost_to_date if data.forecast else None,
    ),
    DominionSCEntityDescription(
        key="forecasted_cost",
        translation_key="forecasted_cost",
        device_class=SensorDeviceClass.MONETARY,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        # Dominion's projected end-of-cycle cost based on current usage trend.
        value_fn=lambda data: data.forecast.forecasted_cost if data.forecast else None,
    ),
    DominionSCEntityDescription(
        key="typical_cost",
        translation_key="typical_cost",
        device_class=SensorDeviceClass.MONETARY,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        # Historical average cost for this time of year (from Dominion API).
        value_fn=lambda data: data.forecast.typical_cost if data.forecast else None,
    ),
    DominionSCEntityDescription(
        key="start_date",
        translation_key="start_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,  # Hidden by default; enable if needed
        value_fn=lambda data: data.forecast.start_date if data.forecast else None,
    ),
    DominionSCEntityDescription(
        key="end_date",
        translation_key="end_date",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,  # Hidden by default; enable if needed
        value_fn=lambda data: data.forecast.end_date if data.forecast else None,
    ),
    DominionSCEntityDescription(
        key="last_updated",
        translation_key="last_updated",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        # UTC timestamp set by the coordinator after each successful poll.
        value_fn=lambda data: data.last_updated,
    ),
)


# Gas cost sensor — registered only when gas cost statistics are being written.
GAS_COST_SENSOR = DominionSCEntityDescription(
    key="gas_cost_to_date",
    translation_key="gas_cost_to_date",
    device_class=SensorDeviceClass.MONETARY,
    entity_category=EntityCategory.DIAGNOSTIC,
    native_unit_of_measurement="USD",
    state_class=SensorStateClass.TOTAL,
    suggested_display_precision=2,
    # Reads the running sum from the gas cost statistic.
    value_fn=lambda data: data.gas_cost_to_date,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DominionSCConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """
    Set up sensor entities for a DominionSC config entry.

    Called by HA after ``__init__.async_setup_entry`` has initialised the
    coordinator and forwarded platform setup. Creates one
    :class:`DominionSCSensor` per (account, sensor description) combination
    for account sensors, and one per billing sensor description if a forecast
    is available.

    All entities share a single HA device (keyed on the service address) so
    they appear grouped in the device registry.

    Args:
        hass:               The Home Assistant instance.
        entry:              The config entry being set up.
        async_add_entities: Callback to register the new sensor entities.

    """
    coordinator = entry.runtime_data
    entities: list[DominionSCSensor] = []

    dominionsc_data = coordinator.data
    accounts_data = dominionsc_data.accounts
    forecast = dominionsc_data.forecast
    service_addr_account_no = dominionsc_data.service_addr_account_no
    clean_addr = clean_service_addr(service_addr_account_no)

    # One HA device per service address. All sensors (electric, gas, billing)
    # are grouped under this device in the device registry.
    device_id = f"{DOMAIN}_{clean_addr}"
    device = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=f"{service_addr_account_no}",
        manufacturer="Dominion Energy SC",
        entry_type=DeviceEntryType.SERVICE,
    )

    # Register per-account sensors (e.g. last_changed for ELECTRIC, GAS).
    for account in accounts_data:
        entities.extend(
            DominionSCSensor(
                coordinator,
                sensor,
                account,
                device,
                device_id,
            )
            for sensor in ACCOUNT_SENSORS
        )

    # Register billing sensors only when forecast data is available.
    # The forecast can be None if the API call failed or the account lacks it.
    if forecast is not None:
        entities.extend(
            DominionSCSensor(
                coordinator,
                sensor,
                "billing",
                device,
                device_id,
            )
            for sensor in BILLING_SENSORS
        )

    # Register gas cost sensor only when gas cost statistics are present.
    if dominionsc_data.gas_cost_to_date is not None:
        entities.append(
            DominionSCSensor(
                coordinator,
                GAS_COST_SENSOR,
                "billing",
                device,
                device_id,
            )
        )

    async_add_entities(entities)


class DominionSCSensor(CoordinatorEntity[DominionSCCoordinator], SensorEntity):
    """
    A single DominionSC sensor entity backed by the shared coordinator.

    Inherits from :class:`~homeassistant.helpers.update_coordinator.CoordinatorEntity`
    so it automatically re-renders whenever the coordinator publishes new data,
    and from :class:`~homeassistant.components.sensor.SensorEntity` for the
    standard HA sensor contract.

    The actual value extraction is delegated to the ``value_fn`` in the
    entity's :class:`DominionSCEntityDescription`, keeping this class generic.
    """

    _attr_has_entity_name = True
    entity_description: DominionSCEntityDescription

    def __init__(
        self,
        coordinator: DominionSCCoordinator,
        description: DominionSCEntityDescription,
        account: str,
        device: DeviceInfo,
        device_id: str,
    ) -> None:
        """
        Initialise a DominionSC sensor.

        Args:
            coordinator: The shared data coordinator for this config entry.
            description: The sensor descriptor (key, device class, value_fn…).
            account:     Account type this sensor belongs to (``"ELECTRIC"``,
                         ``"GAS"``, or ``"billing"`` for shared billing sensors).
            device:      HA DeviceInfo shared by all sensors on this service address.
            device_id:   String identifier for the device, used to build a
                         globally unique entity ``unique_id``.

        """
        super().__init__(coordinator)
        self.entity_description = description
        # unique_id must be stable across restarts and globally unique within HA.
        self._attr_unique_id = f"{device_id}_{account}_{description.key}"
        self._attr_device_info = device
        # ``account`` is injected into translation strings so the sensor's
        # display name reads e.g. "Electric last changed" or "Gas last changed".
        self._attr_translation_placeholders = {"account": account.title()}
        self.account = account

    @property
    def native_value(self) -> StateType | date | datetime:
        """
        Return the current sensor value.

        Dispatches to either the shared coordinator data (for billing sensors)
        or the per-account data slice (for account sensors) based on
        ``self.account``, then delegates to ``entity_description.value_fn``
        for the actual value extraction.
        """
        coordinator_data = self.coordinator.data

        if self.account == "billing":
            # Billing sensors operate on the full coordinator data snapshot
            # (they read from coordinator_data.forecast).
            return self.entity_description.value_fn(coordinator_data)

        # Account sensors operate on the per-account data slice.
        return self.entity_description.value_fn(coordinator_data.accounts[self.account])
