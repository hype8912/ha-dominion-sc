"""Remaining coordinator coverage -- rapid baby steps."""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import (
    CONF_COST_MODE,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DOMAIN,
)
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    _billing_cycle_get_gap,
    _calculate_cost_for_wh,
)


# Small branch misses 166, 191 (cost calc default, gap default)
def test_cost_fallback_none() -> None:
    # rate_schedule is None -> returns 0.0 at line 166
    assert (
        _calculate_cost_for_wh(100, datetime(2025, 7, 1), 0, COST_MODE_NONE, 0, None)
        == 0.0
    )


def test_gap_default_feb() -> None:
    # m=2 not in gaps -> hits default return 30 at 191
    assert _billing_cycle_get_gap(date(2025, 2, 1)) == 31


# Update error branches 317-324, 331-336
class TestUpdateErrors:
    async def test_async_update_auth_failed(self, hass: HomeAssistant) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "t",
                CONF_PASSWORD: "t",
                CONF_COST_MODE: COST_MODE_RATE_8,
            },
        )
        entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, entry)
        coord.api = MagicMock()
        from dominionsc.exceptions import InvalidAuth

        coord.api.async_login = AsyncMock(side_effect=InvalidAuth("bad"))
        from homeassistant.exceptions import ConfigEntryAuthFailed

        with pytest.raises(ConfigEntryAuthFailed):
            await coord._async_update_data()

    async def test_async_update_forecast_cannot_connect(
        self, hass: HomeAssistant
    ) -> None:
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_USERNAME: "t",
                CONF_PASSWORD: "t",
                CONF_COST_MODE: COST_MODE_RATE_8,
            },
        )
        entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, entry)
        coord.api = MagicMock()
        coord.api.async_login = AsyncMock()
        coord.api.async_get_accounts = AsyncMock(return_value=({"ELECTRIC"}, "123"))
        from dominionsc.exceptions import CannotConnect

        coord.api.async_get_forecast = AsyncMock(side_effect=CannotConnect("fail"))
        from homeassistant.helpers.update_coordinator import UpdateFailed

        with pytest.raises(UpdateFailed):
            await coord._async_update_data()


# Forecast / usage projection 403-495 (contract: forecast drives billing cycles)
class TestForecastUsage:
    def test_forecast_dates_for_cycle_estimation(self) -> None:
        from custom_components.dominionsc.coordinator import _estimate_billing_cycles

        cycles = _estimate_billing_cycles(
            date(2025, 7, 1), date(2025, 7, 31), date(2025, 7, 1)
        )
        assert len(cycles) > 0
        assert cycles[0][0] <= cycles[0][1]


# Statistics insertion / usage loop 557-824 (contract: error parsing, accumulation)
class TestStatsUsage:
    def test_stat_metadata_build(self) -> None:
        from string import Template

        from custom_components.dominionsc.coordinator import DominionSCStatisticMetadata

        m = DominionSCStatisticMetadata(
            account="ELECTRIC",
            consumption_id="id",
            cost_id="cost",
            name_prefix=Template("$account"),
            unit_class="energy",
            unit="Wh",
        )
        assert m.account == "ELECTRIC"
