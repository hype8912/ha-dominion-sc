"""Options flow for dominionsc integration."""

from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow

from .const import (
    CONF_COST_MODE,
    CONF_FIXED_RATE,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
    DOMAIN,
)
from .rates import TIERED_RATE_REGISTRY, build_cost_mode_choices

CONF_RECALCULATE_HISTORY = "recalculate_history"
CONF_RECALC_START_DATE = "recalc_start_date"
CONF_RECALC_END_DATE = "recalc_end_date"


def _cost_mode_label(mode: str) -> str:
    """Return a human-readable label for a cost mode constant."""
    if mode in TIERED_RATE_REGISTRY:
        return TIERED_RATE_REGISTRY[mode].name
    return {
        COST_MODE_NONE: "None",
        COST_MODE_FIXED: "Fixed Rate",
    }.get(mode, mode)


class DominionSCOptionsFlow(OptionsFlow):
    """Handle options flow for Dominion Energy SC."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._selected_mode: str | None = None
        self._new_options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Select cost calculation mode."""
        # Guard: block changes while a background recalculation is running.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        if coordinator is not None and coordinator.recalculation_lock.locked():
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors={"base": "recalculation_in_progress"},
            )

        if user_input is not None:
            self._selected_mode = user_input[CONF_COST_MODE]

            if self._selected_mode == COST_MODE_FIXED:
                return await self.async_step_fixed_rate()
            if self._selected_mode in TIERED_RATE_REGISTRY:
                self._new_options[CONF_COST_MODE] = self._selected_mode
                return await self.async_step_recalculate_history()
            # No cost calculation - skip history recalculation (nothing to calculate)
            return self.async_create_entry(
                title="", data={CONF_COST_MODE: COST_MODE_NONE}
            )

        current_options = self._config_entry.options

        mode_choices = build_cost_mode_choices()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COST_MODE,
                        default=current_options.get(CONF_COST_MODE, COST_MODE_RATE_8),
                    ): vol.In(mode_choices),
                }
            ),
        )

    async def async_step_fixed_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2a: Configure fixed rate."""
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
        """Step 3: Ask if the user wants to recalculate historic cost records."""
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
        """Step 4: Select the date range for historic recalculation."""
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
