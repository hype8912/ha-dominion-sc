"""Additional branch tests for coordinator edge cases."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.dominionsc.const import (
    CONF_COST_MODE,
    COST_MODE_FIXED,
    COST_MODE_RATE_8,
    DOMAIN,
)
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    DominionSCStatisticMetadata,
)
from dominionsc.exceptions import ApiException
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _make(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "u", CONF_PASSWORD: "p"},
        options=options or {CONF_COST_MODE: COST_MODE_RATE_8},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.dominionsc.coordinator.create_cookie_jar",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.dominionsc.coordinator.async_create_clientsession",
            return_value=MagicMock(),
        ),
    ):
        return DominionSCCoordinator(hass, entry)


def test_listener_and_extended_backfill(hass):
    coordinator = _make(
        hass,
        {
            "extended_backfill": True,
            "extended_cost_backfill": False,
            CONF_COST_MODE: COST_MODE_FIXED,
        },
    )
    assert coordinator._listeners
    assert next(iter(coordinator._listeners)) is not None


async def test_update_success_and_missing_changed(hass):
    coordinator = _make(hass)
    coordinator.api.async_login = AsyncMock()
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC", "GAS"}, "address")
    )
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    coordinator.api.async_get_forecast = AsyncMock(return_value=forecast)
    changed = datetime.now()
    with patch.object(
        coordinator,
        "_insert_statistics",
        new=AsyncMock(return_value={"ELECTRIC": changed}),
    ):
        result = await coordinator._async_update_data()
    assert result.accounts["ELECTRIC"].last_changed == changed
    assert result.accounts["GAS"].last_changed is None


async def test_process_api_error_and_empty_with_changed(hass):
    coordinator = _make(hass)
    coordinator.api.get_timezone = MagicMock(return_value="UTC")
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=3), end_date=date.today()
    )
    m = DominionSCStatisticMetadata(
        "ELECTRIC", "c", "cost", MagicMock(), "energy", "Wh"
    )
    coordinator.api.async_get_usage_reads = AsyncMock(
        side_effect=ApiException("x", "url")
    )
    changed = {"ELECTRIC": datetime.now()}
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        changed["ELECTRIC"],
        changed,
        forecast,
    )
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[])
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        changed["ELECTRIC"],
        changed,
        forecast,
    )
    assert "ELECTRIC" in changed


async def test_update_datetime_and_stale_paths(hass):
    coordinator = _make(hass)
    coordinator.api.get_timezone = MagicMock(return_value="UTC")
    forecast = SimpleNamespace(start_date=date.today() - timedelta(days=2))
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        return_value={"c": [{"start": datetime.now()}]}
    )
    m = DominionSCStatisticMetadata("GAS", "c", None, MagicMock(), "volume", "ft³")
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(
            coordinator, "_process_and_insert_statistics", new=AsyncMock()
        ) as process,
    ):
        await coordinator._update_statistics(
            m, {"c": [{"start": datetime.now(), "sum": None}]}, {}, {}, forecast
        )
    process.assert_awaited()


def test_empty_aggregate(hass):
    coordinator = _make(hass)
    forecast = SimpleNamespace(start_date=date.today(), end_date=date.today())
    assert coordinator._aggregate_hourly_data(
        [], MagicMock(), forecast, date.today(), False
    ) == ({}, {})
