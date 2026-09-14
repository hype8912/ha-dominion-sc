"""
Config flow for the dominionsc integration.

This module guides a first-time user through setting up the integration,
and handles re-authentication when the stored session token expires.

Initial setup flow (step IDs)
------------------------------
::

    user
     +-- no MFA  ------------------------------------------------> backfill_options
     +-- MFA required --> tfa_options --> tfa_code -----------> backfill_options
     +-- bad credentials  ------------------------------------> (error, retry)
                                                                        |
    backfill_options -----------------------------------------------> cost_mode
                                                                        |
    cost_mode --> "rate_8" / "rate_5" / etc. / "none" --------> gas_cost_mode (if GAS) --> CREATE ENTRY
              |                                               +-> CREATE ENTRY (no GAS)
              +-- "fixed" --> cost_mode_fixed_rate -----------> gas_cost_mode (if GAS) --> CREATE ENTRY
                                                            +-> CREATE ENTRY (no GAS)

Re-authentication flow
-----------------------
::

    reauth --> reauth_confirm
                +-- no MFA  -----------------------------------------> RELOAD ENTRY
                +-- MFA required --> tfa_options --> tfa_code -------> RELOAD ENTRY
                +-- bad credentials  ---------------------------------> (error)

Key design decisions
--------------------
- The TFA session token (``CONF_LOGIN_DATA``) is stored in ``entry.data`` so
  future 12-hour re-logins skip the MFA challenge without user interaction.
- Backfill and cost-mode choices are stored in ``entry.options`` (not ``data``)
  so they can be changed later via the options flow without re-authenticating.
- Options are handled by :class:`~.options_flow.DominionSCOptionsFlow`
  in ``options_flow.py``.
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
    CONF_GAS_COST_MODE,
    CONF_LOGIN_DATA,
    CONF_SERVICE_ADDR,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_8,
    DEFAULT_FIXED_RATE,
    DOMAIN,
)
from .options_flow import DominionSCOptionsFlow
from .rates import build_cost_mode_choices, build_gas_cost_mode_choices

_LOGGER = logging.getLogger(__name__)

# Form field keys used in the TFA steps (not stored in the config entry).
CONF_TFA_CODE = "tfa_code"
CONF_TFA_METHOD = "tfa_method"


async def _validate_login(
    hass: HomeAssistant,
    data: Mapping[str, Any],
) -> None:
    """
    Attempt a login with the provided credentials and raise on failure.

    Creates a throw-away API client instance, attempts to log in, and discards
    the client. The config flow uses the result to decide whether to proceed to
    the next step or show an error.

    Raises:
        :class:`~dominionsc.MfaChallenge`: TFA is required. The caller should
            transition to the ``tfa_options`` step. The exception carries the
            :class:`~dominionsc.DominionSCTFAHandler` needed for the TFA steps.
        :class:`~dominionsc.InvalidAuth`: Username or password is wrong.
        :class:`~dominionsc.CannotConnect`: Network error.
        :class:`~dominionsc.ApiException`: Unexpected API response structure.

    Args:
        hass: The Home Assistant instance (used to get the aiohttp session).
        data: Dict containing at minimum ``CONF_USERNAME`` and
              ``CONF_PASSWORD``. May also contain ``CONF_LOGIN_DATA`` (the
              cached TFA session token from a prior successful TFA submission).

    """
    api = DominionSC(
        async_create_clientsession(hass, cookie_jar=create_cookie_jar()),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        # CONF_LOGIN_DATA holds a cached TFA session token from a prior
        # successful TFA submission. Passing it lets the API skip TFA on
        # subsequent logins. It is None on the very first login attempt.
        data.get(CONF_LOGIN_DATA),
    )
    _LOGGER.debug("API: async_login")
    await api.async_login()


async def _fetch_accounts(
    hass: HomeAssistant,
    data: Mapping[str, Any],
) -> tuple[list[str], str]:
    """
    Fetch the list of account type strings and service address after a successful login.

    Creates a fresh API client using the stored credentials and calls
    ``async_get_accounts()``. Returns a tuple of (account_list, service_addr).
    Returns ``([], "")`` if any network or API error occurs so that callers can
    treat accounts as unknown and skip account-specific flow steps.

    Args:
        hass: The Home Assistant instance.
        data: Dict containing ``CONF_USERNAME``, ``CONF_PASSWORD``, and
              optionally ``CONF_LOGIN_DATA``.

    Returns:
        Tuple of (list of account type strings, service address string).
        Both are empty on error.
    """
    try:
        api = DominionSC(
            async_create_clientsession(hass, cookie_jar=create_cookie_jar()),
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_LOGIN_DATA),
        )
        await api.async_login()
        accounts, service_addr = await api.async_get_accounts()
        return list(accounts), service_addr
    except (CannotConnect, ApiException, InvalidAuth):
        return [], ""


class DominionSCConfigFlow(ConfigFlow, domain=DOMAIN):
    """
    Handle the initial config flow for dominionsc.

    Guides the user through:
    1. Credentials entry (username + password).
    2. Optional TFA delivery-method selection and code entry.
    3. Backfill preferences (how far back to load consumption data).
    4. Cost-mode selection (tiered rate schedule, fixed rate, or none).

    State is accumulated in ``_data`` (for ``entry.data`` — credentials) and
    ``_options`` (for ``entry.options`` — user preferences) across steps and
    written to the config entry in :meth:`_async_create_dominionsc_entry`.

    ``VERSION = 1``: bump this if ``entry.data`` schema changes in a way that
    requires a migration step (see HA migration docs).
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state for a new config-flow session."""
        # Accumulates credentials (username, password, login_data).
        # Written to entry.data at the end of the flow.
        self._data: dict[str, Any] = {}
        # Accumulates user preferences (backfill flags, cost mode, fixed rate).
        # Written to entry.options at the end of the flow.
        self._options: dict[str, Any] = {}
        # Holds the TFA handler returned by MfaChallenge so tfa_options and
        # tfa_code steps can use it. None when TFA is not required.
        self.tfa_handler: DominionSCTFAHandler | None = None
        # Holds the list of account type strings fetched after a successful login.
        # Used to determine whether to show the gas cost mode step.
        self._accounts: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """
        Return the options flow handler for this integration.

        Called by HA when the user clicks "Configure" on an existing entry.
        Returns a :class:`~.options_flow.DominionSCOptionsFlow` instance which
        handles cost-mode changes and historic cost recalculation.
        """
        return DominionSCOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step 1: Collect username and password.

        On first call (``user_input`` is ``None``) renders a form with username
        and password fields. On submission, attempts a login:
        - If MFA is required, transitions to ``tfa_options``.
        - If credentials are invalid, re-renders the form with an error.
        - If login succeeds (no MFA), transitions to ``backfill_options``.

        Also aborts if an entry for the same username already exists, preventing
        duplicate integrations.
        """
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
                self._accounts, service_addr = await _fetch_accounts(self.hass, self._data)
                if service_addr:
                    self._data[CONF_SERVICE_ADDR] = service_addr
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
        """
        Step 2a: Let user select how to receive the TFA code.

        The Dominion API may support multiple delivery methods (SMS, email,
        etc.). If the API returns an empty list (only one method available),
        skips this step and jumps straight to ``tfa_code``.

        On method selection, sends the code to the chosen destination and
        transitions to ``tfa_code``.
        """
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
        """
        Step 2b: Collect the TFA code entered by the user.

        On successful code submission the library returns a session token
        (``login_data``) which is stored in ``_data[CONF_LOGIN_DATA]``. This
        token is persisted in ``entry.data`` and passed to the API on every
        subsequent login, allowing the coordinator to re-authenticate every
        12 hours without prompting the user for another TFA code.

        If this is a re-authentication flow, ends here (no backfill/cost-mode
        steps needed — those settings already exist in the entry's options).
        """
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
                if self.source != SOURCE_REAUTH:
                    self._accounts, service_addr = await _fetch_accounts(self.hass, self._data)
                    if service_addr:
                        self._data[CONF_SERVICE_ADDR] = service_addr
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
        """
        Step 3: Ask whether to backfill up to 365 days of consumption history.

        Presents two boolean toggles:
        - ``CONF_EXTENDED_BACKFILL``: seed up to 365 days of consumption data.
        - ``CONF_EXTENDED_COST_BACKFILL``: also calculate cost for that range.

        Validation rule: cost backfill requires consumption backfill (cost is
        derived from consumption data). If the user enables cost-only backfill,
        an error is shown.

        Selected flags are stored in ``_options`` (not ``_data``) because they
        are user preferences that can be changed later, not credentials.
        """
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
        """
        Step 4: Select cost calculation mode.

        Presents a dropdown with all available cost modes (from
        :func:`~.rates.build_cost_mode_choices`). The default is Rate 8
        (Dominion's standard residential rate).

        - If ``COST_MODE_FIXED`` is selected, continues to
          ``cost_mode_fixed_rate`` to collect the custom rate.
        - For all other modes, creates the config entry immediately.
        """
        if user_input is not None:
            mode = user_input[CONF_COST_MODE]
            if mode == COST_MODE_FIXED:
                return await self.async_step_cost_mode_fixed_rate()
            self._options[CONF_COST_MODE] = mode
            if "GAS" in self._accounts:
                return await self.async_step_gas_cost_mode()
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
        """
        Step 4a: Collect the custom $/kWh rate for fixed-rate mode.

        Only reached when the user chose ``COST_MODE_FIXED`` in the previous
        step. Presents a numeric input pre-filled with ``DEFAULT_FIXED_RATE``.
        On submission, stores the rate in ``_options`` and creates the entry.
        """
        if user_input is not None:
            self._options[CONF_COST_MODE] = COST_MODE_FIXED
            self._options[CONF_FIXED_RATE] = user_input[CONF_FIXED_RATE]
            if "GAS" in self._accounts:
                return await self.async_step_gas_cost_mode()
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

    async def async_step_gas_cost_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step 4b: Select gas cost calculation rate (shown only if GAS account detected).

        Only reached when ``"GAS"`` was found in the account list after login.
        Presents a dropdown with available gas rate plans (Rate 32S, Rate 32V, or None).
        The default is ``COST_MODE_NONE`` (no gas cost calculation).

        On submission, stores ``CONF_GAS_COST_MODE`` in ``_options`` and creates
        the config entry.
        """
        if user_input is not None:
            self._options[CONF_GAS_COST_MODE] = user_input[CONF_GAS_COST_MODE]
            return self._async_create_dominionsc_entry(self._data)

        gas_choices = build_gas_cost_mode_choices()

        return self.async_show_form(
            step_id="gas_cost_mode",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GAS_COST_MODE, default=COST_MODE_NONE
                    ): vol.In(gas_choices),
                }
            ),
        )

    @callback
    def _async_create_dominionsc_entry(
        self, data: dict[str, Any], **kwargs: Any
    ) -> ConfigFlowResult:
        """
        Create the config entry from the accumulated data and options.

        Called at the end of every successful config-flow path. The entry
        title includes the username so the user can distinguish multiple
        accounts at a glance in the integrations list.
        """
        return self.async_create_entry(
            title=f"{COMMON_NAME} ({data[CONF_USERNAME]})",
            data=data,
            options=self._options,
            **kwargs,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """
        Start the re-authentication flow.

        Triggered automatically by HA when the coordinator raises
        :class:`~homeassistant.exceptions.ConfigEntryAuthFailed` (e.g. when
        the stored TFA session token has been invalidated on the server side).

        Loads the existing entry's data into ``_data`` so the confirm form
        can pre-fill the username field, then shows the ``reauth_confirm``
        form.
        """
        reauth_entry = self._get_reauth_entry()
        # Pre-load existing credentials so the confirm form can show the
        # current username and so _data is ready if the user doesn't change it.
        self._data = dict(reauth_entry.data)
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={CONF_NAME: reauth_entry.title},
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Step 2 of re-auth: collect fresh credentials.

        Presents username and password fields (pre-filled with the stored
        username). On submission:
        - If credentials are valid and MFA is not required, updates the entry
          and reloads the integration.
        - If MFA is required, transitions to ``tfa_options`` / ``tfa_code``
          exactly as in the initial setup flow. After TFA completes, the entry
          is updated and reloaded.
        - On credential failure, re-renders the form with an error.
        """
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
