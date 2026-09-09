"""
Coordinator for the dominionsc integration.

This is the **heart** of the integration. The
:class:`DominionSCCoordinator` owns all communication with the Dominion API,
processes usage data, and writes statistics into the HA recorder.

Responsibilities
----------------
1. **Authentication**: re-logs in on every poll cycle (sessions are short-lived).
2. **Data fetching**: retrieves account list, billing forecast, and usage intervals.
3. **Statistics insertion**: inserts hourly energy consumption and cost statistics
   into the HA recorder via ``async_add_external_statistics``. These appear in
   the Energy Dashboard.
4. **Register discovery** (Phase 5): for ELECTRIC accounts that have never been
   backfilled, performs a one-time look-back to count physical meter registers.
   Net-metered solar accounts have two registers (grid delivery + solar export)
   and receive separate statistic series for each.
5. **Backfill vs incremental update**: on first setup, loads data from the
   billing-cycle start (or up to 365 days if extended backfill is enabled). On
   subsequent polls, performs incremental updates with a short lookback window
   to fill any gaps from late-arriving API data.
6. **Historic cost recalculation**: when the user changes cost mode, can
   re-price all stored consumption rows for a chosen date range using the new
   rate schedule.

Statistics data flow
--------------------
::

    Dominion API (dominionsc library)
            |
    _async_update_data()
            |
    _insert_statistics(accounts, service_addr, forecast)
            |
         for each account:
            |
    _insert_statistics()  ─── checks recorder for legacy statistic ID
            |                  ─── if no legacy stat AND no backfill in flight:
            |                       calls _discover_registers() [once only]
            |
    _process_account()   ─── backfill or incremental update decision
            |
    _backfill_statistics()         OR      _update_statistics()
            |                                      |
    _process_and_insert_statistics() ──────────────┘
            |
        async_get_usage_reads() / async_get_register_reads()
            |
        _aggregate_hourly_data()  (wrapper -> aggregation.py)
            |
        async_add_external_statistics()  -> HA recorder

Module-level re-exports
-----------------------
The data models and pure helpers that were originally in this file now live in
:mod:`.models`, :mod:`.cost`, :mod:`.billing`, :mod:`.aggregation`, and
:mod:`.statistics_ids`. They are re-exported here via ``__all__`` so that
existing imports of ``from ...coordinator import <name>`` continue to work
without change. See docs/REFACTOR_PLAN.md for the full migration history.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from string import Template
from typing import Any

from dominionsc import (
    DominionSC,
    Forecast,
    create_cookie_jar,
)
from dominionsc.exceptions import ApiException, CannotConnect, InvalidAuth, MfaChallenge
from dominionsc.models.register_reads import RegisterReads
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .aggregation import aggregate_hourly_data
from .billing import (
    _billing_cycle_get_gap,
    _estimate_billing_cycles,
    _find_billing_cycle_for_date,
)
from .const import (
    CONF_EXTENDED_BACKFILL,
    CONF_EXTENDED_COST_BACKFILL,
    CONF_LOGIN_DATA,
    COST_MODE_NONE,
    DOMAIN,
    EXTENDED_BACKFILL_DAYS,
    LOOKBACK_DAYS,
)
from .cost import _calculate_cost_for_wh, _resolve_cost_config
from .models import (
    DominionSCAccountData,
    DominionSCData,
    DominionSCStatisticMetadata,
)
from .rates import TIERED_RATE_REGISTRY
from .statistics_ids import _build_register_statistic_ids, _build_statistic_ids

_LOGGER = logging.getLogger(__name__)

type DominionSCConfigEntry = ConfigEntry[DominionSCCoordinator]

# Re-exported for backward compatibility. The dataclasses now live in
# models.py, the pure cost helpers in cost.py, the billing-cycle helpers in
# billing.py, and statistic-id construction in statistics_ids.py; existing
# imports of ``from ...coordinator import <name>`` continue to resolve via
# these re-exports. See docs/REFACTOR_PLAN.md Phase 2.
__all__ = [
    "DominionSCAccountData",
    "DominionSCConfigEntry",
    "DominionSCCoordinator",
    "DominionSCData",
    "DominionSCStatisticMetadata",
    "_billing_cycle_get_gap",
    "_build_register_statistic_ids",
    "_build_statistic_ids",
    "_calculate_cost_for_wh",
    "_estimate_billing_cycles",
    "_find_billing_cycle_for_date",
    "_resolve_cost_config",
]


# ---------------------------------------------------------------------------
# The pure helpers (statistic-id / cost / billing-cycle construction) that
# previously lived here now live in statistics_ids.py, cost.py, and billing.py
# respectively, and are re-exported above for backward compatibility.
# ---------------------------------------------------------------------------


class DominionSCCoordinator(DataUpdateCoordinator[DominionSCData]):
    """
    Fetch DominionSC data, update sensors, and insert long-term statistics.

    This coordinator acts as the single owner of:
    - The Dominion API client instance.
    - The backfill-tracking state (which accounts have had their initial
      statistics load started).
    - The recalculation lock (prevents concurrent historic cost recalculations).

    It extends HA's ``DataUpdateCoordinator`` with a 12-hour polling interval.
    The base class handles debouncing, listener notification, and error-state
    management automatically.
    """

    config_entry: DominionSCConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: DominionSCConfigEntry,
    ) -> None:
        """
        Initialise the coordinator and create the API client.

        Args:
            hass:         The Home Assistant instance.
            config_entry: The config entry holding credentials and options.

        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            # Dominion's data is updated once daily. Poll every 12 hours so
            # we are at most 12 hours behind without hammering the API.
            update_interval=timedelta(hours=12),
        )
        # Long-lived API client. The session is re-authenticated on every poll
        # cycle because Dominion sessions expire after a few minutes of inactivity.
        self.api = DominionSC(
            async_create_clientsession(hass, cookie_jar=create_cookie_jar()),
            config_entry.data[CONF_USERNAME],
            config_entry.data[CONF_PASSWORD],
            # Cached TFA session token. Passing it allows the API client to
            # skip the MFA challenge on re-authentication calls.
            config_entry.data.get(CONF_LOGIN_DATA),
        )
        # Maps a "backfill key" (account or account:usage_point_id) to True
        # while the initial statistics backfill has been submitted to the
        # recorder but not yet committed. Prevents a second poll from starting
        # a duplicate backfill before the first one is visible in the recorder.
        self._backfill_initiated: dict[str, bool] = {}

        # Held for the full duration of an async_recalculate_historic_costs
        # call. The options flow inspects this lock and blocks a second
        # recalculation from starting while one is already in progress.
        self.recalculation_lock = asyncio.Lock()

        @callback
        def _dummy_listener() -> None:
            """
            No-op listener that keeps the coordinator alive.

            Without at least one listener the base class stops calling
            ``_async_update_data`` after the first poll. Accounts without a
            billing forecast produce no sensor entities, which would otherwise
            leave no listeners registered and halt statistics updates.
            """

        # Registering the dummy listener ensures the coordinator keeps polling
        # even when no sensor entities are present (e.g. forecast unavailable).
        self.async_add_listener(_dummy_listener)

    async def _async_update_data(
        self,
    ) -> DominionSCData:
        """
        Fetch account data from the Dominion API and insert statistics.

        Called every 12 hours by the base coordinator. The sequence is:
        1. Re-authenticate (sessions are short-lived).
        2. Fetch accounts and billing forecast.
        3. Call ``_insert_statistics`` to upsert hourly consumption / cost data
           into the HA recorder.
        4. Return a :class:`~.models.DominionSCData` snapshot for sensors.

        Raises:
            :class:`~homeassistant.exceptions.ConfigEntryAuthFailed`: on
                ``InvalidAuth`` or ``MfaChallenge`` — triggers HA's built-in
                re-auth flow so the user is prompted to re-enter credentials.
            :class:`~homeassistant.helpers.update_coordinator.UpdateFailed`:
                on ``CannotConnect`` — HA will retry on the next interval.

        """
        try:
            # Sessions expire after a few minutes. Since we only poll every
            # 12 hours, always treat the previous session as expired and
            # re-authenticate unconditionally.
            _LOGGER.debug("API: async_login")
            await self.api.async_login()
        except (InvalidAuth, MfaChallenge) as err:
            _LOGGER.error("Error during login: %s", err)
            raise ConfigEntryAuthFailed from err
        except CannotConnect as err:
            _LOGGER.error("Error during login: %s", err)
            raise UpdateFailed from err
        except ApiException as err:
            _LOGGER.error("Error during login: %s", err)
            raise

        accounts, service_addr_account_no = await self.api.async_get_accounts()

        try:
            _LOGGER.debug("API: async_get_forecast")
            forecast = await self.api.async_get_forecast()
        except CannotConnect as err:
            _LOGGER.error("Error getting forecast: %s", err)
            raise UpdateFailed from err
        except ApiException as err:
            _LOGGER.error("Error getting forecast: %s", err)
            raise

        _LOGGER.debug("Updating sensor data with: %s", forecast)

        # Because DominionSC provides historical usage with a delay of a couple of days
        # we need to insert data into statistics.
        last_changed_per_account = await self._insert_statistics(
            accounts, service_addr_account_no, forecast
        )

        # Build account-specific data dictionary
        account_data = {
            account: DominionSCAccountData(
                account=account,
                last_changed=last_changed_per_account.get(account),
            )
            for account in accounts
        }

        # Return combined struct with accounts and shared data
        return DominionSCData(
            accounts=account_data,
            forecast=forecast,
            service_addr_account_no=service_addr_account_no,
            last_updated=dt_util.utcnow(),
        )

    def _push_cost_statistics(
        self,
        cost_statistic_id: str,
        cost_stat_name: str,
        cost_statistics: list[StatisticData],
        operation_type: str,
        final_sum: float,
    ) -> None:
        """
        Build cost StatisticMetaData and submit rows to the HA recorder.

        Factored out to avoid code duplication between
        :meth:`_process_and_insert_statistics` (regular backfill/update) and
        :meth:`_async_recalculate_historic_costs_locked` (historic recalculation),
        which previously duplicated this block verbatim.

        Cost statistics use ``unit_class=None`` and
        ``unit_of_measurement=None`` because HA represents monetary values
        without a physical unit class in the statistics schema.

        Args:
            cost_statistic_id: Full HA statistic ID for the cost series.
            cost_stat_name:    Human-readable name for the series.
            cost_statistics:   Pre-built list of :class:`StatisticData` rows
                               (each with ``start``, ``state``, and ``sum``).
            operation_type:    Short label for log messages (``"backfill"``,
                               ``"update"``, or ``"recalculation"``).
            final_sum:         The running sum at the end of the batch, logged
                               for diagnostic purposes.

        """
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=cost_stat_name,
            source=DOMAIN,
            statistic_id=cost_statistic_id,
            unit_class=None,
            unit_of_measurement=None,
        )
        _LOGGER.info(
            "Adding %d hourly cost statistics for %s (%s, sum=%.4f)",
            len(cost_statistics),
            cost_statistic_id,
            operation_type,
            final_sum,
        )
        async_add_external_statistics(self.hass, cost_metadata, cost_statistics)

    async def _discover_registers(self, account: str) -> list[RegisterReads]:
        """
        Discover how many physical meter registers an account has.

        This is a one-time operation, called only for accounts that have never
        been backfilled. It fetches a short window of register-level data
        solely to count distinct ``usage_point_id`` values — it does not
        process or insert any of that data.

        Why a network call is safe here: this code path is only reached when
        ``get_last_statistics`` returns empty *and* ``_backfill_initiated`` is
        False, meaning this is a genuinely brand-new install or a first-time
        account. Established installs never reach this code (see
        :meth:`_insert_statistics` for the guard logic).

        Args:
            account: Account type (``"ELECTRIC"`` or ``"GAS"``).

        Returns:
            A list of :class:`~dominionsc.models.register_reads.RegisterReads`
            objects, one per physical meter register. Returns an empty list on
            any API failure, causing the caller to fall back to the legacy
            single-statistic path (safe default).

        """
        today = date.today()
        end_date = today - timedelta(days=1)
        start_date = end_date - timedelta(days=LOOKBACK_DAYS)
        tz = await dt_util.async_get_time_zone(self.api.get_timezone())
        start = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
        end = datetime.combine(end_date, datetime.min.time()).replace(tzinfo=tz)
        try:
            return await self.api.async_get_register_reads(account, start, end)
        except (CannotConnect, ApiException) as err:
            _LOGGER.debug(
                "Register discovery failed for %s, falling back to legacy path: %s",
                account,
                err,
            )
            return []

    async def _process_account(
        self,
        account: str,
        usage_point_id: str | None,
        consumption_statistic_id: str,
        cost_statistic_id: str | None,
        name_prefix: Template,
        last_changed_per_account: dict[str, datetime],
        forecast: Forecast | None,
    ) -> None:
        """
        Decide whether to backfill or incrementally update one statistic series.

        This method contains the backfill-vs-update decision logic in a single
        place so that both the sole-register (legacy) path and the multi-register
        (Phase 5) path in :meth:`_insert_statistics` share identical behaviour.

        Decision tree:
        - No existing statistics in recorder AND backfill not yet started
          -> start backfill, mark ``_backfill_initiated[backfill_key] = True``.
        - No existing statistics AND backfill already started
          -> skip (wait for recorder to commit the in-flight data).
        - Existing statistics found
          -> clear the backfill flag and perform an incremental update.

        The ``backfill_key`` is ``"{account}:{usage_point_id}"`` for multi-register
        accounts so two registers of the same account type (both ``"ELECTRIC"``)
        each get their own independent tracking flag.

        Args:
            account:                    Account type (``"ELECTRIC"`` or ``"GAS"``).
            usage_point_id:             ESPI UsagePoint ID for this register, or
                                        ``None`` for the legacy merged path.
            consumption_statistic_id:   HA statistic ID for energy consumption.
            cost_statistic_id:          HA statistic ID for cost, or ``None``.
            name_prefix:                Template for human-readable stat names.
            last_changed_per_account:   Mutable dict updated with the timestamp
                                        of the most recent data interval processed.
            forecast:                   Current billing forecast (used for backfill
                                        start date and billing-cycle estimation).

        """
        # Only track cost for electric accounts when a cost mode is active.
        cost_mode, _, _ = _resolve_cost_config(self.config_entry.options)
        if account != "ELECTRIC" or cost_mode == COST_MODE_NONE:
            cost_statistic_id = None

        _LOGGER.debug("Updating Statistics for %s", consumption_statistic_id)

        consumption_unit_class = (
            EnergyConverter.UNIT_CLASS
            if account == "ELECTRIC"
            else VolumeConverter.UNIT_CLASS
        )
        consumption_unit = (
            UnitOfEnergy.WATT_HOUR if account == "ELECTRIC" else UnitOfVolume.CUBIC_FEET
        )

        # Check if we have existing statistics
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            consumption_statistic_id,
            True,
            {"sum"},
        )

        consumption_exists = bool(last_stat.get(consumption_statistic_id))

        # Also check for cost statistics (for electric accounts)
        last_cost_stat = {}
        if cost_statistic_id:
            last_cost_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics, self.hass, 1, cost_statistic_id, True, {"sum"}
            )

        # Backfill-initiated tracking key: widened for a specific register so
        # two registers under the same measurement type (e.g. grid + solar,
        # both "ELECTRIC") don't share one flag. Unchanged (bare account) for
        # the legacy/sole-register case.
        backfill_key = f"{account}:{usage_point_id}" if usage_point_id else account

        if not consumption_exists:
            # No statistics - perform initial backfill
            if self._backfill_initiated.get(backfill_key, False):
                # Backfill was already started, waiting for recorder to commit
                _LOGGER.debug(
                    "Backfill already initiated for %s, waiting for recorder to commit",
                    consumption_statistic_id,
                )
                return

            _LOGGER.info(
                "First statistics update for %s - "
                "backfilling since last billing cycle.",
                consumption_statistic_id,
            )
            self._backfill_initiated[backfill_key] = True

        dominionsc_metadata = DominionSCStatisticMetadata(
            account=account,
            consumption_id=consumption_statistic_id,
            cost_id=cost_statistic_id,
            name_prefix=name_prefix,
            unit_class=consumption_unit_class,
            unit=consumption_unit,
            usage_point_id=usage_point_id,
        )

        if not consumption_exists:
            await self._backfill_statistics(
                dominionsc_metadata,
                last_changed_per_account,
                forecast,
            )
        else:
            # Statistics exist - perform incremental update
            self._backfill_initiated[backfill_key] = False
            _LOGGER.debug(
                "Found existing statistics for %s, performing incremental update",
                consumption_statistic_id,
            )

            await self._update_statistics(
                dominionsc_metadata,
                last_stat,
                last_cost_stat,
                last_changed_per_account,
                forecast,
            )

    async def _insert_statistics(
        self,
        accounts: list[str],
        service_addr_account_no: str,
        forecast: Forecast | None,
    ) -> dict[str, datetime]:
        """
        Orchestrate statistics insertion for all accounts on a service address.

        This is the main entry point for the statistics pipeline. It iterates
        over all account types (ELECTRIC, GAS) and routes each one through
        either the legacy single-statistic path or the multi-register (Phase 5)
        path.

        Statistic-ID stability guarantee (critical for existing installs)
        -----------------------------------------------------------------
        An account that already has statistics in the recorder (the
        ``legacy_last_stat`` check) is processed via the ORIGINAL path with
        **no register-discovery network call**. This guarantees that established
        installs' statistic IDs never change regardless of what register
        discovery might find, and Energy Dashboard history is never orphaned.

        Register discovery (a network call) only happens **once per account**,
        only for accounts that have **never** been backfilled:
        - 0 or 1 registers found -> use legacy IDs (identical to pre-Phase-5).
        - 2+ registers found (net-metered solar) -> use per-register IDs.

        After the first backfill the recorder will have statistics under the
        chosen ID, the ``legacy_last_stat`` check will be truthy on the next
        poll, and discovery will never be called again.

        Args:
            accounts:               List of account type strings (e.g.
                                    ``["ELECTRIC", "GAS"]``).
            service_addr_account_no: Service address / account number from API.
            forecast:               Current billing forecast, or ``None``.

        Returns:
            Dict mapping account type -> timestamp of most recent interval
            processed. Sensor entities use this for the ``last_changed`` value.

        """
        last_changed_per_account: dict[str, datetime] = {}
        for account in accounts:
            legacy_consumption_id, legacy_cost_id, legacy_name_prefix = (
                _build_statistic_ids(service_addr_account_no, account)
            )

            legacy_last_stat = await get_instance(self.hass).async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,
                legacy_consumption_id,
                True,
                {"sum"},
            )

            if legacy_last_stat.get(
                legacy_consumption_id
            ) or self._backfill_initiated.get(account, False):
                # Established install, or a legacy backfill already started
                # this cycle and we're waiting on the recorder to commit --
                # proceed exactly as pre-Phase-5, no discovery call.
                await self._process_account(
                    account=account,
                    usage_point_id=None,
                    consumption_statistic_id=legacy_consumption_id,
                    cost_statistic_id=legacy_cost_id,
                    name_prefix=legacy_name_prefix,
                    last_changed_per_account=last_changed_per_account,
                    forecast=forecast,
                )
                continue

            # No legacy statistic, no backfill in flight: this account has
            # never been backfilled. Discover registers once to decide the
            # statistic-id scheme going forward.
            registers = await self._discover_registers(account)

            if len(registers) <= 1:
                await self._process_account(
                    account=account,
                    usage_point_id=None,
                    consumption_statistic_id=legacy_consumption_id,
                    cost_statistic_id=legacy_cost_id,
                    name_prefix=legacy_name_prefix,
                    last_changed_per_account=last_changed_per_account,
                    forecast=forecast,
                )
            else:
                _LOGGER.info(
                    "Account %s has %d meter registers; tracking separately: %s",
                    account,
                    len(registers),
                    [r.usage_point_id for r in registers],
                )
                for register in registers:
                    consumption_statistic_id, cost_statistic_id, name_prefix = (
                        _build_register_statistic_ids(
                            service_addr_account_no,
                            account,
                            usage_point_id=register.usage_point_id,
                            is_sole_register=False,
                        )
                    )
                    await self._process_account(
                        account=account,
                        usage_point_id=register.usage_point_id,
                        consumption_statistic_id=consumption_statistic_id,
                        cost_statistic_id=cost_statistic_id,
                        name_prefix=name_prefix,
                        last_changed_per_account=last_changed_per_account,
                        forecast=forecast,
                    )

        return last_changed_per_account

    async def _backfill_statistics(
        self,
        metadata: DominionSCStatisticMetadata,
        last_changed_per_account: dict[str, datetime],
        forecast: Forecast | None,
    ) -> None:
        """
        Load historical statistics on first setup (initial backfill).

        Determines the fetch window based on the user's backfill preferences
        in ``config_entry.options``:

        - **No extended backfill** (default): fetch from the billing-cycle
          start date provided by the API forecast to yesterday.
        - **Extended backfill** (``CONF_EXTENDED_BACKFILL=True``): fetch from
          365 days ago to yesterday.
        - **Extended cost backfill** (``CONF_EXTENDED_COST_BACKFILL=True``,
          requires extended backfill): also calculate cost for the full 365-day
          window. Without this flag, cost is only calculated from the current
          billing-cycle start even when consumption goes further back.

        The ``cost_start_date`` parameter limits cost calculation without
        limiting consumption fetching — users can have full consumption history
        while only computing costs from the point where the current rate
        schedule applies.

        After determining the date range, delegates to
        :meth:`_process_and_insert_statistics` with ``consumption_sum=0.0``
        (starting from scratch) and ``last_stat_dt=None`` (no prior data).

        Args:
            metadata:                  Statistic metadata for this account/register.
            last_changed_per_account:  Mutable dict updated with the latest interval
                                       timestamp after insert.
            forecast:                  Current billing forecast. ``start_date`` is
                                       used as the non-extended backfill start and as
                                       the cost-start gate when only consumption is
                                       extended.

        """
        today = date.today()
        billing_cycle_start = forecast.start_date
        data_date = today - timedelta(days=1)  # Yesterday

        extended = self.config_entry.options.get(CONF_EXTENDED_BACKFILL, False)
        extended_cost = self.config_entry.options.get(
            CONF_EXTENDED_COST_BACKFILL, False
        )

        if extended:
            start_date = today - timedelta(days=EXTENDED_BACKFILL_DAYS)
        else:
            start_date = billing_cycle_start

        # When consumption is extended but cost is not, limit cost to the
        # current billing cycle.  When both are extended (or neither is),
        # cost_start_date is None which means no additional restriction.
        cost_start_date: date | None = None
        if extended and not extended_cost:
            cost_start_date = billing_cycle_start

        _LOGGER.debug(
            "Backfilling statistics from %s to %s "
            "(extended=%s, extended_cost=%s, billing_cycle_start=%s)",
            start_date,
            data_date,
            extended,
            extended_cost,
            billing_cycle_start,
        )

        # Call the common processing function with initial values
        await self._process_and_insert_statistics(
            metadata=metadata,
            start_date=start_date,
            data_date=data_date,
            consumption_sum=0.0,  # Start from 0 for backfill
            cost_sum=0.0,  # Start from 0 for backfill
            last_stat_dt=None,  # No previous stat for backfill
            last_changed_per_account=last_changed_per_account,
            forecast=forecast,
            cost_start_date=cost_start_date,
        )

    async def _update_statistics(
        self,
        metadata: DominionSCStatisticMetadata,
        last_stat: dict,
        last_cost_stat: dict,
        last_changed_per_account: dict[str, datetime],
        forecast: Forecast | None,
    ) -> None:
        """
        Incrementally update statistics since the last recorded data point.

        Called when statistics already exist in the recorder (regular 12-hour
        polls after the initial backfill). The strategy is:

        1. Determine the last recorded statistic's timestamp and running sum.
        2. Compute a "lookback window" that extends ``LOOKBACK_DAYS`` before
           yesterday. This catches late-arriving API data (intervals the API
           delivered a day or two after the fact).
        3. Query the recorder for all existing statistic rows in that window
           so we can skip already-recorded hours while still contributing their
           Wh to the cumulative counter for tier accuracy.
        4. If the window contains only fully-covered dates AND there are no new
           days since the last statistic, skip the API call entirely.
        5. Otherwise, fetch usage data from the API and delegate to
           :meth:`_process_and_insert_statistics`.

        The existing ``consumption_sum`` and ``cost_sum`` from the last
        recorder row are passed through so the new statistics rows continue the
        cumulative sum from where the recorder left off.

        Args:
            metadata:                 Statistic metadata for this account/register.
            last_stat:                ``get_last_statistics`` result dict keyed by
                                      ``metadata.consumption_id``.
            last_cost_stat:           ``get_last_statistics`` result for the cost
                                      statistic, or an empty dict if none exists.
            last_changed_per_account: Mutable dict updated with the latest interval
                                      timestamp.
            forecast:                 Current billing forecast. ``start_date`` is
                                      used to clamp the lookback window to data
                                      the API actually has.

        """
        try:
            # Get the last recorded statistic time and sum
            last_stat_data = last_stat[metadata.consumption_id][0]
            last_stat_start = last_stat_data["start"]
            consumption_sum = float(last_stat_data.get("sum") or 0)

            _LOGGER.debug(
                "Last statistic for %s: start=%s (type=%s), sum=%.3f",
                metadata.consumption_id,
                last_stat_start,
                type(last_stat_start).__name__,
                consumption_sum,
            )

            # Convert to datetime for comparison
            if isinstance(last_stat_start, (int, float)):
                last_stat_dt = datetime.fromtimestamp(last_stat_start, tz=dt_util.UTC)
            else:
                last_stat_dt = last_stat_start

            # Convert to local timezone for date comparison
            local_tz = dt_util.get_default_time_zone()
            last_stat_local = last_stat_dt.astimezone(local_tz)
            last_stat_date = last_stat_local.date()

        except (KeyError, IndexError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Error parsing last statistic for %s: %s (last_stat=%s)",
                metadata.consumption_id,
                err,
                last_stat,
            )
            return

        # Get the last cost sum (default to 0 if cost stats don't exist yet)
        cost_sum = 0.0
        if metadata.cost_id:
            try:
                cost_sum = float(last_cost_stat[metadata.cost_id][0].get("sum") or 0)
            except (KeyError, IndexError, TypeError, ValueError) as err:
                _LOGGER.debug(
                    "WARNING: Unable to get cost_sum: %s, %s, %s",
                    str(last_cost_stat),
                    metadata.cost_id,
                    err,
                )

        # Determine the date range to fetch
        today = date.today()
        data_date = today - timedelta(
            days=1
        )  # Yesterday is the most recent complete day

        _LOGGER.debug(
            "Date comparison: last_stat_date=%s, data_date=%s",
            last_stat_date,
            data_date,
        )

        # ── Determine the lookback window for gap detection ──────────────
        # Look back LOOKBACK_DAYS from yesterday to catch late-arriving data
        # from the API (e.g. a day that was skipped and reported later).
        oldest_available = forecast.start_date
        lookback_date = max(data_date - timedelta(days=LOOKBACK_DAYS), oldest_available)

        # The fetch window covers whichever is earlier: the day after the
        # last stat (for new data) or the lookback date (for gap filling).
        start_date = min(last_stat_date + timedelta(days=1), lookback_date)
        if start_date < oldest_available:
            _LOGGER.warning(
                "Statistics are very stale (last: %s). "
                "Limiting fetch to last billing cycle. "
                "Some historical data may be lost.",
                last_stat_date,
            )
            start_date = oldest_available

        # ── Query recorder for existing statistics in the window ─────────
        # We need this both to detect gaps and to pass to the processing
        # function so it can skip already-recorded hours.
        tz = await dt_util.async_get_time_zone(self.api.get_timezone())
        lookback_start_dt = datetime.combine(start_date, datetime.min.time()).replace(
            tzinfo=tz
        )
        lookback_end_dt = datetime.combine(
            data_date + timedelta(days=1), datetime.min.time()
        ).replace(tzinfo=tz)

        existing_rows: list[dict] = (
            await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                lookback_start_dt,
                lookback_end_dt,
                {metadata.consumption_id},
                "hour",
                None,
                {"start"},
            )
        ).get(metadata.consumption_id, [])

        existing_hours: set[datetime] = set()
        for row in existing_rows:
            ts = row["start"]
            if isinstance(ts, (int, float)):
                existing_hours.add(datetime.fromtimestamp(ts, tz=dt_util.UTC))
            else:
                existing_hours.add(ts)

        # ── Check whether there is anything to fetch ─────────────────────
        # Build the set of dates we expect to have data for in the window.
        expected_dates: set[date] = set()
        d = start_date
        while d <= data_date:
            expected_dates.add(d)
            d += timedelta(days=1)

        # Dates already fully covered in the recorder (at least 1 hour present).
        covered_dates: set[date] = set()
        for h in existing_hours:
            local_h = h.astimezone(tz) if tz else h
            covered_dates.add(local_h.date())

        missing_dates = expected_dates - covered_dates
        has_new_days = last_stat_date < data_date

        if not has_new_days and not missing_dates:
            _LOGGER.debug(
                "Statistics up to date and no gaps found in lookback window "
                "(%s to %s, %d hours recorded)",
                start_date,
                data_date,
                len(existing_hours),
            )
            last_changed_per_account[metadata.account] = last_stat_dt
            return

        if missing_dates:
            _LOGGER.info(
                "Detected %d missing date(s) in lookback window: %s",
                len(missing_dates),
                sorted(missing_dates),
            )

        _LOGGER.info(
            "Fetching statistics update from %s to %s "
            "(lookback=%d days, existing_hours=%d, missing_dates=%d, "
            "consumption_sum=%.3f%s)",
            start_date,
            data_date,
            (data_date - start_date).days,
            len(existing_hours),
            len(missing_dates),
            consumption_sum,
            f", cost_sum={cost_sum:.3f}" if metadata.cost_id else "",
        )

        # Call the common processing function, passing existing hours
        # so it can skip already-recorded intervals.
        await self._process_and_insert_statistics(
            metadata=metadata,
            start_date=start_date,
            data_date=data_date,
            consumption_sum=consumption_sum,
            cost_sum=cost_sum,
            last_stat_dt=last_stat_dt,
            last_changed_per_account=last_changed_per_account,
            forecast=forecast,
            existing_hours=existing_hours,
        )

    def _aggregate_hourly_data(
        self,
        usage_reads: list,
        metadata: DominionSCStatisticMetadata,
        forecast: Forecast | None,
        start_date: date,
        is_electric: bool,
        cost_start_date: date | None = None,
        existing_hours: set[datetime] | None = None,
    ) -> tuple[dict[datetime, float], dict[datetime, float]]:
        """
        Resolve cost config and delegate to the pure aggregation function.

        This is a thin coordinator-method wrapper around the pure
        :func:`~.aggregation.aggregate_hourly_data` function. It exists so that:

        1. The pure function has no dependency on the coordinator or HA.
        2. Tests that call ``coordinator._aggregate_hourly_data(...)`` or patch
           it (e.g. to inject mock data) are unaffected by the module split.

        See :func:`~.aggregation.aggregate_hourly_data` for the full parameter
        and return-value documentation.

        Args:
            usage_reads:      Raw interval objects from the Dominion API.
            metadata:         Statistic metadata for this account/register.
            forecast:         Current billing forecast.
            start_date:       Earliest date of the fetch window.
            is_electric:      ``True`` for ELECTRIC accounts.
            cost_start_date:  Optional gate for cost calculation start date.
            existing_hours:   Hours already present in the recorder (update path).

        Returns:
            ``(hourly_consumption, hourly_cost)`` dicts keyed by hour start.

        """
        cost_mode_here, fixed_rate_here, rate_schedule_here = _resolve_cost_config(
            self.config_entry.options
        )
        is_tiered_rate = cost_mode_here in TIERED_RATE_REGISTRY
        return aggregate_hourly_data(
            usage_reads=usage_reads,
            metadata=metadata,
            forecast=forecast,
            start_date=start_date,
            is_electric=is_electric,
            cost_mode=cost_mode_here,
            fixed_rate=fixed_rate_here,
            rate_schedule=rate_schedule_here,
            is_tiered_rate=is_tiered_rate,
            cost_start_date=cost_start_date,
            existing_hours=existing_hours,
        )

    async def _process_and_insert_statistics(
        self,
        metadata: DominionSCStatisticMetadata,
        start_date: date,
        data_date: date,
        consumption_sum: float,
        cost_sum: float,
        last_stat_dt: datetime | None,
        last_changed_per_account: dict[str, datetime],
        forecast: Forecast | None,
        cost_start_date: date | None = None,
        existing_hours: set[datetime] | None = None,
    ) -> None:
        """
        Fetch usage data from the API, aggregate it, and write to the recorder.

        This is the common implementation shared by both the backfill path and
        the incremental-update path. It:

        1. Converts date bounds to timezone-aware datetimes for the API call.
        2. Fetches usage reads — either flat (legacy/sole-register path via
           ``async_get_usage_reads``) or register-specific (multi-register path
           via ``async_get_register_reads``, filtered to this metadata's
           ``usage_point_id``).
        3. Filters out zero-consumption days. The API sometimes returns zeros
           for days where data hasn't been processed yet (e.g. holidays or
           very recent dates). Including zeros would insert false "no usage"
           records that are difficult to correct later.
        4. Aggregates raw intervals into hourly buckets using
           :meth:`_aggregate_hourly_data`.
        5. Builds cumulative ``StatisticData`` rows (each row's ``sum`` includes
           all preceding rows) and submits them to the HA recorder via
           ``async_add_external_statistics``.

        The caller passes ``consumption_sum`` and ``cost_sum`` from the last
        existing statistic row so that new rows continue the cumulative sum
        series seamlessly. For the backfill path both are ``0.0``.

        Args:
            metadata:                 Statistic metadata (IDs, units, name prefix).
            start_date:               First day of the API fetch window (inclusive).
            data_date:                Last day of the fetch window (inclusive;
                                      typically yesterday — today's data is incomplete).
            consumption_sum:          Running sum of Wh at the start of this batch.
                                      ``0.0`` for initial backfill.
            cost_sum:                 Running sum of USD at the start of this batch.
                                      ``0.0`` for initial backfill.
            last_stat_dt:             Datetime of the most recent existing statistic
                                      row, or ``None`` for initial backfill. Used to
                                      populate ``last_changed_per_account`` when there
                                      is nothing new to insert.
            last_changed_per_account: Mutable dict updated with the timestamp of the
                                      last interval processed this run.
            forecast:                 Current billing forecast. Required for tiered-rate
                                      billing-cycle estimation.
            cost_start_date:          If set, cost rows are only produced for intervals
                                      on or after this date (extended consumption but
                                      not extended cost backfill).
            existing_hours:           Set of hour-start datetimes already in the
                                      recorder. Skipped in output but still contribute
                                      to cumulative Wh for tier accuracy.

        """
        # Convert dates to datetimes for API call
        tz = await dt_util.async_get_time_zone(self.api.get_timezone())
        start = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=tz)
        end = datetime.combine(data_date, datetime.min.time()).replace(tzinfo=tz)

        # Track the initial sum to determine if this is a backfill or update
        initial_sum = consumption_sum

        try:
            if metadata.usage_point_id is None:
                # Legacy / sole-register path: flat, merged usage reads --
                # unchanged from pre-Phase-5 behavior.
                _LOGGER.debug("API: async_get_usage_reads")
                usage_reads = await self.api.async_get_usage_reads(
                    metadata.account, start, end
                )
            else:
                # Multi-register path: fetch grouped by register and select
                # only this metadata's register. A register with no data in
                # this window (e.g. newly discovered) yields an empty list
                # rather than an error.
                _LOGGER.debug(
                    "API: async_get_register_reads (register=%s)",
                    metadata.usage_point_id,
                )
                registers = await self.api.async_get_register_reads(
                    metadata.account, start, end
                )
                matching_register = next(
                    (
                        r
                        for r in registers
                        if r.usage_point_id == metadata.usage_point_id
                    ),
                    None,
                )
                usage_reads = matching_register.reads if matching_register else []
        except CannotConnect as err:
            _LOGGER.warning("Could not fetch statistics data: %s", err)
            return
        except ApiException as err:
            _LOGGER.warning("Could not fetch statistics data: %s", err)
            return

        if not usage_reads:
            _LOGGER.debug(
                "No interval data for statistics (requested %s to %s). "
                "API may not have data available yet.",
                start_date,
                data_date,
            )
            # Set last_changed to the last statistic time if we have one
            if last_stat_dt:
                last_changed_per_account[metadata.account] = last_stat_dt
            return

        # Calculate daily totals to identify zero-consumption days (API may return
        # zeros when data isn't available yet, e.g., during holidays)
        daily_totals: dict[date, float] = {}
        for usage_read in usage_reads:
            d = usage_read.start_time.date()
            daily_totals.setdefault(d, 0.0)
            daily_totals[d] += usage_read.consumption

        # Filter out intervals from zero-consumption days
        zero_days = {d for d, total in daily_totals.items() if total == 0}
        if zero_days:
            _LOGGER.warning(
                "Skipping %d days with zero consumption (data not yet available): %s",
                len(zero_days),
                sorted(zero_days),
            )
            usage_reads = [
                i for i in usage_reads if i.start_time.date() not in zero_days
            ]

        if not usage_reads:
            _LOGGER.debug("No valid interval data after filtering zero days")
            # Set last_changed to the last statistic time if we have one
            if last_stat_dt:
                last_changed_per_account[metadata.account] = last_stat_dt
            return

        _LOGGER.debug("Received %d intervals for statistics", len(usage_reads))

        # Aggregate intervals into hourly buckets for consumption and cost
        is_electric = metadata.account == "ELECTRIC"
        hourly_consumption, hourly_cost = self._aggregate_hourly_data(
            usage_reads=usage_reads,
            metadata=metadata,
            forecast=forecast,
            start_date=start_date,
            is_electric=is_electric,
            cost_start_date=cost_start_date,
            existing_hours=existing_hours,
        )

        # Build statistics with cumulative sums
        consumption_statistics: list[StatisticData] = []
        cost_statistics: list[StatisticData] = []

        for hour_start in sorted(hourly_consumption.keys()):
            consumption = hourly_consumption[hour_start]
            consumption_sum += consumption
            consumption_statistics.append(
                StatisticData(start=hour_start, state=consumption, sum=consumption_sum)
            )

        if hourly_cost:
            for hour_start in sorted(hourly_cost.keys()):
                cost = hourly_cost[hour_start]
                if cost > 0:
                    cost_sum += cost
                    cost_statistics.append(
                        StatisticData(start=hour_start, state=cost, sum=cost_sum)
                    )

        if not consumption_statistics:
            _LOGGER.debug(
                "No new statistics to insert for %s "
                "(all hours may already be recorded)",
                metadata.consumption_id,
            )
            if last_stat_dt:
                last_changed_per_account[metadata.account] = last_stat_dt
            return

        if existing_hours:
            _LOGGER.info(
                "Gap-fill: inserting %d newly-available hours for %s",
                len(consumption_statistics),
                metadata.consumption_id,
            )

        # Update last_changed with the latest interval time
        last_changed_per_account[metadata.account] = usage_reads[-1].end_time

        # Create metadata for consumption
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=metadata.name_prefix.substitute(stat_type="consumption"),
            source=DOMAIN,
            statistic_id=metadata.consumption_id,
            unit_class=metadata.unit_class,
            unit_of_measurement=metadata.unit,
        )

        operation_type = "backfill" if initial_sum == 0.0 else "update"
        _LOGGER.info(
            "Adding %d hourly statistics for %s (%s, sum=%.3f)",
            len(consumption_statistics),
            metadata.consumption_id,
            operation_type,
            consumption_sum,
        )
        async_add_external_statistics(
            self.hass, consumption_metadata, consumption_statistics
        )

        # Add cost statistics for electric accounts
        if is_electric and metadata.cost_id and cost_statistics:
            self._push_cost_statistics(
                metadata.cost_id,
                metadata.name_prefix.substitute(stat_type="cost"),
                cost_statistics,
                operation_type,
                cost_sum,
            )

    async def async_recalculate_historic_costs(
        self,
        start_date: date,
        end_date: date,
        new_options: dict[str, Any],
    ) -> None:
        """
        Recalculate cost statistics for a historic date range under a new rate.

        Public entry point, called as an ``asyncio`` task by the options flow
        (non-blocking). Acquires :attr:`recalculation_lock` for the full
        duration so :meth:`~.options_flow.DominionSCOptionsFlow.async_step_init`
        can detect a running recalculation and block a second one from starting.

        The actual work is delegated to
        :meth:`_async_recalculate_historic_costs_locked`.

        Args:
            start_date:  First day of the recalculation window (inclusive).
            end_date:    Last day of the window (inclusive; must not be in future).
            new_options: The new options dict (from the options flow). The new
                         cost mode and rate are read from this, not from the
                         config entry (which may not have been saved yet).

        """
        async with self.recalculation_lock:
            await self._async_recalculate_historic_costs_locked(
                start_date, end_date, new_options
            )

    async def _async_recalculate_historic_costs_locked(
        self,
        start_date: date,
        end_date: date,
        new_options: dict[str, Any],
    ) -> None:
        """
        Re-price stored consumption rows and upsert cost statistics.

        Called while :attr:`recalculation_lock` is held. Works in six stages:

        1. **Resolve config**: extract cost mode, fixed rate, and rate schedule
           from ``new_options``. Exit early if mode is ``COST_MODE_NONE``.
        2. **Estimate billing cycles** (tiered rates only): needed to reset the
           cumulative Wh counter at cycle boundaries for accurate tier splits.
        3. **Fetch consumption rows** from the recorder. For tiered rates, the
           fetch window extends back to the start of the earliest estimated
           billing cycle (which may be before ``start_date``) so that the Wh
           counter is seeded correctly even for mid-cycle window starts.
        4. **Seed the running cost sum** from the last cost statistic row that
           precedes the window. This keeps the recalculated series continuous
           with any pre-existing rows outside the window.
        5. **Price each consumption row**, resetting ``cumulative_wh`` at
           billing-cycle boundaries. Only rows within the requested window are
           emitted to the output; earlier rows are used only for Wh seeding.
        6. **Upsert** via :meth:`_push_cost_statistics`.

        Note: this only recalculates the ELECTRIC cost statistic. Gas accounts
        have no cost statistic.

        Args:
            start_date:  First day of the recalculation window (inclusive).
            end_date:    Last day of the window (inclusive).
            new_options: Options dict containing the new cost mode and rate.

        """
        new_cost_mode, new_fixed_rate, rate_schedule = _resolve_cost_config(new_options)
        if new_cost_mode == COST_MODE_NONE:
            _LOGGER.info("New cost mode is NONE; skipping recalculation.")
            return

        is_tiered = new_cost_mode in TIERED_RATE_REGISTRY

        _LOGGER.info(
            "Starting historic cost recalculation from %s to %s (mode: %s)",
            start_date,
            end_date,
            new_cost_mode,
        )

        tz = await dt_util.async_get_time_zone(self.api.get_timezone())

        def _to_dt(d: date) -> datetime:
            """Convert a local calendar date to the recorder query timezone."""
            return datetime.combine(d, datetime.min.time()).replace(tzinfo=tz)

        window_start = _to_dt(start_date)
        window_end = _to_dt(end_date + timedelta(days=1))

        # ── 1. Resolve statistic IDs ──────────────────────────────────────────────
        accounts, service_addr_account_no = await self.api.async_get_accounts()
        if "ELECTRIC" not in accounts:
            _LOGGER.info("No ELECTRIC account found; nothing to recalculate.")
            return

        consumption_id, cost_id, name_prefix = _build_statistic_ids(
            service_addr_account_no, "ELECTRIC"
        )
        assert cost_id is not None

        # ── 2. Estimate billing cycles (tiered rates only) ────────────────────────
        billing_cycles: list[tuple[date, date]] = []
        if is_tiered:
            forecast = await self.api.async_get_forecast()
            billing_cycles = _estimate_billing_cycles(
                forecast.start_date,
                forecast.end_date,
                start_date,
                latest=end_date,
            )

        # ── 3. Fetch consumption rows ─────────────────────────────────────────────
        # For tiered rates, start from the earliest estimated cycle so the
        # cumulative Wh counter is correct even when the window is mid-cycle.
        fetch_start = window_start
        if billing_cycles:
            fetch_start = min(fetch_start, _to_dt(billing_cycles[0][0]))

        consumption_rows: list[dict] = (
            await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                fetch_start,
                window_end,
                {consumption_id},
                "hour",
                None,
                {"state"},
            )
        ).get(consumption_id, [])

        if not consumption_rows:
            _LOGGER.warning("No consumption data in %s-%s.", start_date, end_date)
            return

        # ── 4. Seed running cost sum from last record before the window ───────────
        pre_cost_sum = 0.0
        last_cost = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            cost_id,
            True,
            {"sum"},
        )
        if cost_id in last_cost:
            last_ts = last_cost[cost_id][0].get("start", 0)
            if isinstance(last_ts, (int, float)):
                last_ts_dt = datetime.fromtimestamp(last_ts, tz=dt_util.UTC)
            else:
                last_ts_dt = last_ts
            if last_ts_dt < window_start:
                pre_cost_sum = float(last_cost[cost_id][0].get("sum") or 0.0)

        # ── 5. Price each row, resetting cumulative Wh at cycle boundaries ────────
        cost_statistics: list[StatisticData] = []
        running_sum = pre_cost_sum
        current_cycle: tuple[date, date] | None = None
        cumulative_wh = 0.0

        for row in consumption_rows:
            hour_dt = datetime.fromtimestamp(row["start"], tz=dt_util.UTC)
            interval_wh = float(row.get("state") or 0.0)
            row_date = hour_dt.astimezone(tz).date() if tz else hour_dt.date()

            # Reset cumulative Wh at billing cycle boundaries
            if is_tiered and billing_cycles:
                row_cycle = _find_billing_cycle_for_date(row_date, billing_cycles)
                if row_cycle != current_cycle:
                    current_cycle = row_cycle
                    cumulative_wh = 0.0

            cost = _calculate_cost_for_wh(
                interval_wh,
                hour_dt,
                cumulative_wh,
                new_cost_mode,
                new_fixed_rate,
                rate_schedule,
            )
            cumulative_wh += interval_wh

            # Only emit rows within the requested window (earlier rows are
            # only fetched to seed the cumulative Wh counter for mid-cycle starts)
            if cost > 0 and row_date >= start_date:
                running_sum += cost
                cost_statistics.append(
                    StatisticData(start=hour_dt, state=cost, sum=running_sum)
                )

        if not cost_statistics:
            _LOGGER.warning(
                "Recalculation produced no cost data for %s-%s.", start_date, end_date
            )
            return

        # ── 6. Upsert into recorder ──────────────────────────────────────────────
        self._push_cost_statistics(
            cost_id,
            name_prefix.substitute(stat_type="cost"),
            cost_statistics,
            "recalculation",
            running_sum,
        )
        _LOGGER.info("Historic cost recalculation complete.")
