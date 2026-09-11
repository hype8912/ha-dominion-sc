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

from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    CONF_GAS_COST_MODE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
    DOMAIN,
)
from .rates import GAS_RATE_PLAN_REGISTRY, RATE_PLAN_REGISTRY, build_cost_mode_choices, build_gas_cost_mode_choices

CONF_RECALCULATE_HISTORY = "recalculate_history"
CONF_RECALC_START_DATE = "recalc_start_date"
CONF_RECALC_END_DATE = "recalc_end_date"


def _cost_mode_label(mode: str) -> str:
    """
    Return a human-readable label for a ``COST_MODE_*`` constant.

    Used to populate the ``description_placeholders`` in the
    ``recalculate_history`` form so the user can see which rate they are
    switching from and to.

    Args:
        mode: A ``COST_MODE_*`` string constant (e.g. ``"rate_8"``).

    Returns:
        The rate schedule's full name for tiered modes (from the registry), or
        a short label for ``"none"`` and ``"fixed"``. Falls back to the raw
        mode string if it is not recognised.

    """
    if mode in RATE_PLAN_REGISTRY:
        return RATE_PLAN_REGISTRY[mode].name
    return {
        COST_MODE_NONE: "None",
        COST_MODE_FIXED: "Fixed Rate",
    }.get(mode, mode)


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
        Initialise the options flow.

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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
        has_gas = (
            coordinator is not None
            and coordinator.data is not None
            and "GAS" in coordinator.data.accounts
        )

        if user_input is not None:
            self._selected_mode = user_input[CONF_COST_MODE]

            # Always capture gas cost mode when GAS is present.
            if has_gas:
                self._new_options[CONF_GAS_COST_MODE] = user_input.get(
                    CONF_GAS_COST_MODE, COST_MODE_NONE
                )

            if self._selected_mode == COST_MODE_FIXED:
                return await self.async_step_fixed_rate()
            if self._selected_mode in RATE_PLAN_REGISTRY:
                self._new_options[CONF_COST_MODE] = self._selected_mode
                return await self.async_step_recalculate_history()
            # No cost calculation - skip history recalculation (nothing to calculate)
            return self.async_create_entry(
                title="", data={CONF_COST_MODE: COST_MODE_NONE, **self._new_options}
            )

        current_options = self._config_entry.options
        mode_choices = build_cost_mode_choices()

        schema_dict = {
            vol.Required(
                CONF_COST_MODE,
                default=current_options.get(CONF_COST_MODE, COST_MODE_RATE_8),
            ): vol.In(mode_choices),
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

    async def async_step_fixed_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                        default=current_options.get(
                            CONF_FIXED_RATE, DEFAULT_FIXED_RATE
                        ),
                    ): vol.Coerce(float),
                }
            ),
        )

    async def async_step_recalculate_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step 3: Ask whether to recalculate historic cost records.

        Shows a yes/no toggle pre-set to ``True`` when the cost mode has
        changed (a changed rate almost always means old cost data is wrong).
        The form description shows the old and new mode names so the user
        understands what they are confirming.

        - ``No``  -> saves options and ends the flow.
        - ``Yes`` -> continues to ``recalculate_date_range``.
        """
        if user_input is not None:
            if user_input.get(CONF_RECALCULATE_HISTORY, False):
                return await self.async_step_recalculate_date_range()
            # No recalculation requested — save options and finish
            return self.async_create_entry(title="", data=self._new_options)

        old_mode = self._config_entry.options.get(CONF_COST_MODE, COST_MODE_RATE_8)
        new_mode = self._new_options.get(CONF_COST_MODE, COST_MODE_RATE_8)
        mode_changed = old_mode != new_mode

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
            description_placeholders={
                "old_mode": _cost_mode_label(old_mode),
                "new_mode": _cost_mode_label(new_mode),
            },
        )

    async def async_step_recalculate_date_range(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step 4: Select the start and end dates for historic cost recalculation.

        Collects ISO-format date strings (YYYY-MM-DD) for the recalculation
        window. Default start date is the first day of the current month;
        default end date is yesterday (today's data is never complete).

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
        default_start = date(today.year, today.month, 1)

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
