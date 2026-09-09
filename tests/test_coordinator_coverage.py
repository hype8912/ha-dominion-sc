"""Unit tests for uncovered coordinator sections (baby-step additions)."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dominionsc.exceptions import ApiException, CannotConnect, InvalidAuth
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import CONF_COST_MODE, COST_MODE_FIXED, DOMAIN
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    _billing_cycle_get_gap,
    _estimate_billing_cycles,
)


def test_billing_gap_near_leap_april() -> None:
    assert _billing_cycle_get_gap(date(2024, 4, 1)) == 31
    assert _billing_cycle_get_gap(date(2025, 4, 1)) == 30


def test_billing_gap_near_leap_june() -> None:
    assert _billing_cycle_get_gap(date(2024, 6, 1)) == 30
    assert _billing_cycle_get_gap(date(2025, 6, 1)) == 31


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "t", CONF_PASSWORD: "t", CONF_COST_MODE: COST_MODE_FIXED},
    )


async def test_login_invalid_auth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock(side_effect=InvalidAuth("bad"))
    with pytest.raises(Exception):
        await coord._async_update_data()


async def test_login_cannot_connect(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock(side_effect=CannotConnect("down"))
    with pytest.raises(Exception):
        await coord._async_update_data()


async def test_login_api_exception(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock(
        side_effect=ApiException("err", "https://example.test")
    )
    with pytest.raises(ApiException):
        await coord._async_update_data()


async def test_insert_statistics_backfill_path(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, mock_config_entry)
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock()
    coord.api.async_get_accounts = AsyncMock(return_value=({"ELECTRIC"}, "123 Main"))
    forecast_mock = MagicMock()
    forecast_mock.start_date = date(2025, 7, 1)
    forecast_mock.end_date = date(2025, 7, 31)
    coord.api.async_get_forecast = AsyncMock(return_value=forecast_mock)
    # This account has never been backfilled (recorder returns {} below), so
    # _insert_statistics now performs a one-time register-discovery call
    # before deciding the statistic-id scheme (see docs/REFACTOR_PLAN.md
    # Phase 5). An empty result correctly falls back to the legacy
    # sole-register path, preserving this test's original assertions.
    coord.api.get_timezone = MagicMock(return_value="America/New_York")
    coord.api.async_get_register_reads = AsyncMock(return_value=[])
    with (
        patch.object(coord, "_backfill_statistics", new=AsyncMock()) as m_back,
        patch.object(coord, "_update_statistics", new=AsyncMock()) as m_upd,
    ):
        recorder = MagicMock()
        recorder.async_add_executor_job = AsyncMock(return_value={})
        with patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ):
            await coord._insert_statistics(["ELECTRIC"], "123 Main", forecast_mock)
        m_back.assert_awaited()
        m_upd.assert_not_awaited()


def test_estimate_billing_cycles() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 1, 1), date(2025, 1, 31), date(2025, 1, 15)
    )
    assert len(cycles) == 1
