"""Complete unit coverage for the Dominion SC coordinator."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    DominionSCStatisticMetadata,
    _billing_cycle_get_gap,
    _calculate_cost_for_wh,
    _estimate_billing_cycles,
    _find_billing_cycle_for_date,
)
from custom_components.dominionsc.rates import SC_RATE_8
from dominionsc.exceptions import ApiException, CannotConnect, InvalidAuth, MfaChallenge


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
        coordinator = DominionSCCoordinator(hass, entry)
        coordinator.api = MagicMock()
        return coordinator


def metadata(
    account: str = "ELECTRIC", cost_id: str | None = "cost"
) -> DominionSCStatisticMetadata:
    from string import Template

    return DominionSCStatisticMetadata(
        account, "consumption", cost_id, Template("$stat_type"), "energy", "Wh"
    )


def test_cost_and_cycle_edge_cases() -> None:
    assert _calculate_cost_for_wh(10, datetime(2025, 1, 1), 0, "unknown", 0, None) == 0
    assert (
        _calculate_cost_for_wh(10, datetime(2025, 1, 1), 0, COST_MODE_RATE_8, 0, None)
        == 0
    )
    assert _billing_cycle_get_gap(date(2025, 2, 1)) == 31
    assert _billing_cycle_get_gap(date(2024, 6, 1)) == 30
    assert _billing_cycle_get_gap(date(2025, 6, 1)) == 31
    cycles = _estimate_billing_cycles(
        date(2025, 7, 1), date(2025, 7, 31), date(2025, 5, 1), date(2025, 9, 1)
    )
    assert cycles[0][0] <= date(2025, 5, 1)
    assert cycles[-1][1] >= date(2025, 9, 1)
    assert _find_billing_cycle_for_date(date(2030, 1, 1), cycles) is None


@pytest.mark.parametrize(
    "exc, expected",
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


async def test_insert_existing_and_already_started(
    coordinator: DominionSCCoordinator,
) -> None:
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

    coordinator._backfill_initiated["GAS"] = True
    recorder.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "custom_components.dominionsc.coordinator.get_instance", return_value=recorder
    ):
        assert await coordinator._insert_statistics(["GAS"], "address", forecast) == {}


async def test_backfill_options(coordinator: DominionSCCoordinator) -> None:
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    with patch.object(
        coordinator, "_process_and_insert_statistics", new=AsyncMock()
    ) as process:
        await coordinator._backfill_statistics(metadata(), {}, forecast)
        process.assert_awaited_once()
        coordinator.config_entry._options = {
            CONF_EXTENDED_BACKFILL: True,
            CONF_EXTENDED_COST_BACKFILL: False,
        }
        await coordinator._backfill_statistics(metadata(), {}, forecast)
        assert process.await_args.kwargs["cost_start_date"] is None


def test_aggregate_all_paths(coordinator: DominionSCCoordinator) -> None:
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=2), end_date=date.today()
    )
    read = lambda when, consumption: SimpleNamespace(
        start_time=when, end_time=when + timedelta(hours=1), consumption=consumption
    )
    first = datetime.now().replace(minute=15, second=0, microsecond=0)
    rows = [read(first, 100), read(first + timedelta(minutes=20), 50)]
    cons, costs = coordinator._aggregate_hourly_data(
        rows, metadata(), forecast, first.date(), True
    )
    assert len(cons) == 1 and costs
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
    assert cons and not any(costs.values())
    existing = {first.replace(minute=0)}
    assert coordinator._aggregate_hourly_data(
        rows, metadata(), forecast, first.date(), True, existing_hours=existing
    ) == ({}, {})
    assert (
        coordinator._aggregate_hourly_data(
            rows, metadata("GAS", None), forecast, first.date(), False
        )[1]
        == {}
    )


async def test_process_paths(coordinator: DominionSCCoordinator) -> None:
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
    zero = SimpleNamespace(
        start_time=datetime.now(), end_time=datetime.now(), consumption=0
    )
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[zero])
    changed = {}
    await coordinator._process_and_insert_statistics(
        m,
        date.today() - timedelta(days=2),
        date.today() - timedelta(days=1),
        0,
        0,
        None,
        changed,
        forecast,
    )
    assert changed == {}


async def test_update_statistics_error_and_current(
    coordinator: DominionSCCoordinator,
) -> None:
    m = metadata()
    await coordinator._update_statistics(
        m, {}, {}, {}, SimpleNamespace(start_date=date.today() - timedelta(days=2))
    )
    last = {"consumption": [{"start": datetime.now().timestamp(), "sum": 1}]}
    coordinator.api.get_timezone.return_value = "UTC"
    coordinator.api.async_get_usage_reads = AsyncMock(return_value=[])
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={"consumption": []})
    with patch(
        "custom_components.dominionsc.coordinator.get_instance", return_value=recorder
    ):
        await coordinator._update_statistics(
            m,
            last,
            {"cost": [{"sum": "bad"}]},
            {},
            SimpleNamespace(start_date=date.today() - timedelta(days=2)),
        )


async def test_recalculate_paths(coordinator: DominionSCCoordinator) -> None:
    start, end = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    await coordinator._async_recalculate_historic_costs_locked(
        start, end, {CONF_COST_MODE: COST_MODE_NONE}
    )
    coordinator.api.async_get_accounts = AsyncMock(return_value=({"GAS"}, "address"))
    coordinator.api.get_timezone.return_value = "UTC"
    await coordinator._async_recalculate_historic_costs_locked(
        start, end, {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.2}
    )
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


def test_push_cost(coordinator: DominionSCCoordinator) -> None:
    with patch(
        "custom_components.dominionsc.coordinator.async_add_external_statistics"
    ) as push:
        coordinator._push_cost_statistics("cost", "Cost", [], "test", 0)
    push.assert_called_once()


def test_billing_gap_fallback_branch() -> None:
    assert _billing_cycle_get_gap(SimpleNamespace(month=13, year=2025)) == 30


async def test_process_success_and_cost(coordinator: DominionSCCoordinator) -> None:
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


async def test_update_statistics_dispatches_processing(
    coordinator: DominionSCCoordinator,
) -> None:
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


def test_billing_gap_fallback_branch() -> None:
    assert _billing_cycle_get_gap(SimpleNamespace(month=13, year=2025)) == 30


async def test_backfill_extended_cost_window(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
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
        coordinator = DominionSCCoordinator(hass, entry)
    forecast = SimpleNamespace(
        start_date=date.today() - timedelta(days=5), end_date=date.today()
    )
    with patch.object(
        coordinator, "_process_and_insert_statistics", new=AsyncMock()
    ) as process:
        await coordinator._backfill_statistics(metadata(), {}, forecast)
    assert process.await_args.kwargs["cost_start_date"] == forecast.start_date


async def test_update_current_window_early_return(
    coordinator: DominionSCCoordinator,
) -> None:
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


async def test_update_stale_stat_clamps_start(
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


async def test_process_update_gas_does_not_push_cost(
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


async def test_update_new_days_without_missing_dates(
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


async def test_update_all_expected_dates_but_new_days(
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


async def test_aggregate_fixed_cost_path_with_rows(
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
    assert consumption and costs


async def test_process_empty_output_without_last_stat(
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


async def test_aggregate_fixed_empty_and_tiered_existing(
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
                        "start": datetime.combine(
                            start, datetime.min.time()
                        ).timestamp(),
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
    before = datetime.combine(
        start - timedelta(days=1), datetime.min.time()
    ).timestamp()
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


async def test_recalculate_no_cost_rows(coordinator: DominionSCCoordinator) -> None:
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
                        "start": datetime.combine(
                            start, datetime.min.time()
                        ).timestamp(),
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


def test_cost_helper_effective_date_branches() -> None:
    assert (
        _calculate_cost_for_wh(10, datetime(2025, 1, 1), 0, COST_MODE_FIXED, 0.2, None)
        == 0.002
    )
    assert (
        _calculate_cost_for_wh(
            10, datetime(2025, 1, 1), 0, COST_MODE_RATE_8, 0, SC_RATE_8
        )
        == 0
    )


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


async def test_update_datetime_row_and_new_day_without_missing(
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


async def test_process_api_exception_and_zero_filter_return(
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
    coordinator.api.async_get_usage_reads = AsyncMock(
        return_value=[SimpleNamespace(start_time=when, end_time=when, consumption=0)]
    )
    changed = {"ELECTRIC": when}
    await coordinator._process_and_insert_statistics(
        metadata(), when.date(), when.date(), 0, 0, when, changed, forecast
    )
    assert changed["ELECTRIC"] == when


async def test_process_no_new_statistics_keeps_last_changed(
    coordinator: DominionSCCoordinator,
) -> None:
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
