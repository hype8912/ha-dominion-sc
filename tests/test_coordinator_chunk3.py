"""Chunk 3 tests for coordinator: init + update data."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import CONF_COST_MODE, COST_MODE_RATE_8, DOMAIN
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    DominionSCData,
)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "test",
            CONF_PASSWORD: "test",
            CONF_COST_MODE: COST_MODE_RATE_8,
        },
    )


async def test_coordinator_init(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    assert coord.api is not None
    # Dummy listener registered
    assert len(coord._listeners) > 0


async def test_async_update_data(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    # Mock API
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock()
    coord.api.async_get_accounts = AsyncMock(return_value=({"ELECTRIC"}, "123 Main"))
    forecast_mock = MagicMock()
    forecast_mock.start_date = datetime(2025, 7, 23).date()
    forecast_mock.end_date = datetime(2025, 7, 31).date()
    coord.api.async_get_forecast = AsyncMock(return_value=forecast_mock)
    # Patch statistics insertion
    with patch.object(
        coord,
        "_insert_statistics",
        new=AsyncMock(return_value={"ELECTRIC": datetime(2025, 7, 1)}),
    ):
        data = await coord._async_update_data()
    assert isinstance(data, DominionSCData)
    assert "ELECTRIC" in data.accounts
