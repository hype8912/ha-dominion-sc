"""Tests for Phase 5: register-aware statistics (grid/solar/gas separation).

See docs/REFACTOR_PLAN.md Phase 5. Written to be behavioral from the start
(unlike the coverage-driven test_coordinator_* files) since this is new
feature code, not a pure refactor.

Test groups:
    TestBuildRegisterStatisticIds -- pure function, no coordinator needed.
    TestStatisticIdRegressionGuarantee -- the release-blocking guarantee that
        established (sole-register) installs' statistic ids never change.
    TestDiscoverRegisters -- the discovery call and its failure fallback.
    TestInsertStatisticsRegisterRouting -- established vs. never-backfilled
        vs. multi-register routing in _insert_statistics.
    TestProcessAndInsertStatisticsRegisterFetch -- the register-specific
        fetch-and-select path in _process_and_insert_statistics.
"""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dominionsc.models.register_reads import RegisterReads
from dominionsc.usage_read import UsageRead
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.const import DOMAIN
from custom_components.dominionsc.coordinator import DominionSCCoordinator
from custom_components.dominionsc.models import DominionSCStatisticMetadata
from custom_components.dominionsc.statistics_ids import (
    _build_register_statistic_ids,
    _build_statistic_ids,
)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Bare config entry; individual tests set whatever options they need."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_USERNAME: "t", CONF_PASSWORD: "t"},
    )


# ---------------------------------------------------------------------------
# Pure function: _build_register_statistic_ids
# ---------------------------------------------------------------------------


class TestBuildRegisterStatisticIds:
    """Tests for the pure statistic-id builder."""

    def test_sole_register_matches_legacy_exactly(self) -> None:
        """is_sole_register=True must produce byte-identical output to the
        legacy _build_statistic_ids -- this is the core backward-compat
        guarantee. (Template objects compare by identity, not value, so
        compare ids directly and the template *string*.)
        """
        legacy_id, legacy_cost, legacy_name = _build_statistic_ids("123 Main St", "ELECTRIC")
        reg_id, reg_cost, reg_name = _build_register_statistic_ids(
            "123 Main St", "ELECTRIC", usage_point_id="unused", is_sole_register=True
        )
        assert reg_id == legacy_id
        assert reg_cost == legacy_cost
        assert reg_name.template == legacy_name.template

    def test_sole_register_ignores_usage_point_id(self) -> None:
        """The usage_point_id argument is irrelevant when is_sole_register=True
        (delegation to the legacy function doesn't even see it)."""
        a_id, a_cost, a_name = _build_register_statistic_ids(
            "123 Main St", "ELECTRIC", usage_point_id="AAA", is_sole_register=True
        )
        b_id, b_cost, b_name = _build_register_statistic_ids(
            "123 Main St", "ELECTRIC", usage_point_id="ZZZ", is_sole_register=True
        )
        assert a_id == b_id
        assert a_cost == b_cost
        assert a_name.template == b_name.template

    def test_multi_register_ids_differ_by_usage_point(self) -> None:
        """Two registers under the same account get distinct consumption ids."""
        grid_id, grid_cost, _ = _build_register_statistic_ids(
            "123 Main St",
            "ELECTRIC",
            usage_point_id="789650231124208724",
            is_sole_register=False,
        )
        solar_id, solar_cost, _ = _build_register_statistic_ids(
            "123 Main St",
            "ELECTRIC",
            usage_point_id="8026138875215173424",
            is_sole_register=False,
        )
        assert grid_id != solar_id
        assert grid_cost != solar_cost

    def test_multi_register_ids_differ_from_legacy(self) -> None:
        """A multi-register id must NOT collide with the legacy merged id --
        otherwise a fresh multi-register discovery could silently overwrite
        an unrelated statistic."""
        legacy_id, _, _ = _build_statistic_ids("123 Main St", "ELECTRIC")
        register_id, _, _ = _build_register_statistic_ids(
            "123 Main St",
            "ELECTRIC",
            usage_point_id="789650231124208724",
            is_sole_register=False,
        )
        assert register_id != legacy_id

    def test_multi_register_gas_cost_id_generated(self) -> None:
        """GAS accounts now get a cost_id; coordinator nullifies it when no gas mode active."""
        _, cost_id, _ = _build_register_statistic_ids("123 Main St", "GAS", usage_point_id="ABC123", is_sole_register=False)
        # Phase 4: gas cost IDs are generated so gas cost stats can be written.
        assert cost_id == "dominionsc:123_main_st_gas_abc123_energy_cost"

    def test_multi_register_name_includes_meter_suffix(self) -> None:
        """The display name distinguishes registers via trailing UsagePoint digits."""
        _, _, name_prefix = _build_register_statistic_ids(
            "123 Main St",
            "ELECTRIC",
            usage_point_id="789650231124208724",
            is_sole_register=False,
        )
        rendered = name_prefix.substitute(stat_type="consumption")
        assert "208724" in rendered  # trailing 6 digits of the UsagePoint id

    def test_same_usage_point_id_is_stable(self) -> None:
        """Calling twice with the same inputs produces the same id (determinism)."""
        first_id, first_cost, first_name = _build_register_statistic_ids(
            "123 Main St", "ELECTRIC", usage_point_id="XYZ", is_sole_register=False
        )
        second_id, second_cost, second_name = _build_register_statistic_ids(
            "123 Main St", "ELECTRIC", usage_point_id="XYZ", is_sole_register=False
        )
        assert first_id == second_id
        assert first_cost == second_cost
        assert first_name.template == second_name.template


# ---------------------------------------------------------------------------
# THE release-blocking regression guarantee
# ---------------------------------------------------------------------------


class TestStatisticIdRegressionGuarantee:
    """
    Guards docs/REFACTOR_PLAN.md's release-blocking constraint:

        "Statistic-id stability is release-blocking. [...] A dedicated test
        must assert existing ids are unchanged."

    If this test ever fails, an existing single-meter install's Energy
    Dashboard history would be orphaned by this change. Do not relax it.
    """

    @pytest.mark.parametrize(
        ("address", "account"),
        [
            ("3005 ELLINGTON DR", "ELECTRIC"),
            ("3005 ELLINGTON DR", "GAS"),
            ("123 Main St, Unit 4B", "ELECTRIC"),
            ("O'Brien Ave", "GAS"),
        ],
    )
    def test_sole_register_ids_unchanged_across_addresses(self, address: str, account: str) -> None:
        legacy_id, legacy_cost, legacy_name = _build_statistic_ids(address, account)
        reg_id, reg_cost, reg_name = _build_register_statistic_ids(address, account, usage_point_id="", is_sole_register=True)
        assert reg_id == legacy_id
        assert reg_cost == legacy_cost
        assert reg_name.template == legacy_name.template

    async def test_established_install_never_calls_discovery(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """
        An account with an existing legacy statistic must NEVER trigger a
        register-discovery call, regardless of what that discovery might
        find. This is what makes the id-stability guarantee airtight: no
        network call means no possible path to a different outcome.
        """
        mock_config_entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, mock_config_entry)
        coord.api = MagicMock()
        coord.api.async_get_register_reads = AsyncMock(
            side_effect=AssertionError("discovery must not be called for an established install")
        )
        forecast_mock = MagicMock()
        forecast_mock.start_date = date(2025, 7, 1)
        forecast_mock.end_date = date(2025, 7, 31)

        legacy_id, _, _ = _build_statistic_ids("123 Main", "ELECTRIC")
        recorder = MagicMock()
        # The legacy id already has a recorded statistic -> established install.
        recorder.async_add_executor_job = AsyncMock(return_value={legacy_id: [{"start": 0, "sum": 100}]})

        with (
            patch.object(coord, "_update_statistics", new=AsyncMock()),
            patch(
                "custom_components.dominionsc.coordinator.get_instance",
                return_value=recorder,
            ),
        ):
            # Should not raise -- if it does, discovery was called.
            await coord._insert_statistics(["ELECTRIC"], "123 Main", forecast_mock)

        coord.api.async_get_register_reads.assert_not_called()


# ---------------------------------------------------------------------------
# _discover_registers
# ---------------------------------------------------------------------------


class TestDiscoverRegisters:
    """Tests for the register-discovery call and its failure fallback."""

    async def _make_coordinator(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> DominionSCCoordinator:
        mock_config_entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, mock_config_entry)
        coord.api = MagicMock()
        coord.api.get_timezone = MagicMock(return_value="America/New_York")
        return coord

    async def test_returns_registers_on_success(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
        coord = await self._make_coordinator(hass, mock_config_entry)
        registers = [
            RegisterReads(usage_point_id="A", reads=[]),
            RegisterReads(usage_point_id="B", reads=[]),
        ]
        coord.api.async_get_register_reads = AsyncMock(return_value=registers)

        result = await coord._discover_registers("ELECTRIC")

        assert result == registers

    async def test_cannot_connect_falls_back_to_empty(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
        from dominionsc.exceptions import CannotConnect

        coord = await self._make_coordinator(hass, mock_config_entry)
        coord.api.async_get_register_reads = AsyncMock(side_effect=CannotConnect("down"))

        result = await coord._discover_registers("ELECTRIC")

        assert result == []

    async def test_api_exception_falls_back_to_empty(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
        from dominionsc.exceptions import ApiException

        coord = await self._make_coordinator(hass, mock_config_entry)
        coord.api.async_get_register_reads = AsyncMock(side_effect=ApiException("bad response", "https://example.test"))

        result = await coord._discover_registers("ELECTRIC")

        assert result == []


# ---------------------------------------------------------------------------
# _insert_statistics routing (established / never-backfilled / multi-register)
# ---------------------------------------------------------------------------


class TestInsertStatisticsRegisterRouting:
    """Tests for the three routing branches in _insert_statistics."""

    def _forecast(self) -> MagicMock:
        forecast_mock = MagicMock()
        forecast_mock.start_date = date(2025, 7, 1)
        forecast_mock.end_date = date(2025, 7, 31)
        return forecast_mock

    async def test_multi_register_backfills_each_register_separately(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """
        A never-backfilled account where discovery finds 2 registers must
        result in 2 separate _backfill_statistics calls, each with metadata
        carrying a distinct usage_point_id and a distinct consumption_id.
        """
        mock_config_entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, mock_config_entry)
        coord.api = MagicMock()
        coord.api.get_timezone = MagicMock(return_value="America/New_York")
        coord.api.async_get_register_reads = AsyncMock(
            return_value=[
                RegisterReads(usage_point_id="GRID_UP", reads=[]),
                RegisterReads(usage_point_id="SOLAR_UP", reads=[]),
            ]
        )

        recorder = MagicMock()
        recorder.async_add_executor_job = AsyncMock(return_value={})  # no stats exist
        seen_metadata: list[DominionSCStatisticMetadata] = []

        async def fake_backfill(metadata, last_changed, forecast) -> None:
            seen_metadata.append(metadata)

        with (
            patch.object(coord, "_backfill_statistics", side_effect=fake_backfill),
            patch(
                "custom_components.dominionsc.coordinator.get_instance",
                return_value=recorder,
            ),
        ):
            await coord._insert_statistics(["ELECTRIC"], "123 Main", self._forecast())

        assert len(seen_metadata) == 2
        seen_usage_points = {m.usage_point_id for m in seen_metadata}
        assert seen_usage_points == {"GRID_UP", "SOLAR_UP"}
        seen_ids = {m.consumption_id for m in seen_metadata}
        assert len(seen_ids) == 2  # distinct statistic ids

    async def test_multi_register_backfill_key_isolated_per_register(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """
        Two registers under the same account must not share a
        _backfill_initiated flag -- one register's backfill being "in
        flight" must not block the other's from starting.
        """
        mock_config_entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, mock_config_entry)
        coord.api = MagicMock()
        coord.api.get_timezone = MagicMock(return_value="America/New_York")
        coord.api.async_get_register_reads = AsyncMock(
            return_value=[
                RegisterReads(usage_point_id="GRID_UP", reads=[]),
                RegisterReads(usage_point_id="SOLAR_UP", reads=[]),
            ]
        )
        # Simulate GRID_UP's backfill already in flight from a prior cycle.
        coord._backfill_initiated["ELECTRIC:GRID_UP"] = True

        recorder = MagicMock()
        recorder.async_add_executor_job = AsyncMock(return_value={})
        called_for: list[str] = []

        async def fake_backfill(metadata, last_changed, forecast) -> None:
            called_for.append(metadata.usage_point_id)

        with (
            patch.object(coord, "_backfill_statistics", side_effect=fake_backfill),
            patch(
                "custom_components.dominionsc.coordinator.get_instance",
                return_value=recorder,
            ),
        ):
            await coord._insert_statistics(["ELECTRIC"], "123 Main", self._forecast())

        # GRID_UP was skipped (backfill in flight); SOLAR_UP proceeded.
        assert called_for == ["SOLAR_UP"]


# ---------------------------------------------------------------------------
# _process_and_insert_statistics register-specific fetch
# ---------------------------------------------------------------------------


class TestProcessAndInsertStatisticsRegisterFetch:
    """Tests for the register-aware fetch-and-select branch."""

    def _metadata(self, usage_point_id: str | None) -> DominionSCStatisticMetadata:
        from string import Template

        return DominionSCStatisticMetadata(
            account="ELECTRIC",
            consumption_id="dominionsc:test_energy_consumption",
            cost_id=None,
            name_prefix=Template("ELECTRIC $stat_type test"),
            unit_class="energy",
            unit="Wh",
            usage_point_id=usage_point_id,
        )

    async def _make_coordinator(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> DominionSCCoordinator:
        mock_config_entry.add_to_hass(hass)
        coord = DominionSCCoordinator(hass, mock_config_entry)
        coord.api = MagicMock()
        coord.api.get_timezone = MagicMock(return_value="America/New_York")
        return coord

    async def test_sole_register_uses_flat_fetch(self, hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
        """usage_point_id=None must call the flat async_get_usage_reads, not
        async_get_register_reads -- the legacy path is untouched."""
        coord = await self._make_coordinator(hass, mock_config_entry)
        coord.api.async_get_usage_reads = AsyncMock(return_value=[])
        coord.api.async_get_register_reads = AsyncMock(
            side_effect=AssertionError("must not be called for sole-register metadata")
        )

        await coord._process_and_insert_statistics(
            metadata=self._metadata(usage_point_id=None),
            start_date=date(2025, 7, 1),
            data_date=date(2025, 7, 2),
            consumption_sum=0.0,
            cost_sum=0.0,
            last_stat_dt=None,
            last_changed_per_account={},
            forecast=None,
        )

        coord.api.async_get_usage_reads.assert_awaited_once()
        coord.api.async_get_register_reads.assert_not_called()

    async def test_multi_register_selects_matching_register(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A specific usage_point_id must fetch via async_get_register_reads
        and use only that register's reads, ignoring other registers'."""
        coord = await self._make_coordinator(hass, mock_config_entry)
        coord.api.async_get_usage_reads = AsyncMock(side_effect=AssertionError("must not be called for register-aware metadata"))
        target_read = UsageRead(
            start_time=MagicMock(date=MagicMock(return_value=date(2025, 7, 1))),
            end_time=MagicMock(),
            consumption=500,
        )
        coord.api.async_get_register_reads = AsyncMock(
            return_value=[
                RegisterReads(usage_point_id="OTHER_UP", reads=[MagicMock()]),
                RegisterReads(usage_point_id="TARGET_UP", reads=[target_read]),
            ]
        )

        last_changed: dict = {}
        with patch.object(
            coord,
            "_aggregate_hourly_data",
            return_value=({}, {}),
        ) as mock_aggregate:
            await coord._process_and_insert_statistics(
                metadata=self._metadata(usage_point_id="TARGET_UP"),
                start_date=date(2025, 7, 1),
                data_date=date(2025, 7, 2),
                consumption_sum=0.0,
                cost_sum=0.0,
                last_stat_dt=None,
                last_changed_per_account=last_changed,
                forecast=None,
            )

        coord.api.async_get_register_reads.assert_awaited_once()
        # Only TARGET_UP's single read was passed in, not OTHER_UP's.
        passed_usage_reads = mock_aggregate.call_args.kwargs["usage_reads"]
        assert passed_usage_reads == [target_read]

    async def test_register_with_no_data_yields_empty_reads(
        self, hass: HomeAssistant, mock_config_entry: MockConfigEntry
    ) -> None:
        """A usage_point_id not present in this window's response (e.g. a
        newly-discovered register with no data yet) must not raise -- it
        yields an empty read list, handled by the existing no-data path."""
        coord = await self._make_coordinator(hass, mock_config_entry)
        coord.api.async_get_register_reads = AsyncMock(return_value=[RegisterReads(usage_point_id="SOME_OTHER_UP", reads=[])])
        last_changed: dict = {}

        await coord._process_and_insert_statistics(
            metadata=self._metadata(usage_point_id="MISSING_UP"),
            start_date=date(2025, 7, 1),
            data_date=date(2025, 7, 2),
            consumption_sum=0.0,
            cost_sum=0.0,
            last_stat_dt=None,
            last_changed_per_account=last_changed,
            forecast=None,
        )

        # No exception, and nothing recorded for this account (no data).
        assert "ELECTRIC" not in last_changed
