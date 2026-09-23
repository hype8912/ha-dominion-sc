"""Multi-poll scenario tests for the DominionSC coordinator.

Unlike the rest of the coordinator test suite -- which mocks each recorder
call individually with a hand-built ``side_effect`` -- these tests run the
coordinator's real ``_async_update_data()`` / ``async_recalculate_historic_costs``
against :class:`~tests._fake_recorder.FakeStatisticsStore`, a lightweight
in-memory stand-in for the HA recorder. That makes it cheap to simulate a
sequence of real polls (e.g. the API slowly publishing a backlog of days,
one poll at a time -- exactly what was observed in production logs) and
assert on the final accumulated statistics, without needing a real Home
Assistant instance or a deploy.

Use this file as a template for new scenarios: write the coordinator/store
setup once, drive several ``_async_update_data()`` calls with different
mocked ``async_get_usage_reads`` responses, and assert on ``store.rows``.
"""

from datetime import date, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dominionsc.models.forecast import Forecast
from dominionsc.models.usage_read import UsageRead
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    DOMAIN,
)
from custom_components.dominionsc.coordinator import (
    DominionSCCoordinator,
    _build_statistic_ids,
)

from ._fake_recorder import FakeStatisticsStore, patched_recorder

FIXED_TODAY = date(2026, 9, 20)


class _FixedDate(date):
    """Freezes ``date.today()`` inside the coordinator module.

    Patched in as ``custom_components.dominionsc.coordinator.date`` for the
    duration of a scenario so repeated polls agree on what day it is --
    modeling several real polls happening within the same calendar day,
    which is exactly how the gap-fill scenario below was originally observed
    in production (a later same-day poll found data an earlier one didn't).
    """

    @classmethod
    def today(cls) -> Self:
        return cls(FIXED_TODAY.year, FIXED_TODAY.month, FIXED_TODAY.day)


def _hourly_reads(day: date, num_days: int, wh_per_hour: float = 1000.0) -> list[UsageRead]:
    """Build ``num_days`` of naive, hour-aligned UsageReads starting at ``day``."""
    reads = []
    for d in range(num_days):
        day_start = datetime.combine(day + timedelta(days=d), datetime.min.time())
        for h in range(24):
            start = day_start + timedelta(hours=h)
            reads.append(
                UsageRead(
                    start_time=start,
                    end_time=start + timedelta(minutes=59, seconds=59),
                    consumption=wh_per_hour,
                )
            )
    return reads


def _make_coordinator(hass: HomeAssistant, entry: MockConfigEntry, forecast: Forecast) -> DominionSCCoordinator:
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
    coord.api.async_login = AsyncMock()
    coord.api.get_timezone = MagicMock(return_value="America/New_York")
    coord.api.async_get_accounts = AsyncMock(return_value=(["ELECTRIC"], "123_main_st"))
    coord.api.async_get_register_reads = AsyncMock(return_value=[])
    coord.api.async_get_forecast = AsyncMock(return_value=forecast)
    return coord


@pytest.fixture
def entry() -> MockConfigEntry:
    from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "user", CONF_PASSWORD: "password"},
        options={CONF_COST_MODE: COST_MODE_NONE},
    )


async def test_multi_poll_gap_fill_converges(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Three polls, each seeing more of the backlog, converge to one clean series.

    Poll 1 sees only 3 published days (initial backfill). Poll 2 sees a 4th
    day that showed up since. Poll 3 sees a 5th (yesterday, finally
    published). No hour should ever be inserted twice, and the cumulative
    sum should always match exactly what's been published so far -- this is
    the exact "Detected N missing date(s) ... Gap-fill: inserting N newly-
    available hours" behavior seen in the real logs.
    """
    billing_start = FIXED_TODAY - timedelta(days=5)
    forecast = Forecast(
        start_date=billing_start,
        end_date=FIXED_TODAY + timedelta(days=25),
        current_date=FIXED_TODAY - timedelta(days=1),
        cost_to_date=0.0,
        forecasted_cost=0.0,
        typical_cost=0.0,
    )
    coord = _make_coordinator(hass, entry, forecast)
    stat_id, _, _ = _build_statistic_ids("123_main_st", "ELECTRIC")

    with (
        patched_recorder(FakeStatisticsStore()) as store,
        patch("custom_components.dominionsc.coordinator.date", _FixedDate),
        patch(
            "custom_components.dominionsc.coordinator.dt_util.async_get_time_zone",
            new=AsyncMock(return_value=None),
        ),
    ):
        for published_days in (3, 4, 5):
            coord.api.async_get_usage_reads = AsyncMock(return_value=_hourly_reads(billing_start, published_days))
            await coord._async_update_data()
            assert store.row_count(stat_id) == published_days * 24
            assert store.last_sum(stat_id) == published_days * 24 * 1000.0

        starts = [r["start"] for r in store.rows[stat_id]]
        assert len(starts) == len(set(starts)), "an hour was inserted more than once"


async def test_recalculation_prices_real_consumption_rows(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A fixed-rate recalculation run against real backfilled consumption
    produces the expected cumulative cost, exercising the full
    ``async_recalculate_historic_costs`` path against a real (fake) store
    instead of a fully mocked recorder."""
    billing_start = FIXED_TODAY - timedelta(days=3)
    forecast = Forecast(
        start_date=billing_start,
        end_date=FIXED_TODAY + timedelta(days=27),
        current_date=FIXED_TODAY - timedelta(days=1),
        cost_to_date=0.0,
        forecasted_cost=0.0,
        typical_cost=0.0,
    )
    coord = _make_coordinator(hass, entry, forecast)
    consumption_id, cost_id, _ = _build_statistic_ids("123_main_st", "ELECTRIC")

    with (
        patched_recorder(FakeStatisticsStore()) as store,
        patch("custom_components.dominionsc.coordinator.date", _FixedDate),
        patch(
            "custom_components.dominionsc.coordinator.dt_util.async_get_time_zone",
            new=AsyncMock(return_value=None),
        ),
    ):
        # Backfill 3 days of consumption (1000 Wh/hour) with no cost mode yet.
        coord.api.async_get_usage_reads = AsyncMock(return_value=_hourly_reads(billing_start, 3))
        await coord._async_update_data()
        assert store.row_count(consumption_id) == 3 * 24

        # Now recalculate cost over those same 3 days at a fixed $0.15/kWh.
        await coord.async_recalculate_historic_costs(
            start_date=billing_start,
            end_date=billing_start + timedelta(days=2),
            new_options={CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.15},
        )

    # 3 days * 24 hours * 1000 Wh * $0.15/kWh == $10.80 total.
    assert store.row_count(cost_id) == 3 * 24
    assert store.last_sum(cost_id) == pytest.approx(10.80)
