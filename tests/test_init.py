"""Tests for dominionsc __init__."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc import (
    async_setup_entry,
    async_unload_entry,
    update_listener,
)
from custom_components.dominionsc.const import DOMAIN


@pytest.fixture
def mock_coordinator() -> MagicMock:
    coord = MagicMock()
    coord.async_config_entry_first_refresh = AsyncMock()
    coord.async_request_refresh = AsyncMock()
    return coord


async def test_async_setup_entry(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"username": "test"}, entry_id="test-id"
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.dominionsc.DominionSCCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
        ) as mock_fwd,
    ):
        result = await async_setup_entry(hass, entry)
    assert result is True
    mock_fwd.assert_awaited_once()


async def test_async_unload_entry(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="unload-id")
    entry.add_to_hass(hass)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=True
    ) as mock_unload:
        result = await async_unload_entry(hass, entry)
    assert result is True
    mock_unload.assert_awaited_once()


async def test_update_listener(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="listener-id")
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator
    await update_listener(hass, entry)
    mock_coordinator.async_request_refresh.assert_awaited_once()
