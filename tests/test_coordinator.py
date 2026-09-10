"""
Unit tests for the DominionSC coordinator and its pure-function helpers.

Consolidated from: test_coordinator_chunk1/2/3, test_coordinator_complete,
test_coordinator_final_coverage, test_coordinator_full, test_coordinator_remaining,
test_coordinator_additional, and test_coordinator_coverage (Phase 4, 2026-09-09).

The pure helpers (_build_statistic_ids, _resolve_cost_config, etc.) now live in
their own modules but are re-exported from coordinator for backward compat; tests
import them via the coordinator re-export so this file tests both paths.

Organisation:
  1. Fixtures / shared helpers
  2. Pure helpers: statistic-ID construction and cost-config resolution
  3. Pure helpers: cost calculation and billing-cycle gap
  4. Pure helpers: billing-cycle estimation and lookup
  5. Coordinator lifecycle: initialisation and _async_update_data
  6. Statistics pipeline: _insert_statistics
  7. Statistics pipeline: _backfill_statistics and _update_statistics
  8. Statistics pipeline: _aggregate_hourly_data
  9. Statistics pipeline: _process_and_insert_statistics and _push_cost_statistics
  10. Historic cost recalculation
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dominionsc.exceptions import ApiException, CannotConnect, InvalidAuth, MfaChallenge
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import (
    CONF_COST_MODE,
    CONF_EXTENDED_BACKFILL,
    CONF_EXTENDED_COST_BACKFILL,
    CONF_FIXED_RATE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DOMAIN,
)
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    DominionSCData,
    DominionSCStatisticMetadata,
    _billing_cycle_get_gap,
    _build_statistic_ids,
    _calculate_cost_for_wh,
    _estimate_billing_cycles,
    _find_billing_cycle_for_date,
    _resolve_cost_config,
)
from dominionsc import RATE_8


# ---------------------------------------------------------------------------
# 1. Fixtures / shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user", CONF_PASSWORD: "password"},
        options={CONF_COST_MODE: COST_MODE_RATE_8},
    )


@pytest.fixture
def coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> DominionSCCoordinator:
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
        coord = DominionSCCoordinator(hass, entry)
        coord.api = MagicMock()
        return coord


def metadata(
    account: str = "ELECTRIC", cost_id: str | None = "cost"
) -> DominionSCStatisticMetadata:
    from string import Template

    return DominionSCStatisticMetadata(
        account, "consumption", cost_id, Template("$stat_type"), "energy", "Wh"
    )


# ---------------------------------------------------------------------------
# 2. Pure helpers: statistic-ID construction and cost-config resolution
# ---------------------------------------------------------------------------


def test_build_statistic_ids_electric() -> None:
    cid, cost_id, prefix = _build_statistic_ids("123 Oak St", "ELECTRIC")
    assert cid == f"{DOMAIN}:123_oak_st_electric_energy_consumption"
    assert cost_id == f"{DOMAIN}:123_oak_st_electric_energy_cost"
    from string import Template

    assert isinstance(prefix, Template)
    assert "Electric" in prefix.template


def test_build_statistic_ids_gas() -> None:
    cid, cost_id, _prefix = _build_statistic_ids("456-789", "GAS")
    assert cid == f"{DOMAIN}:456_789_gas_energy_consumption"
    assert cost_id is None


def test_resolve_cost_config_default() -> None:
    mode, _rate, plan = _resolve_cost_config({})
    assert mode == COST_MODE_RATE_8
    assert plan is RATE_8


def test_resolve_cost_config_fixed() -> None:
    mode, rate, sched = _resolve_cost_config(
        {"cost_mode": COST_MODE_FIXED, "fixed_rate": 0.15}
    )
    assert mode == COST_MODE_FIXED
    assert rate == 0.15
    assert sched is None


# ---------------------------------------------------------------------------
# 3. Pure helpers: cost calculation and billing-cycle gap
# ---------------------------------------------------------------------------


def test_cost_none() -> None:
    assert (
        _calculate_cost_for_wh(100, datetime(2025, 7, 1), 0, COST_MODE_NONE, 0.0, None)
        == 0.0
    )


def test_cost_fixed() -> None:
    assert (
        _calculate_cost_for_wh(1000, datetime(2025, 7, 1), 0, COST_MODE_FIXED, 0.15, None)
        == 0.15
    )


def test_cost_tiered_before_effective() -> None:
    # RATE_8.effective_from is 2026-07-01; intervals before that return $0
    assert (
        _calculate_cost_for_wh(100, datetime(2026, 6, 30), 0, COST_MODE_RATE_8, 0, RATE_8)
        == 0.0
    )


def test_cost_tiered_after_effective() -> None:
    assert (
        _calculate_cost_for_wh(100, datetime(2026, 8, 1), 0, COST_MODE_RATE_8, 0, RATE_8)
        > 0
    )


def test_cost_tiered_all_over_boundary() -> None:
    # cumulative_before well above the 800 kWh (800_000 Wh) boundary → upper tier
    # RATE_8 summer over-800 rate: $0.17442/kWh = 0.00017442 $/Wh
    result = _calculate_cost_for_wh(
        1000, datetime(2026, 8, 1), 900_000, COST_MODE_RATE_8, 0, RATE_8
    )
    assert abs(result - 1000 * 0.17442 / 1000) < 1e-9


def test_cost_tiered_straddles_boundary() -> None:
    # cumulative_before=799_000 Wh, interval=2000 Wh → crosses 800_000 Wh boundary
    # 1000 Wh at lower tier ($0.15878/kWh), 1000 Wh at upper tier ($0.17442/kWh)
    result = _calculate_cost_for_wh(
        2000, datetime(2026, 8, 1), 799_000, COST_MODE_RATE_8, 0, RATE_8
    )
    expected = 1000 * 0.15878 / 1000 + 1000 * 0.17442 / 1000
    assert abs(result - expected) < 1e-9


def test_cost_tiered_no_tiered_charge_returns_zero() -> None:
    # A RatePlan with no TieredUsageCharge hits the defensive return 0.0 path.
    from dominionsc import RATE_2

    from custom_components.dominionsc.cost import _calculate_tiered_cost

    result = _calculate_tiered_cost(1000, datetime(2026, 8, 1), 0, RATE_2)
    assert result == 0.0


def test_cost_unknown_mode_returns_zero() -> None:
    assert _calculate_cost_for_wh(10, datetime(2025, 1, 1), 0, "unknown", 0, None) == 0


def test_cost_rate8_with_no_schedule_returns_zero() -> None:
    assert (
        _calculate_cost_for_wh(10, datetime(2025, 1, 1), 0, COST_MODE_RATE_8, 0, None)
        == 0
    )


def test_billing_gap_january() -> None:
    assert _billing_cycle_get_gap(date(2025, 1, 1)) == 30


def test_billing_gap_february() -> None:
    assert _billing_cycle_get_gap(date(2025, 2, 1)) == 31


def test_billing_gap_april_leap_vs_non_leap() -> None:
    assert _billing_cycle_get_gap(date(2024, 4, 1)) == 31  # leap year
    assert _billing_cycle_get_gap(date(2025, 4, 1)) == 30  # non-leap year


def test_billing_gap_june_leap_vs_non_leap() -> None:
    assert _billing_cycle_get_gap(date(2024, 6, 1)) == 30  # leap year
    assert _billing_cycle_get_gap(date(2025, 6, 1)) == 31  # non-leap year


def test_billing_gap_fallback_branch() -> None:
    # month=13 exercises the fallback path (returns 30)
    assert _billing_cycle_get_gap(SimpleNamespace(month=13, year=2025)) == 30


# ---------------------------------------------------------------------------
# 4. Pure helpers: billing-cycle estimation and lookup
# ---------------------------------------------------------------------------


def test_estimate_billing_cycles_expansion() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 7, 1), date(2025, 7, 31), date(2025, 5, 1), date(2025, 9, 1)
    )
    assert cycles[0][0] <= date(2025, 5, 1)
    assert cycles[-1][1] >= date(2025, 9, 1)


def test_estimate_billing_cycles_backward() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 7, 31), date(2025, 7, 31), date(2025, 7, 1)
    )
    assert len(cycles) >= 1


def test_estimate_billing_cycles_forward() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 7, 1), date(2025, 7, 31), date(2025, 7, 1), latest=date(2025, 8, 15)
    )
    assert any(end > date(2025, 7, 31) for _, end in cycles)


def test_estimate_billing_cycles_single_month() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 1, 1), date(2025, 1, 31), date(2025, 1, 15)
    )
    assert len(cycles) == 1


def test_find_billing_cycle_hit() -> None:
    cycles = [(date(2025, 7, 1), date(2025, 7, 31))]
    assert _find_billing_cycle_for_date(date(2025, 7, 15), cycles) == (
        date(2025, 7, 1),
        date(2025, 7, 31),
    )


def test_find_billing_cycle_miss() -> None:
    cycles = _estimate_billing_cycles(
        date(2025, 7, 1), date(2025, 7, 31), date(2025, 5, 1), date(2025, 9, 1)
    )
    assert _find_billing_cycle_for_date(date(2030, 1, 1), cycles) is None


# ---------------------------------------------------------------------------
# 5. Coordinator lifecycle: initialisation and _async_update_data
# ---------------------------------------------------------------------------


async def test_coordinator_init(hass: HomeAssistant, entry: MockConfigEntry) -> None:
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
        coord = DominionSCCoordinator(hass, entry)
    assert coord.api is not None
    assert len(coord._listeners) > 0


def test_coordinator_init_with_extended_backfill_options(
    hass: HomeAssistant,
) -> None:
    """Coordinator registers listener even when extended backfill options are set."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "u", CONF_PASSWORD: "p"},
        options={
            "extended_backfill": True,
            "extended_cost_backfill": False,
            CONF_COST_MODE: COST_MODE_FIXED,
        },
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
        coord = DominionSCCoordinator(hass, entry)
    assert coord._listeners
    assert next(iter(coord._listeners)) is not None


async def test_async_update_data_happy_path(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, entry)
    coord.api = MagicMock()
    coord.api.async_login = AsyncMock()
    coord.api.async_get_accounts = AsyncMock(return_value=({"ELECTRIC"}, "123 Main"))
    forecast_mock = MagicMock()
    forecast_mock.start_date = datetime(2025, 7, 23).date()
    forecast_mock.end_date = datetime(2025, 7, 31).date()
    coord.api.async_get_forecast = AsyncMock(return_value=forecast_mock)
    with patch.object(
        coord,
        "_insert_statistics",
        new=AsyncMock(return_value={"ELECTRIC": datetime(2025, 7, 1)}),
    ):
        data = await coord._async_update_data()
    assert isinstance(data, DominionSCData)
    assert "ELECTRIC" in data.accounts


async def test_async_update_data_multi_account_missing_last_changed(
    hass: HomeAssistant,
) -> None:
    """GAS account gets last_changed=None when _insert_statistics only returns ELECTRIC."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "u", CONF_PASSWORD: "p"},
        options={CONF_COST_MODE: COST_MODE_RATE_8},
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
        coord = DominionSCCoordinator(hass, entry)
    coord.api.async_login = AsyncMock()
    coord.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC", "GAS"}, "address")
    )
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    coord.api.async_get_forecast = AsyncMock(return_value=forecast)
    changed = datetime.now()
    with patch.object(
        coord,
        "_insert_statistics",
        new=AsyncMock(return_value={"ELECTRIC": changed}),
    ):
        result = await coord._async_update_data()
    assert result.accounts["ELECTRIC"].last_changed == changed
    assert result.accounts["GAS"].last_changed is None


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (InvalidAuth("bad"), ConfigEntryAuthFailed),
        (MfaChallenge("mfa", MagicMock()), ConfigEntryAuthFailed),
        (CannotConnect("down"), UpdateFailed),
    ],
)
async def test_update_login_errors(
    coordinator: DominionSCCoordinator, exc: Exception, expected: type[Exception]
) -> None:
    coordinator.api.async_login = AsyncMock(side_effect=exc)
    with pytest.raises(expected):
        await coordinator._async_update_data()


async def test_update_login_api_exception_reraises(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.async_login = AsyncMock(
        side_effect=ApiException("err", "https://example.test")
    )
    with pytest.raises(ApiException):
        await coordinator._async_update_data()


async def test_update_api_errors(coordinator: DominionSCCoordinator) -> None:
    coordinator.api.async_login = AsyncMock()
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "address")
    )
    coordinator.api.async_get_forecast = AsyncMock(side_effect=CannotConnect("down"))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    coordinator.api.async_get_forecast = AsyncMock(
        side_effect=ApiException("bad", "url")
    )
    with pytest.raises(ApiException):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# 6. Statistics pipeline: _insert_statistics
# ---------------------------------------------------------------------------


async def test_insert_existing_stat_runs_update(
    coordinator: DominionSCCoordinator,
) -> None:
    """When the recorder already has data, _update_statistics is called."""
    recorder = MagicMock()

    async def executor(_func, _hass, _count, statistic_id, *_args):
        return {statistic_id: [{"start": datetime.now(), "sum": 1}]}

    recorder.async_add_executor_job = executor
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=3), end_date=date.today()
    )
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(coordinator, "_update_statistics", new=AsyncMock()) as update,
    ):
        result = await coordinator._insert_statistics(["ELECTRIC"], "address", forecast)
    assert result == {}
    update.assert_awaited_once()


async def test_insert_already_started_backfill_is_skipped(
    coordinator: DominionSCCoordinator,
) -> None:
    """When a backfill is already in flight, the second poll skips the account."""
    coordinator._backfill_initiated["GAS"] = True
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={})
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=3), end_date=date.today()
    )
    with patch(
        "custom_components.dominionsc.coordinator.get_instance", return_value=recorder
    ):
        assert (
            await coordinator._insert_statistics(["GAS"], "address", forecast) == {}
        )


async def test_insert_statistics_backfill_path(
    hass: HomeAssistant,
) -> None:
    """Recorder returns empty → _backfill_statistics is awaited; _update_statistics is not.

    Phase 5: register discovery is called once for a never-backfilled account.
    An empty discovery result correctly falls back to the legacy sole-register path,
    keeping the original assertions intact.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "t", CONF_PASSWORD: "t", CONF_COST_MODE: COST_MODE_FIXED},
    )
    entry.add_to_hass(hass)
    coord = DominionSCCoordinator(hass, entry)
    coord.api = MagicMock()
    coord.api.get_timezone = MagicMock(return_value="America/New_York")
    coord.api.async_get_register_reads = AsyncMock(return_value=[])
    forecast_mock = MagicMock()
    forecast_mock.start_date = date(2025, 7, 1)
    forecast_mock.end_date = date(2025, 7, 31)
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


# ---------------------------------------------------------------------------
# 7. Statistics pipeline: _backfill_statistics and _update_statistics
# ---------------------------------------------------------------------------


async def test_backfill_default_options(coordinator: DominionSCCoordinator) -> None:
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    with patch.object(
        coordinator, "_process_and_insert_statistics", new=AsyncMock()
    ) as process:
        await coordinator._backfill_statistics(metadata(), {}, forecast)
    process.assert_awaited_once()


async def test_backfill_extended_backfill_without_extended_cost(
    coordinator: DominionSCCoordinator,
) -> None:
    """Extended backfill + no extended cost → cost_start_date set to billing cycle start."""
    coordinator.config_entry._options = {
        CONF_EXTENDED_BACKFILL: True,
        CONF_EXTENDED_COST_BACKFILL: False,
    }
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    with patch.object(
        coordinator, "_process_and_insert_statistics", new=AsyncMock()
    ) as process:
        await coordinator._backfill_statistics(metadata(), {}, forecast)
    assert process.await_args.kwargs["cost_start_date"] is None


async def test_backfill_extended_cost_window(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Extended backfill=True + extended_cost=False → cost_start_date = billing cycle start."""
    entry.add_to_hass(hass)
    object.__setattr__(
        entry,
        "options",
        {
            CONF_COST_MODE: COST_MODE_FIXED,
            CONF_EXTENDED_BACKFILL: True,
            CONF_EXTENDED_COST_BACKFILL: False,
        },
    )
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
        coord = DominionSCCoordinator(hass, entry)
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=5), end_date=date.today()
    )
    with patch.object(
        coord, "_process_and_insert_statistics", new=AsyncMock()
    ) as process:
        await coord._backfill_statistics(metadata(), {}, forecast)
    assert process.await_args.kwargs["cost_start_date"] == forecast.start_date


async def test_update_statistics_empty_last_stat_returns_early(
    coordinator: DominionSCCoordinator,
) -> None:
    m = metadata()
    await coordinator._update_statistics(
        m, {}, {}, {}, SimpleNamespace(start_date=date.today() - timedelta(days=2))
    )


async def test_update_statistics_stale_stat_clamps_start_date(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        return_value={"consumption": [{"start": 0}]}
    )
    forecast = SimpleNamespace(start_date=date.today() - timedelta(days=2))
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
            metadata(),
            {"consumption": [{"start": 0, "sum": 1}]},
            {"cost": [{"sum": 2}]},
            {},
            forecast,
        )
    assert process.await_args.kwargs["start_date"] == forecast.start_date


async def test_update_statistics_current_window_early_return(
    coordinator: DominionSCCoordinator,
) -> None:
    """When all dates are covered and there are no new days, skip the API call."""
    coordinator.api.get_timezone.return_value = "UTC"
    data_date = date.today() - timedelta(days=1)
    lookback_start = data_date - timedelta(days=5)
    last_dt = datetime.combine(data_date, datetime.min.time()).replace(
        hour=12, tzinfo=__import__("datetime").timezone.utc
    )
    rows = [
        {
            "start": datetime.combine(
                lookback_start + timedelta(days=i), datetime.min.time()
            ).timestamp()
        }
        for i in range(6)
    ]
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": rows})
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(
            coordinator, "_process_and_insert_statistics", new=AsyncMock()
        ) as process,
        patch(
            "custom_components.dominionsc.coordinator.dt_util.get_default_time_zone",
            return_value=__import__("datetime").timezone.utc,
        ),
    ):
        changed = {}
        await coordinator._update_statistics(
            metadata(cost_id=None),
            {"consumption": [{"start": last_dt, "sum": 5}]},
            {},
            changed,
            SimpleNamespace(start_date=date.today() - timedelta(days=20)),
        )
    process.assert_not_awaited()
    assert changed["ELECTRIC"] == last_dt


async def test_update_statistics_dispatches_processing(
    coordinator: DominionSCCoordinator,
) -> None:
    """New days available → _process_and_insert_statistics is awaited."""
    coordinator.api.get_timezone.return_value = "UTC"
    now = datetime.now().replace(tzinfo=None)
    last = {"consumption": [{"start": now.timestamp(), "sum": 12.5}]}
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": []})
    forecast = SimpleNamespace(start_date=date.today() - timedelta(days=10))
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
            metadata(), last, {"cost": [{"sum": "bad"}]}, {}, forecast
        )
    process.assert_awaited_once()


async def test_update_statistics_datetime_start_dispatches(
    coordinator: DominionSCCoordinator,
) -> None:
    """Datetime-typed start field (not a timestamp float) is handled correctly."""
    coordinator.api.get_timezone.return_value = "UTC"
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


async def test_update_statistics_new_days_without_missing_dates(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    data_date = date.today() - timedelta(days=1)
    lookback_start = data_date - timedelta(days=5)
    rows = [
        {
            "start": datetime.combine(
                lookback_start + timedelta(days=i), datetime.min.time()
            )
        }
        for i in range(6)
    ]
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": rows})
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
            metadata(cost_id=None),
            {
                "consumption": [
                    {
                        "start": datetime.combine(
                            lookback_start - timedelta(days=1), datetime.min.time()
                        ),
                        "sum": 1,
                    }
                ]
            },
            {},
            {},
            SimpleNamespace(start_date=lookback_start - timedelta(days=1)),
        )
    process.assert_awaited_once()


async def test_update_statistics_all_expected_dates_but_new_days(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    data_date = date.today() - timedelta(days=1)
    start_date = data_date - timedelta(days=5)
    rows = [
        {
            "start": datetime.combine(
                start_date - timedelta(days=1) + timedelta(days=i), datetime.min.time()
            )
        }
        for i in range(8)
    ]
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": rows})
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
            metadata(cost_id=None),
            {
                "consumption": [
                    {
                        "start": datetime.combine(
                            start_date - timedelta(days=1), datetime.min.time()
                        ),
                        "sum": 1,
                    }
                ]
            },
            {},
            {},
            SimpleNamespace(start_date=start_date - timedelta(days=1)),
        )
    process.assert_awaited_once()


async def test_update_statistics_datetime_row_and_new_day(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    data_date = date.today() - timedelta(days=1)
    old_date = data_date - timedelta(days=2)
    rows = [
        {"start": datetime.combine(old_date, datetime.min.time())},
        {"start": datetime.combine(data_date, datetime.min.time())},
    ]
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": rows})
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
            metadata(cost_id=None),
            {
                "consumption": [
                    {"start": datetime.combine(old_date, datetime.min.time()), "sum": 2}
                ]
            },
            {},
            {},
            SimpleNamespace(start_date=old_date - timedelta(days=1)),
        )
    process.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. Statistics pipeline: _aggregate_hourly_data
# ---------------------------------------------------------------------------


def test_aggregate_hourly_data_all_paths(coordinator: DominionSCCoordinator) -> None:
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )

    def read(when, consumption):
        return SimpleNamespace(
            start_time=when, end_time=when + timedelta(hours=1), consumption=consumption
        )

    first = datetime.now().replace(minute=15, second=0, microsecond=0)
    rows = [read(first, 100), read(first + timedelta(minutes=20), 50)]

    # Default rate-8 path: produces consumption and cost
    cons, costs = coordinator._aggregate_hourly_data(
        rows, metadata(), forecast, first.date(), True
    )
    assert len(cons) == 1
    assert costs

    # Fixed rate with future cost_start_date: consumption produced, cost zeroed
    coordinator.config_entry._options = {
        CONF_COST_MODE: COST_MODE_FIXED,
        CONF_FIXED_RATE: 0.2,
    }
    cons, costs = coordinator._aggregate_hourly_data(
        rows,
        metadata(),
        forecast,
        first.date(),
        True,
        cost_start_date=first.date() + timedelta(days=1),
    )
    assert cons
    assert not any(costs.values())

    # Hour already in recorder → skipped, returns empty dicts
    existing = {first.replace(minute=0)}
    assert coordinator._aggregate_hourly_data(
        rows, metadata(), forecast, first.date(), True, existing_hours=existing
    ) == ({}, {})

    # GAS account → no cost dict
    assert (
        coordinator._aggregate_hourly_data(
            rows, metadata("GAS", None), forecast, first.date(), False
        )[1]
        == {}
    )


def test_aggregate_hourly_data_empty_reads(coordinator: DominionSCCoordinator) -> None:
    forecast = SimpleNamespace(start_date=date.today(), end_date=date.today())
    assert coordinator._aggregate_hourly_data(
        [], MagicMock(), forecast, date.today(), False
    ) == ({}, {})


def test_aggregate_fixed_cost_path_with_rows(
    coordinator: DominionSCCoordinator,
) -> None:
    object.__setattr__(
        coordinator.config_entry,
        "options",
        {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2},
    )
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    forecast = SimpleNamespace(start_date=when.date(), end_date=when.date())
    read = SimpleNamespace(start_time=when, end_time=when, consumption=100)
    consumption, costs = coordinator._aggregate_hourly_data(
        [read], metadata(), forecast, when.date(), True
    )
    assert consumption
    assert costs


def test_aggregate_fixed_empty_and_tiered_existing(
    coordinator: DominionSCCoordinator,
) -> None:
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=5), end_date=date.today()
    )
    object.__setattr__(
        coordinator.config_entry,
        "options",
        {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2},
    )
    assert coordinator._aggregate_hourly_data(
        [], metadata(), forecast, date.today(), True
    ) == ({}, {})
    object.__setattr__(
        coordinator.config_entry, "options", {CONF_COST_MODE: COST_MODE_RATE_8}
    )
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    read = SimpleNamespace(start_time=when, end_time=when, consumption=10)
    assert (
        coordinator._aggregate_hourly_data(
            [read],
            metadata(),
            forecast,
            when.date(),
            True,
            existing_hours={when.replace(minute=0)},
        )[0]
        == {}
    )


# ---------------------------------------------------------------------------
# 9. Statistics pipeline: _process_and_insert_statistics and _push_cost_statistics
# ---------------------------------------------------------------------------


async def test_process_cannot_connect_returns_early(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    m = metadata()
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=10), end_date=date.today()
    )
    coordinator.api.async_get_usage_reads = AsyncMock(side_effect=CannotConnect("down"))
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        None,
        {},
        forecast,
    )


async def test_process_api_exception_returns_early(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=0, second=0, microsecond=0)
    forecast = SimpleNamespace(start_date=when.date(), end_date=when.date())
    coordinator.api.async_get_usage_reads = AsyncMock(
        side_effect=ApiException("bad", "url")
    )
    await coordinator._process_and_insert_statistics(
        metadata(), when.date(), when.date(), 0, 0, None, {}, forecast
    )


async def test_process_empty_reads_returns_early(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    m = metadata()
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=10), end_date=date.today()
    )
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[])
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        None,
        {},
        forecast,
    )


async def test_process_empty_reads_with_last_stat_dt_updates_changed(
    coordinator: DominionSCCoordinator,
) -> None:
    """Empty reads + last_stat_dt set → last_changed updated with last_stat_dt."""
    coordinator.api.get_timezone.return_value = "UTC"
    last_dt = datetime.now()
    m = metadata()
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=10), end_date=date.today()
    )
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[])
    changed = {}
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        last_dt,
        changed,
        forecast,
    )
    assert changed[m.account] == last_dt


async def test_process_zero_consumption_filter(
    coordinator: DominionSCCoordinator,
) -> None:
    """Zero-consumption days are filtered; changed dict is untouched."""
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    zero = SimpleNamespace(start_time=when, end_time=when, consumption=0)
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[zero])
    changed = {}
    await coordinator._process_and_insert_statistics(
        metadata(),
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        None,
        changed,
        SimpleNamespace(start_date=date.today() - timedelta(days=10), end_date=date.today()),
    )
    assert changed == {}


async def test_process_zero_filter_preserves_existing_last_changed(
    coordinator: DominionSCCoordinator,
) -> None:
    """Zero-consumption filter when last_stat_dt is set: preserves changed entry."""
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=0, second=0, microsecond=0)
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[SimpleNamespace(start_time=when, end_time=when, consumption=0)]
    )
    changed = {"ELECTRIC": when}
    await coordinator._process_and_insert_statistics(
        metadata(), when.date(), when.date(), 0, 0, when, changed,
        SimpleNamespace(start_date=when.date(), end_date=when.date()),
    )
    assert changed["ELECTRIC"] == when


async def test_process_success_pushes_consumption_and_cost(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[
            SimpleNamespace(
                start_time=when, end_time=when + timedelta(hours=1), consumption=100
            )
        ]
    )
    forecast = SimpleNamespace(
        start_date=when.date() - timedelta(days=1), end_date=when.date()
    )
    hour = when.replace(minute=0)
    with (
        patch.object(
            coordinator,
            "_aggregate_hourly_data",
            return_value=({hour: 100.0}, {hour: 0.25}),
        ),
        patch(
            "custom_components.dominionsc.coordinator.async_add_external_statistics"
        ) as push,
    ):
        changed = {}
        await coordinator._process_and_insert_statistics(
            metadata(), when.date(), when.date(), 0, 0, None, changed, forecast
        )
    assert push.call_count == 2
    assert changed["ELECTRIC"] == when + timedelta(hours=1)


async def test_process_gas_does_not_push_cost(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[
            SimpleNamespace(
                start_time=when, end_time=when + timedelta(hours=1), consumption=100
            )
        ]
    )
    hour = when.replace(minute=0)
    with (
        patch.object(
            coordinator,
            "_aggregate_hourly_data",
            return_value=({hour: 100.0}, {hour: 0.0}),
        ),
        patch(
            "custom_components.dominionsc.coordinator.async_add_external_statistics"
        ) as push,
    ):
        await coordinator._process_and_insert_statistics(
            metadata("GAS", "dummy"),
            when.date(),
            when.date(),
            50,
            0,
            when,
            {},
            SimpleNamespace(start_date=when.date(), end_date=when.date()),
            existing_hours={when - timedelta(hours=1)},
        )
    assert push.call_count == 1


async def test_process_zero_filter_and_no_aggregate_output(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=15, second=0, microsecond=0)
    rows = [
        SimpleNamespace(start_time=when, end_time=when, consumption=0),
        SimpleNamespace(
            start_time=when + timedelta(days=1),
            end_time=when + timedelta(days=1, hours=1),
            consumption=1,
        ),
    ]
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=rows)
    with patch.object(coordinator, "_aggregate_hourly_data", return_value=({}, {})):
        changed = {"ELECTRIC": when}
        await coordinator._process_and_insert_statistics(
            metadata(),
            when.date(),
            when.date(),
            0,
            0,
            when,
            changed,
            SimpleNamespace(start_date=when.date(), end_date=when.date()),
        )
    assert changed["ELECTRIC"] == when


async def test_process_empty_aggregate_output_without_last_stat(
    coordinator: DominionSCCoordinator,
) -> None:
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=0, second=0, microsecond=0)
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[SimpleNamespace(start_time=when, end_time=when, consumption=1)]
    )
    with patch.object(coordinator, "_aggregate_hourly_data", return_value=({}, {})):
        changed = {}
        await coordinator._process_and_insert_statistics(
            metadata(),
            when.date(),
            when.date(),
            0,
            0,
            None,
            changed,
            SimpleNamespace(start_date=when.date(), end_date=when.date()),
        )
    assert changed == {}


async def test_process_empty_aggregate_output_with_last_stat(
    coordinator: DominionSCCoordinator,
) -> None:
    """Aggregate returns nothing but last_stat_dt is set → last_changed is updated."""
    coordinator.api.get_timezone.return_value = "UTC"
    when = datetime.now().replace(minute=0, second=0, microsecond=0)
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[SimpleNamespace(start_time=when, end_time=when, consumption=5)]
    )
    changed = {}
    with patch.object(coordinator, "_aggregate_hourly_data", return_value=({}, {})):
        await coordinator._process_and_insert_statistics(
            metadata(),
            when.date(),
            when.date(),
            0,
            0,
            when,
            changed,
            SimpleNamespace(start_date=when.date(), end_date=when.date()),
        )
    assert changed["ELECTRIC"] == when


def test_push_cost_statistics(coordinator: DominionSCCoordinator) -> None:
    with patch(
        "custom_components.dominionsc.coordinator.async_add_external_statistics"
    ) as push:
        coordinator._push_cost_statistics("cost", "Cost", [], "test", 0)
    push.assert_called_once()


# ---------------------------------------------------------------------------
# 10. Historic cost recalculation
# ---------------------------------------------------------------------------


async def test_recalculate_none_mode_skips(coordinator: DominionSCCoordinator) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    await coordinator._async_recalculate_historic_costs_locked(
        start, end, {CONF_COST_MODE: COST_MODE_NONE}
    )


async def test_recalculate_no_electric_account_skips(
    coordinator: DominionSCCoordinator,
) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.async_get_accounts = AsyncMock(return_value=({"GAS"}, "address"))
    coordinator.api.get_timezone.return_value = "UTC"
    await coordinator._async_recalculate_historic_costs_locked(
        start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
    )


async def test_recalculate_no_consumption_rows_warns(
    coordinator: DominionSCCoordinator,
) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "address")
    )
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "custom_components.dominionsc.coordinator.get_instance", return_value=recorder
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
        )


async def test_recalculate_fixed_success(coordinator: DominionSCCoordinator) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "123 Main")
    )
    cid = "dominionsc:123_main_electric_energy_consumption"
    rows = {
        cid: [
            {
                "start": datetime.combine(start, datetime.min.time()).timestamp(),
                "state": 1000.0,
            }
        ]
    }
    recorder = MagicMock()

    async def executor(func, *_args):
        if func.__name__ == "statistics_during_period":
            return rows
        return {}

    recorder.async_add_executor_job = executor
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(coordinator, "_push_cost_statistics") as push,
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
        )
    push.assert_called_once()


async def test_recalculate_seeds_previous_cost(
    coordinator: DominionSCCoordinator,
) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "123 Main")
    )
    cid = "dominionsc:123_main_electric_energy_consumption"
    cost_id = "dominionsc:123_main_electric_energy_cost"
    row_start = datetime.combine(start, datetime.min.time()).timestamp()
    prior_start = datetime.combine(
        start - timedelta(days=1),
        datetime.min.time(),
        tzinfo=__import__("datetime").timezone.utc,
    ).timestamp()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=[
            {cid: [{"start": row_start, "state": 1000}]},
            {cost_id: [{"start": prior_start, "sum": 5.0}]},
        ]
    )
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(coordinator, "_push_cost_statistics") as push,
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
        )
    assert push.call_args.args[-1] == 5.2


async def test_recalculate_ignores_prior_cost_after_window(
    coordinator: DominionSCCoordinator,
) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "123 Main")
    )
    cid = "dominionsc:123_main_electric_energy_consumption"
    cost_id = "dominionsc:123_main_electric_energy_cost"
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=[
            {
                cid: [
                    {
                        "start": datetime.combine(start, datetime.min.time()).timestamp(),
                        "state": 1000,
                    }
                ]
            },
            {
                cost_id: [
                    {
                        "start": datetime.combine(end, datetime.min.time()).timestamp(),
                        "sum": 99,
                    }
                ]
            },
        ]
    )
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch.object(coordinator, "_push_cost_statistics") as push,
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
        )
    assert push.call_args.args[-1] == 0.2


async def test_recalculate_tiered_and_seeded(
    coordinator: DominionSCCoordinator,
) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    anchor = date.today() - timedelta(days=20)
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "123 Main")
    )
    coordinator.api.async_get_forecast = AsyncMock(
        return_value=SimpleNamespace(
            start_date=anchor, end_date=anchor + timedelta(days=10)
        )
    )
    cid = "dominionsc:123_main_electric_energy_consumption"
    before = datetime.combine(start - timedelta(days=1), datetime.min.time()).timestamp()
    inside = datetime.combine(start, datetime.min.time()).timestamp()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=[
            {cid: [{"start": before, "state": 100}, {"start": inside, "state": 100}]},
            {"cost": [{"start": before, "sum": 3}]},
        ]
    )
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.dominionsc.coordinator._calculate_cost_for_wh",
            return_value=1.0,
        ) as calc,
        patch.object(coordinator, "_push_cost_statistics") as push,
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_RATE_8}
        )
    assert calc.call_count == 2
    assert push.called


async def test_recalculate_no_cost_rows_does_not_push(
    coordinator: DominionSCCoordinator,
) -> None:
    """Zero-state rows produce no cost entries → _push_cost_statistics not called."""
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    coordinator.api.get_timezone.return_value = None
    coordinator.api.async_get_accounts = AsyncMock(
        return_value=({"ELECTRIC"}, "123 Main")
    )
    cid = "dominionsc:123_main_electric_energy_consumption"
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=[
            {
                cid: [
                    {
                        "start": datetime.combine(start, datetime.min.time()).timestamp(),
                        "state": 0,
                    }
                ]
            },
            {
                "dominionsc:123_main_electric_energy_cost": [
                    {
                        "start": datetime.combine(
                            start - timedelta(days=1), datetime.min.time()
                        ),
                        "sum": None,
                    }
                ]
            },
        ]
    )
    with (
        patch(
            "custom_components.dominionsc.coordinator.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.dominionsc.coordinator.dt_util.async_get_time_zone",
            new=AsyncMock(return_value=None),
        ),
        patch.object(coordinator, "_push_cost_statistics") as push,
    ):
        await coordinator._async_recalculate_historic_costs_locked(
            start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
        )
    push.assert_not_called()


async def test_public_recalculation_lock(coordinator: DominionSCCoordinator) -> None:
    with patch.object(
        coordinator, "_async_recalculate_historic_costs_locked", new=AsyncMock()
    ) as locked:
        await coordinator.async_recalculate_historic_costs(
            date.today() - timedelta(days=1),
            date.today(),
            {CONF_COST_MODE: COST_MODE_NONE},
        )
    locked.assert_awaited_once()
