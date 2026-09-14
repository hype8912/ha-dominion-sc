"""Tests for dominionsc sensor."""

from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import (
    CONF_GAS_COST_MODE,
    COST_MODE_NONE,
    COST_MODE_RATE_32S,
    DOMAIN,
)
from custom_components.dominionsc.sensor import (
    ACCOUNT_SENSORS,
    BILLING_SENSORS,
    DominionSCSensor,
    async_setup_entry,
)

# Expected keys for each sensor group
_EXPECTED_BILLING_KEYS = {
    "cost_to_date",
    "forecasted_cost",
    "typical_cost",
    "start_date",
    "end_date",
    "last_updated",
}
_EXPECTED_ACCOUNT_KEY = "last_changed"


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
    coord.data.gas_cost_to_date = None
    return coord


async def test_async_setup_entry_with_forecast(
    hass: HomeAssistant, mock_coordinator: MagicMock
) -> None:
    """1 account sensor + 6 billing sensors = 7 entities registered."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = mock_coordinator
    mock_add = MagicMock()

    await async_setup_entry(hass, entry, mock_add)

    mock_add.assert_called_once()
    entities = mock_add.call_args[0][0]
    # Exact count: 1 account sensor × 1 account + 6 billing sensors = 7
    assert len(entities) == 7
    keys = [s.entity_description.key for s in entities]
    assert _EXPECTED_ACCOUNT_KEY in keys
    for billing_key in _EXPECTED_BILLING_KEYS:
        assert billing_key in keys, f"Billing key '{billing_key}' not found in {keys}"


async def test_async_setup_entry_no_forecast(hass: HomeAssistant) -> None:
    """No forecast → only account sensor registered; no billing sensors."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    coord.data.accounts = {"electric": account_data}
    coord.data.forecast = None
    coord.data.gas_cost_to_date = None

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = coord
    mock_add = MagicMock()

    await async_setup_entry(hass, entry, mock_add)

    mock_add.assert_called_once()
    entities = mock_add.call_args[0][0]
    # Exact count: 1 account sensor, no billing sensors
    assert len(entities) == 1
    keys = [s.entity_description.key for s in entities]
    assert keys == [_EXPECTED_ACCOUNT_KEY]
    # Confirm no billing sensor is present
    for billing_key in _EXPECTED_BILLING_KEYS:
        assert billing_key not in keys


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
    """BILLING_SENSORS[0] (cost_to_date) returns coordinator forecast value."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = MagicMock()
    coord.data.forecast.cost_to_date = 99.99
    desc = BILLING_SENSORS[0]
    device = MagicMock()

    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")

    assert sensor.account == "billing"
    assert sensor.native_value == 99.99


# ---------------------------------------------------------------------------
# Parameterized tests: all 6 billing sensors and their None fallback
# ---------------------------------------------------------------------------

_BILLING_SENSOR_CASES = [
    ("cost_to_date", "cost_to_date", 10.5),
    ("forecasted_cost", "forecasted_cost", 20.0),
    ("typical_cost", "typical_cost", 15.0),
    ("start_date", "start_date", "2025-01-01"),
    ("end_date", "end_date", "2025-02-01"),
    ("last_updated", "last_updated", "2025-01-15"),
]


@pytest.mark.parametrize(
    ("sensor_key", "forecast_attr", "expected_value"),
    _BILLING_SENSOR_CASES,
    ids=[c[0] for c in _BILLING_SENSOR_CASES],
)
def test_billing_sensor_returns_forecast_value(
    sensor_key: str, forecast_attr: str, expected_value
) -> None:
    """Each billing sensor extracts its value from coordinator data correctly."""
    # Arrange
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = MagicMock()
    setattr(coord.data.forecast, forecast_attr, expected_value)
    # last_updated reads from data directly, not from forecast
    coord.data.last_updated = expected_value

    desc = next(s for s in BILLING_SENSORS if s.key == sensor_key)
    device = MagicMock()

    # Act
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")
    value = sensor.native_value

    # Assert
    assert value == expected_value, (
        f"Sensor '{sensor_key}' returned {value!r}, expected {expected_value!r}"
    )


@pytest.mark.parametrize(
    "sensor_key",
    [c[0] for c in _BILLING_SENSOR_CASES if c[0] != "last_updated"],
    ids=[c[0] for c in _BILLING_SENSOR_CASES if c[0] != "last_updated"],
)
def test_billing_sensor_none_when_forecast_is_none(sensor_key: str) -> None:
    """Each forecast-backed billing sensor returns None when forecast is None."""
    # Arrange
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = None
    desc = next(s for s in BILLING_SENSORS if s.key == sensor_key)
    device = MagicMock()

    # Act
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")
    value = sensor.native_value

    # Assert
    assert value is None, (
        f"Sensor '{sensor_key}' should return None when forecast is None, got {value!r}"
    )


async def test_async_setup_entry_with_gas_cost(hass: HomeAssistant) -> None:
    """Gas cost sensor is registered when GAS account exists and gas cost mode is set."""
    from custom_components.dominionsc.sensor import GAS_COST_SENSOR

    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    gas_data = MagicMock()
    gas_data.last_changed = "2025-01-01"
    coord.data.accounts = {"ELECTRIC": account_data, "GAS": gas_data}
    coord.data.forecast = None
    coord.data.gas_cost_to_date = None  # May be None on first install — sensor still registered

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_GAS_COST_MODE: COST_MODE_RATE_32S},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = coord
    mock_add = MagicMock()

    await async_setup_entry(hass, entry, mock_add)

    entities = mock_add.call_args[0][0]
    keys = [s.entity_description.key for s in entities]
    assert "gas_cost_to_date" in keys
    assert GAS_COST_SENSOR.key == "gas_cost_to_date"


async def test_async_setup_entry_gas_account_no_rate_plan(hass: HomeAssistant) -> None:
    """Gas cost sensor is NOT registered when GAS account exists but cost mode is none."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    gas_data = MagicMock()
    gas_data.last_changed = "2025-01-01"
    coord.data.accounts = {"ELECTRIC": account_data, "GAS": gas_data}
    coord.data.forecast = None
    coord.data.gas_cost_to_date = None

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_GAS_COST_MODE: COST_MODE_NONE},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = coord
    mock_add = MagicMock()

    await async_setup_entry(hass, entry, mock_add)

    entities = mock_add.call_args[0][0]
    keys = [s.entity_description.key for s in entities]
    assert _EXPECTED_ACCOUNT_KEY in keys
    assert "gas_cost_to_date" not in keys


async def test_async_setup_entry_no_gas_account(hass: HomeAssistant) -> None:
    """Gas cost sensor is NOT registered when there is no GAS account, even if options set."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.service_addr_account_no = "123-456"
    account_data = MagicMock()
    account_data.last_changed = "2025-01-01"
    coord.data.accounts = {"ELECTRIC": account_data}
    coord.data.forecast = None
    coord.data.gas_cost_to_date = None

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={CONF_GAS_COST_MODE: COST_MODE_RATE_32S},
    )
    entry.add_to_hass(hass)
    entry.runtime_data = coord
    mock_add = MagicMock()

    await async_setup_entry(hass, entry, mock_add)

    entities = mock_add.call_args[0][0]
    keys = [s.entity_description.key for s in entities]
    assert _EXPECTED_ACCOUNT_KEY in keys
    assert "gas_cost_to_date" not in keys


def test_gas_cost_sensor_value_fn() -> None:
    """GAS_COST_SENSOR.value_fn extracts gas_cost_to_date from DominionSCData."""
    from custom_components.dominionsc.sensor import GAS_COST_SENSOR

    # Arrange
    data = MagicMock()
    data.gas_cost_to_date = 123.45

    # Act
    value = GAS_COST_SENSOR.value_fn(data)

    # Assert
    assert value == 123.45


def test_gas_cost_sensor_value_fn_none() -> None:
    """GAS_COST_SENSOR.value_fn returns None when gas_cost_to_date is None."""
    from custom_components.dominionsc.sensor import GAS_COST_SENSOR

    # Arrange
    data = MagicMock()
    data.gas_cost_to_date = None

    # Act
    value = GAS_COST_SENSOR.value_fn(data)

    # Assert
    assert value is None


# ---------------------------------------------------------------------------
# last_reset property tests
# ---------------------------------------------------------------------------


def test_cost_to_date_last_reset_returns_cycle_start() -> None:
    """cost_to_date.last_reset returns UTC midnight of the billing cycle start date."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = MagicMock()
    coord.data.forecast.start_date = date(2025, 3, 1)
    coord.data.forecast.cost_to_date = 42.0

    desc = next(s for s in BILLING_SENSORS if s.key == "cost_to_date")
    device = MagicMock()
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")

    expected = datetime(2025, 3, 1, 0, 0, 0, tzinfo=dt_timezone.utc)
    assert sensor.last_reset == expected


def test_cost_to_date_last_reset_none_when_no_forecast() -> None:
    """cost_to_date.last_reset returns None when the forecast is not available."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = None

    desc = next(s for s in BILLING_SENSORS if s.key == "cost_to_date")
    device = MagicMock()
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")

    assert sensor.last_reset is None


@pytest.mark.parametrize(
    "sensor_key",
    ["forecasted_cost", "typical_cost", "start_date", "end_date", "last_updated"],
)
def test_last_reset_is_none_for_non_total_sensors(sensor_key: str) -> None:
    """Sensors without last_reset_fn always return None for last_reset."""
    coord = MagicMock()
    coord.data = MagicMock()
    coord.data.forecast = MagicMock()

    desc = next(s for s in BILLING_SENSORS if s.key == sensor_key)
    device = MagicMock()
    sensor = DominionSCSensor(coord, desc, "billing", device, "dev_1")

    assert sensor.last_reset is None


# ---------------------------------------------------------------------------
# State class assertions
# ---------------------------------------------------------------------------


def test_forecasted_cost_state_class_is_measurement() -> None:
    """forecasted_cost uses MEASUREMENT not TOTAL — it is a point-in-time estimate."""
    desc = next(s for s in BILLING_SENSORS if s.key == "forecasted_cost")
    assert desc.state_class == SensorStateClass.MEASUREMENT


def test_typical_cost_state_class_is_measurement() -> None:
    """typical_cost uses MEASUREMENT not TOTAL — it is a point-in-time estimate."""
    desc = next(s for s in BILLING_SENSORS if s.key == "typical_cost")
    assert desc.state_class == SensorStateClass.MEASUREMENT


def test_cost_to_date_state_class_is_total() -> None:
    """cost_to_date uses TOTAL — it is a running sum that resets each billing cycle."""
    desc = next(s for s in BILLING_SENSORS if s.key == "cost_to_date")
    assert desc.state_class == SensorStateClass.TOTAL
