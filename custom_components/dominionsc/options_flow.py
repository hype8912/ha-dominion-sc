"""
Options flow for the dominionsc integration.

The options flow lets the user change cost settings after initial setup,
without needing to remove and re-add the integration.

Flow steps
----------
::

    init  (select cost mode)
     └── COST_MODE_NONE ──────────────────────────────────────────► SAVE (no recalc)
     └── COST_MODE_FIXED ──► fixed_rate ──► recalculate_history ──► (see below)
     └── COST_MODE_RATE_* ──────────────► recalculate_history ──► (see below)

    recalculate_history  (ask yes/no)
     └── No  ────────────────────────────────────────────────────► SAVE
     └── Yes ──► recalculate_date_range ──────────────────────────► SAVE + fire task

When the user confirms a date range, a background
:meth:`~.coordinator.DominionSCCoordinator.async_recalculate_historic_costs`
task is created (non-blocking) and the options are saved immediately. The
recalculation runs asynchronously and the Energy Dashboard updates once it
completes.

Concurrency guard
-----------------
If a recalculation is already running (``coordinator.recalculation_lock`` is
held), the ``init`` step shows a blocking error rather than allowing a second
recalculation to start. This prevents two concurrent recalculations from
producing duplicate or out-of-order statistics rows.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import voluptuous as vol
from dominionsc.const import BIDGELY_PILOT_ID
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import StatisticsRow, statistics_during_period
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    CONF_GAS_COST_MODE,
    CONF_PILOT_ID,
    CONF_SERVICE_ADDR,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
    DOMAIN,
)
from .rates import (
    RATE_PLAN_REGISTRY,
    build_cost_mode_choices,
    build_gas_cost_mode_choices,
)
from .statistics_ids import _build_statistic_ids

CONF_RECALCULATE_HISTORY = "recalculate_history"
CONF_RECALC_START_DATE = "recalc_start_date"
CONF_RECALC_END_DATE = "recalc_end_date"

# Built once at module level so the *_cost_mode_label functions don't rebuild
# their lookup dict on every call.
_COST_MODE_LABELS: dict[str, str] = {}
_GAS_COST_MODE_LABELS: dict[str, str] = {}


def _cost_mode_label(mode: str) -> str:
    """
    Return a human-readable label for an electric ``COST_MODE_*`` constant.

    Used to populate the ``description_placeholders`` in the
    ``recalculate_history`` form so the user can see which rate they are
    switching from and to.  Labels are taken from :func:`build_cost_mode_choices`
    so the text always matches what the user saw in the dropdown.

    Args:
        mode: A ``COST_MODE_*`` string constant (e.g. ``"rate_8"``).

    Returns:
        The dropdown label for the mode, or the raw mode string as a fallback.

    """
    if not _COST_MODE_LABELS:
        _COST_MODE_LABELS.update(build_cost_mode_choices())
    return _COST_MODE_LABELS.get(mode, mode)


def _gas_cost_mode_label(mode: str) -> str:
    """
    Return a human-readable label for a gas ``COST_MODE_*`` constant.

    Analogous to :func:`_cost_mode_label` but for gas modes, whose labels
    (e.g. "Rate 32V - Gas Residential Value Service") don't appear in
    :func:`build_cost_mode_choices`'s electric-only mapping.

    Args:
        mode: A ``COST_MODE_*`` string constant (e.g. ``"rate_32v"``) or
              ``COST_MODE_NONE``.

    Returns:
        The dropdown label for the mode, or the raw mode string as a fallback.

    """
    if not _GAS_COST_MODE_LABELS:
        _GAS_COST_MODE_LABELS.update(build_gas_cost_mode_choices())
    return _GAS_COST_MODE_LABELS.get(mode, mode)


class DominionSCOptionsFlow(OptionsFlow):
    """
    Handle post-install options for the Dominion Energy SC integration.

    Accessed via the "Configure" button on the integration card in the UI.
    Allows the user to:
    - Switch between cost calculation modes (Rate 8, Rate 6, Fixed, None).
    - Enter a custom $/kWh rate for fixed-rate mode.
    - Optionally recalculate historical cost statistics for a chosen date range
      using the new rate.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """
        Initialize the options flow.

        Args:
            config_entry: The existing config entry being configured. Its
                          current ``options`` dict is used to pre-fill form
                          defaults so the user sees their current settings.

        """
        self._config_entry = config_entry
        # Stores the mode chosen in async_step_init (needed by later steps).
        self._selected_mode: str | None = None
        # Accumulates the new options to be saved when the flow completes.
        self._new_options: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """
        Step 1: Select cost calculation mode.

        Entry point for the options flow. Pre-fills the dropdown with the
        currently stored cost mode.

        On submission:
        - ``COST_MODE_NONE`` -> saves immediately (no further steps needed).
        - ``COST_MODE_FIXED`` -> continues to ``fixed_rate``.
        - Tiered mode (``COST_MODE_RATE_*``) -> continues to
          ``recalculate_history``.

        When the account has a GAS meter (detected from coordinator data), an
        additional gas rate selector is shown in the same step.

        Blocks all changes (with an error message) if a background
        recalculation is currently running to prevent concurrent writes to the
        statistics table.
        """
        # Guard: block changes while a background recalculation is running.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        if coordinator is not None and coordinator.recalculation_lock.locked():
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors={"base": "recalculation_in_progress"},
            )

        # Determine if a GAS account is present (only possible after first poll).
        has_gas = coordinator is not None and coordinator.data is not None and "GAS" in coordinator.data.accounts

        if user_input is not None:
            self._selected_mode = user_input[CONF_COST_MODE]

            # Always capture gas cost mode when GAS is present.
            if has_gas:
                self._new_options[CONF_GAS_COST_MODE] = user_input.get(CONF_GAS_COST_MODE, COST_MODE_NONE)

            # pilot_id lives in entry.data (it's a connection parameter for the
            # API client, like credentials) rather than entry.options. Update
            # it directly and reload the entry so the coordinator rebuilds its
            # DominionSC client with the new value -- unlike cost-mode options,
            # a plain async_request_refresh() would keep using the stale client.
            new_pilot_id = user_input.get(CONF_PILOT_ID, BIDGELY_PILOT_ID)
            if new_pilot_id != self._config_entry.data.get(CONF_PILOT_ID, BIDGELY_PILOT_ID):
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, CONF_PILOT_ID: new_pilot_id},
                )
                self.hass.config_entries.async_schedule_reload(self._config_entry.entry_id)

            if self._selected_mode == COST_MODE_FIXED:
                return await self.async_step_fixed_rate()

            # Offer recalculation if either commodity has a priced mode
            # selected. Gating this on electric alone would skip the prompt
            # entirely for a user whose electric mode is None but who just
            # turned on a gas rate plan -- their existing gas consumption
            # would then never get priced (see coordinator.py's
            # _async_recalculate_historic_costs_locked docstring).
            gas_mode_selected = self._new_options.get(CONF_GAS_COST_MODE, COST_MODE_NONE)
            if self._selected_mode in RATE_PLAN_REGISTRY or gas_mode_selected != COST_MODE_NONE:
                self._new_options[CONF_COST_MODE] = self._selected_mode
                return await self.async_step_recalculate_history()
            # Neither commodity has a priced mode - nothing to recalculate.
            return self.async_create_entry(title="", data={CONF_COST_MODE: COST_MODE_NONE, **self._new_options})

        current_options = self._config_entry.options
        mode_choices = build_cost_mode_choices()

        schema_dict = {
            vol.Required(
                CONF_COST_MODE,
                default=current_options.get(CONF_COST_MODE, COST_MODE_RATE_8),
            ): vol.In(mode_choices),
            vol.Optional(
                CONF_PILOT_ID,
                default=self._config_entry.data.get(CONF_PILOT_ID, BIDGELY_PILOT_ID),
            ): str,
        }
        if has_gas:
            gas_choices = build_gas_cost_mode_choices()
            schema_dict[
                vol.Required(
                    CONF_GAS_COST_MODE,
                    default=current_options.get(CONF_GAS_COST_MODE, COST_MODE_NONE),
                )
            ] = vol.In(gas_choices)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_fixed_rate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """
        Step 2a: Collect the custom $/kWh rate for fixed-rate mode.

        Only reached when the user selected ``COST_MODE_FIXED`` in
        ``async_step_init``. Pre-fills the current stored fixed rate (or the
        default if none is stored). After submission, proceeds to
        ``recalculate_history`` so the user can apply the new rate to history.
        """
        if user_input is not None:
            self._new_options[CONF_COST_MODE] = COST_MODE_FIXED
            self._new_options[CONF_FIXED_RATE] = user_input[CONF_FIXED_RATE]
            return await self.async_step_recalculate_history()

        current_options = self._config_entry.options

        return self.async_show_form(
            step_id="fixed_rate",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FIXED_RATE,
                        default=current_options.get(CONF_FIXED_RATE, DEFAULT_FIXED_RATE),
                    ): vol.Coerce(float),
                }
            ),
        )

    async def async_step_recalculate_history(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """
        Step 3: Ask whether to recalculate historic cost records.

        Shows a yes/no toggle pre-set to ``True`` when either commodity's cost
        mode has changed (a changed rate almost always means old cost data is
        wrong). The form description shows the old and new mode names for
        both electric and gas — considering only electric here would miss the
        common case of a user turning on a gas rate plan while leaving their
        electric mode untouched (see ``async_step_init``'s comment on
        ``gas_mode_selected``): the checkbox would default unchecked and the
        description would show identical "changing from X to X" text with no
        mention of gas at all.

        - ``No``  -> saves options and ends the flow.
        - ``Yes`` -> continues to ``recalculate_date_range``.
        """
        if user_input is not None:
            if user_input.get(CONF_RECALCULATE_HISTORY, False):
                return await self.async_step_recalculate_date_range()
            # No recalculation requested — save options and finish
            return self.async_create_entry(title="", data=self._new_options)

        old_mode: Any = self._config_entry.options.get(CONF_COST_MODE, COST_MODE_RATE_8)
        new_mode: Any = self._new_options.get(CONF_COST_MODE, COST_MODE_RATE_8)
        old_gas_mode: Any = self._config_entry.options.get(CONF_GAS_COST_MODE, COST_MODE_NONE)
        new_gas_mode: Any = self._new_options.get(CONF_GAS_COST_MODE, COST_MODE_NONE)
        electric_changed = old_mode != new_mode
        gas_changed = old_gas_mode != new_gas_mode
        mode_changed: Any = electric_changed or gas_changed

        # Build the summary as a single placeholder (rather than static lines in
        # the translation) so a gas-only change doesn't get silently dropped, and
        # an electric-only change doesn't get padded with an irrelevant
        # "gas: None to None" line.
        #
        # This step is also reached when nothing changed at all — the user can
        # open the options flow purely to re-price history after a rate update
        # (see CURRENT_RATE_SCHEMA_VERSION in const.py). Claiming a change in
        # that case produced a nonsensical "changing from Rate 5 to Rate 5", so
        # changed and unchanged commodities are worded separately. Recalculation
        # covers every commodity with a priced mode, changed or not (see
        # coordinator._async_recalculate_historic_costs_locked), so an unchanged
        # commodity still has to be named here.
        changes: list[str] = []
        unchanged: list[str] = []

        if electric_changed:
            changes.append(
                f"electric cost calculation method is changing from **{_cost_mode_label(old_mode)}** "
                f"to **{_cost_mode_label(new_mode)}**"
            )
        elif new_mode != COST_MODE_NONE:
            # Includes COST_MODE_FIXED, which also routes through this step.
            unchanged.append(f"electric cost calculation method is **{_cost_mode_label(new_mode)}**")

        if gas_changed:
            changes.append(
                f"gas cost calculation method is changing from **{_gas_cost_mode_label(old_gas_mode)}** "
                f"to **{_gas_cost_mode_label(new_gas_mode)}**"
            )
        elif new_gas_mode != COST_MODE_NONE:
            unchanged.append(f"gas cost calculation method is **{_gas_cost_mode_label(new_gas_mode)}**")

        if changes:
            summary = "Your " + " and your ".join(changes) + "."
            if unchanged:
                summary += " Your " + " and your ".join(unchanged) + ", which will be re-priced with the latest rates too."
        elif unchanged:
            summary = (
                "Your "
                + " and your ".join(unchanged)
                + ". Nothing is changing, but rate information has been updated, so recalculating re-prices "
                "your history with the latest rates."
            )
        else:
            # Defensive: async_step_init only routes here when at least one
            # commodity has a priced mode, so this should be unreachable.
            summary = "Recalculating re-prices your history with the latest rates."

        return self.async_show_form(
            step_id="recalculate_history",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RECALCULATE_HISTORY,
                        default=mode_changed,
                    ): bool,
                }
            ),
            description_placeholders={"summary": summary},
        )

    async def _async_earliest_consumption_date(self) -> date | None:
        """
        Find the earliest recorded consumption date for whichever account(s)
        are being recalculated this flow.

        Used to default the date-range picker's start date to the full
        available history, rather than a "1st of this month" default that is
        easy to leave too narrow. That default previously caused a real bug: a user enabling a
        gas rate plan mid-cycle recalculated only the current billing
        cycle's few hours, silently leaving over a year of already-recorded
        gas consumption unpriced (see coordinator.py's
        ``_async_recalculate_historic_costs_locked`` docstring for the full
        story of why already-recorded hours never get priced by normal
        polling and must go through recalculation).

        Queries with ``period="month"`` so the recorder pre-aggregates to
        roughly one row per month of history instead of one per hour --
        cheap even for a year-plus of data.

        Returns:
            The earliest date with recorded consumption, or ``None`` if the
            service address is unknown or no consumption has been recorded
            yet for the relevant account(s) (falls back to the 1st of the
            current month in that case).

        """
        canonical_addr = self._config_entry.data.get(CONF_SERVICE_ADDR)
        if not canonical_addr:
            return None

        consumption_ids: set[str] = set()
        if self._selected_mode != COST_MODE_NONE:
            consumption_ids.add(_build_statistic_ids(canonical_addr, "ELECTRIC")[0])
        if self._new_options.get(CONF_GAS_COST_MODE, COST_MODE_NONE) != COST_MODE_NONE:
            consumption_ids.add(_build_statistic_ids(canonical_addr, "GAS")[0])
        if not consumption_ids:
            return None

        rows: dict[str, list[StatisticsRow]] = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(2000, 1, 1, tzinfo=dt_util.UTC),
            dt_util.utcnow(),
            consumption_ids,
            "month",
            None,
            {"start"},
        )
        starts: list[float] = [
            row["start"] if isinstance(row["start"], (int, float)) else row["start"].timestamp()
            for series in rows.values()
            for row in series
        ]
        if not starts:
            return None
        return datetime.fromtimestamp(min(starts), tz=dt_util.UTC).date()

    async def async_step_recalculate_date_range(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """
        Step 4: Select the start and end dates for historic cost recalculation.

        Collects ISO-format date strings (YYYY-MM-DD) for the recalculation
        window. Default start date is the earliest recorded consumption for
        the account(s) being recalculated (see
        :meth:`_async_earliest_consumption_date`), falling back to the 1st of
        the current month if that can't be determined; default end date is
        yesterday (today's data is never complete).

        Validation:
        - Both values must parse as valid ISO dates.
        - ``end_date`` must not be before ``start_date``.
        - ``end_date`` must not be in the future (no API data exists yet).

        On success, creates a background asyncio task via
        :meth:`~.coordinator.DominionSCCoordinator.async_recalculate_historic_costs`
        and immediately saves the options (the flow does not wait for the
        recalculation to complete). The Energy Dashboard will update once the
        background task finishes writing the new statistics rows.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            start_str = user_input.get(CONF_RECALC_START_DATE, "")
            end_str = user_input.get(CONF_RECALC_END_DATE, "")
            try:
                start_date = date.fromisoformat(str(start_str))
                end_date = date.fromisoformat(str(end_str))
            except (ValueError, TypeError):
                errors["base"] = "invalid_date_format"
            else:
                if end_date < start_date:
                    errors["base"] = "end_before_start"
                elif end_date > date.today():
                    errors["base"] = "end_in_future"
                else:
                    # Trigger async recalculation via coordinator
                    coordinator = self.hass.data[DOMAIN][self._config_entry.entry_id]
                    self.hass.async_create_task(
                        coordinator.async_recalculate_historic_costs(
                            start_date=start_date,
                            end_date=end_date,
                            new_options=self._new_options,
                        )
                    )
                    return self.async_create_entry(title="", data=self._new_options)

        today = date.today()
        default_start = await self._async_earliest_consumption_date() or date(today.year, today.month, 1)

        return self.async_show_form(
            step_id="recalculate_date_range",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_RECALC_START_DATE,
                        default=str(default_start),
                    ): str,
                    vol.Required(
                        CONF_RECALC_END_DATE,
                        default=str(today),
                    ): str,
                }
            ),
            errors=errors,
        )
