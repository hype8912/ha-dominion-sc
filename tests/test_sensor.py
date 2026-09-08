"""Tests for dominionsc sensor."""

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import DOMAIN
from custom_components.dominionsc.sensor import (
    ACCOUNT_SENSORS,
    BILLING_SENSORS,
    DominionSCSensor,
    async_setup_entry,
)


@pytest.fixture
def mock_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    coord.data.accounts = {"electric": account_data}
    coord.data.forecast = MagicMock()
    coord.data.forecast.cost_to_date = 10.5
    coord.data.forecast.forecasted_cost = 20.0
    coord.data.forecast.typical_cost = 15.0
    coord.data.forecast.start_date = "2025-01-01"
    coord.data.forecast.end_date = "2025-02-01"
    return coord


async def test_async_setup_entry_with_forecast(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = mock_coordinator
    mock_add = MagicMock()
    await async_setup_entry(hass, entry, mock_add)
    mock_add.assert_called_once()
    args = mock_add.call_args[0][0]
    assert len(args) > 0


async def test_async_setup_entry_no_forecast(hass: HomeAssistant) -> None:
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    coord.data.accounts = {"electric": account_data}
    coord.data.forecast = None
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord
    mock_add = MagicMock()
    await async_setup_entry(hass, entry, mock_add)
    mock_add.assert_called_once()


def test_dominionsc_sensor_account() -> None:
    coord = MagicMock()
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    coord.data = MagicMock()
    coord.data.accounts = {"electric": account_data}
    desc = ACCOUNT_SENSORS[0]
    device = MagicMock()
    sensor = DominionSCSensor(coord, desc, "electric", device, "dev_1")
    assert sensor.account == "electric"
    assert sensor.native_value == "2025-01-01"


def test_dominionsc_sensor_billing() -> None:
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = MagicMock()
    coord.data.forecast.cost_to_date = 99.99
    desc = BILLING_SENSORS[0]
    device = MagicMock()
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")
    assert sensor.account == "billing"
    assert sensor.native_value == 99.99
