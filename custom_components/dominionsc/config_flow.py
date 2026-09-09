"""
Config flow for dominionsc integration.

Initial setup (config flow) steps:
  user → [tfa_options → tfa_code] → backfill_options → cost_mode
       → [cost_mode_fixed_rate]

Re-authentication (reauth flow) steps:
  reauth → reauth_confirm → [tfa_options → tfa_code]

Options are handled by :class:`DominionSCOptionsFlow` in ``options_flow.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from dominionsc import (
    ApiException,
    CannotConnect,
    DominionSC,
    DominionSCTFAHandler,
    InvalidAuth,
    MfaChallenge,
    create_cookie_jar,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import VolDictType

from .const import (
    COMMON_NAME,
    CONF_COST_MODE,
    CONF_EXTENDED_BACKFILL,
    CONF_EXTENDED_COST_BACKFILL,
    CONF_FIXED_RATE,
    CONF_LOGIN_DATA,
    COST_MODE_FIXED,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
    DOMAIN,
)
from .options_flow import DominionSCOptionsFlow
from .rates import build_cost_mode_choices

_LOGGER = logging.getLogger(__name__)

# Form field keys used in the TFA steps (not stored in the config entry).
CONF_TFA_CODE = "tfa_code"
CONF_TFA_METHOD = "tfa_method"


async def _validate_login(
    hass: HomeAssistant,
    data: Mapping[str, Any],
) -> None:
    """Validate login data and raise exceptions on failure."""
    api = DominionSC(
        async_create_clientsession(hass, cookie_jar=create_cookie_jar()),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        # CONF_LOGIN_DATA holds a cached TFA session token from a prior successful
        # TFA submission. Passing it lets the API skip TFA on subsequent logins.
        # It is None on the very first login.
        data.get(CONF_LOGIN_DATA),
    )
    _LOGGER.debug("API: async_login")
    await api.async_login()


class DominionSCConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Handle a config flow for dominionsc.

    Guides the user through credentials, optional TFA, backfill preferences,
    and cost-mode selection before creating the config entry.
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize a new DominionSCConfigFlow."""
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self.tfa_handler: DominionSCTFAHandler | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return DominionSCOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step (credentials)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)

            # Abort if an entry for this username already exists, preventing
            # duplicate integrations for the same account.
            self._async_abort_entries_match(
                {
                    CONF_USERNAME: self._data[CONF_USERNAME],
                }
            )

            try:
                await _validate_login(self.hass, self._data)
            except MfaChallenge as exc:
                self.tfa_handler = exc.handler
                _LOGGER.debug("API: async_step_tfa_options")
                return await self.async_step_tfa_options()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ApiException as err:
                _LOGGER.error("API structure error during login: %s", err)
                errors["base"] = "unknown"
            else:
                return await self.async_step_backfill_options()

        schema_dict: VolDictType = {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema_dict), user_input
            ),
            errors=errors,
        )

    async def async_step_tfa_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA options step."""
        errors: dict[str, str] = {}
        assert self.tfa_handler is not None

        if user_input is not None:
            method = user_input[CONF_TFA_METHOD]
            try:
                _LOGGER.debug("API: async_select_tfa_option")
                await self.tfa_handler.async_select_tfa_option(method)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ApiException as err:
                _LOGGER.error(
                    "API structure error during TFA option selection: %s", err
                )
                errors["base"] = "unknown"
            else:
                return await self.async_step_tfa_code()

        _LOGGER.debug("API: async_get_tfa_options")
        try:
            tfa_options = await self.tfa_handler.async_get_tfa_options()
        except ApiException as err:
            _LOGGER.error("API structure error getting TFA options: %s", err)
            errors["base"] = "unknown"
            # Show error to user instead of proceeding
            return self.async_show_form(
                step_id="tfa_options",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        # The API returns an empty list when only one delivery method is available,
        # meaning there is nothing for the user to choose — skip straight to code entry.
        if not tfa_options:
            return await self.async_step_tfa_code()
        return self.async_show_form(
            step_id="tfa_options",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_TFA_METHOD): vol.In(tfa_options)}),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_tfa_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle TFA code submission step."""
        assert self.tfa_handler is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_TFA_CODE]
            try:
                _LOGGER.debug("API: async_submit_tfa_code")
                login_data = await self.tfa_handler.async_submit_tfa_code(code)
            except InvalidAuth:
                errors["base"] = "invalid_tfa_code"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ApiException as err:
                _LOGGER.error("API structure error during TFA code submission: %s", err)
                errors["base"] = "unknown"
            else:
                # login_data is a session/cookie token, not a password. Storing it
                # in CONF_LOGIN_DATA allows future logins to bypass TFA entirely.
                self._data[CONF_LOGIN_DATA] = login_data
                if self.source == SOURCE_REAUTH:
                    # Reauth only refreshes credentials — skip backfill/cost-mode.
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=self._data
                    )
                return await self.async_step_backfill_options()

        return self.async_show_form(
            step_id="tfa_code",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_TFA_CODE): str}), user_input
            ),
            errors=errors,
        )

    async def async_step_backfill_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask user if they want to backfill up to 365 days of data."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Cost backfill depends on consumption backfill: cost statistics are
            # derived from consumption data, so you cannot backfill one without
            # backfilling the other.
            if (
                user_input[CONF_EXTENDED_COST_BACKFILL]
                and not user_input[CONF_EXTENDED_BACKFILL]
            ):
                errors["base"] = "invalid_backfill_selection"
            else:
                if user_input[CONF_EXTENDED_BACKFILL]:
                    self._options[CONF_EXTENDED_BACKFILL] = True
                if user_input[CONF_EXTENDED_COST_BACKFILL]:
                    self._options[CONF_EXTENDED_COST_BACKFILL] = True
                return await self.async_step_cost_mode()

        return self.async_show_form(
            step_id="backfill_options",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EXTENDED_BACKFILL, default=False): bool,
                    vol.Required(CONF_EXTENDED_COST_BACKFILL, default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_cost_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select cost calculation mode during initial setup."""
        if user_input is not None:
            mode = user_input[CONF_COST_MODE]
            if mode == COST_MODE_FIXED:
                return await self.async_step_cost_mode_fixed_rate()
            self._options[CONF_COST_MODE] = mode
            return self._async_create_dominionsc_entry(self._data)

        mode_choices = build_cost_mode_choices()

        return self.async_show_form(
            step_id="cost_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_COST_MODE, default=COST_MODE_RATE_8): vol.In(
                        mode_choices
                    ),
                }
            ),
        )

    async def async_step_cost_mode_fixed_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure a custom fixed rate during initial setup."""
        if user_input is not None:
            self._options[CONF_COST_MODE] = COST_MODE_FIXED
            self._options[CONF_FIXED_RATE] = user_input[CONF_FIXED_RATE]
            return self._async_create_dominionsc_entry(self._data)

        return self.async_show_form(
            step_id="cost_mode_fixed_rate",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FIXED_RATE, default=DEFAULT_FIXED_RATE
                    ): vol.Coerce(float),
                }
            ),
        )

    @callback
    def _async_create_dominionsc_entry(
        self, data: dict[str, Any], **kwargs: Any
    ) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=f"{COMMON_NAME} ({data[CONF_USERNAME]})",
            data=data,
            options=self._options,
            **kwargs,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle configuration by re-auth."""
        reauth_entry = self._get_reauth_entry()
        # Pre-load the existing entry data so the confirm form can pre-fill the
        # username and so _data is ready if the user proceeds without changes.
        self._data = dict(reauth_entry.data)
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={CONF_NAME: reauth_entry.title},
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            self._data.update(user_input)

            try:
                await _validate_login(self.hass, self._data)
            except MfaChallenge as exc:
                self.tfa_handler = exc.handler
                return await self.async_step_tfa_options()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except ApiException as err:
                _LOGGER.error("API structure error during reauth: %s", err)
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(reauth_entry, data=self._data)

        schema_dict: VolDictType = {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema_dict), self._data
            ),
            errors=errors,
            description_placeholders={CONF_NAME: reauth_entry.title},
        )
