"""Final coordinator coverage."""

from datetime import date, datetime

from unittest.mock import AsyncMock, MagicMock, patch

import pytest, zoneinfo

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import (
    DOMAIN,
    CONF_COST_MODE,
    COST_MODE_RATE_8,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    CONF_FIXED_RATE,
)

from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    DominionSCStatisticMetadata,
    _estimate_billing_cycles,
    _find_billing_cycle_for_date,
)


@pytest.fixture
def entry():

    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "t", CONF_PASSWORD: "t", CONF_COST_MODE: COST_MODE_RATE_8},
    )


def test_estimate_backward():

    cycles = _estimate_billing_cycles(
        date(2025, 7, 31), date(2025, 7, 31), date(2025, 7, 1)
    )

    assert len(cycles) >= 1


def test_estimate_forward():

    cycles = _estimate_billing_cycles(
        date(2025, 7, 1), date(2025, 7, 31), date(2025, 7, 1), latest=date(2025, 8, 15)
    )

    assert any(end > date(2025, 7, 31) for _, end in cycles)


def test_find_cycle():

    cycles = [(date(2025, 7, 1), date(2025, 7, 31))]

    assert _find_billing_cycle_for_date(date(2025, 7, 15), cycles) == (
        date(2025, 7, 1),
        date(2025, 7, 31),
    )

    assert _find_billing_cycle_for_date(date(2025, 8, 1), cycles) is None


async def test_push_cost(hass, entry):

    entry.add_to_hass(hass)

    coord = DominionSCCoordinator(hass, entry)

    with patch(
        "custom_components.dominionsc.coordinator.async_add_external_statistics"
    ) as m:
        coord._push_cost_statistics("id", "name", [], "op", 1.0)

        m.assert_called_once()


async def test_backfill(hass, entry):

    entry.add_to_hass(hass)

    coord = DominionSCCoordinator(hass, entry)

    meta = DominionSCStatisticMetadata(
        account="ELECTRIC",
        consumption_id="c",
        cost_id="co",
        name_prefix=MagicMock(),
        unit_class="energy",
        unit="Wh",
    )

    with patch.object(coord, "_process_and_insert_statistics", new=AsyncMock()) as m:
        forecast = MagicMock()
        forecast.start_date = date(2025, 7, 1)
        forecast.end_date = date(2025, 7, 31)
        await coord._backfill_statistics(meta, {}, forecast)

        m.assert_awaited_once()


async def test_aggregate(hass, entry):

    entry.add_to_hass(hass)

    coord = DominionSCCoordinator(hass, entry)

    meta = DominionSCStatisticMetadata(
        account="ELECTRIC",
        consumption_id="c",
        cost_id="co",
        name_prefix=MagicMock(),
        unit_class="energy",
        unit="Wh",
    )

    class R:
        start_time = datetime(2025, 7, 15, 10, 0)

        consumption = 50.0

    forecast = MagicMock()

    forecast.start_date = date(2025, 7, 1)
    forecast.end_date = date(2025, 7, 31)

    cons, cost = coord._aggregate_hourly_data(
        [R()], meta, forecast, date(2025, 7, 1), True, existing_hours=set()
    )

    assert len(cons) == 1


async def test_recalc_lock(hass, entry):

    entry.add_to_hass(hass)

    coord = DominionSCCoordinator(hass, entry)

    with patch.object(
        coord, "_async_recalculate_historic_costs_locked", new=AsyncMock()
    ) as m:
        await coord.async_recalculate_historic_costs(
            date(2025, 7, 1),
            date(2025, 7, 31),
            {"cost_mode": COST_MODE_FIXED, "fixed_rate": 0.2},
        )

        m.assert_awaited_once()


async def test_recalc_none_skip(hass, entry):

    entry.add_to_hass(hass)

    coord = DominionSCCoordinator(hass, entry)

    coord.api = MagicMock()

    coord.api.async_get_accounts = AsyncMock(return_value=({"ELECTRIC"}, "123"))

    coord.api.get_timezone = MagicMock(return_value="US/Eastern")

    with patch(
        "homeassistant.util.dt.async_get_time_zone",
        new=AsyncMock(return_value=zoneinfo.ZoneInfo("US/Eastern")),
    ):
        await coord._async_recalculate_historic_costs_locked(
            date(2025, 7, 1), date(2025, 7, 31), {"cost_mode": COST_MODE_NONE}
        )


async def test_process_usage(hass, entry):
    entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, entry)
