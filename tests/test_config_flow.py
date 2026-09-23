"""Tests for Dominion Energy SC config flow."""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from dominionsc.const import BIDGELY_PILOT_ID
from dominionsc.exceptions import ApiException, CannotConnect, InvalidAuth, MfaChallenge
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dominionsc.config_flow import (
    CONF_TFA_CODE,
    CONF_TFA_METHOD,
    DominionSCConfigFlow,
    _fetch_accounts,
    _validate_login,
)
from custom_components.dominionsc.const import (
    CONF_COST_MODE,
    CONF_EXTENDED_BACKFILL,
    CONF_EXTENDED_COST_BACKFILL,
    CONF_FIXED_RATE,
    CONF_GAS_COST_MODE,
    CONF_LOGIN_DATA,
    CONF_PILOT_ID,
    CONF_SERVICE_ADDR,
    COST_MODE_FIXED,
    COST_MODE_NONE,
    COST_MODE_RATE_6,
    COST_MODE_RATE_8,
    COST_MODE_RATE_32S,
    DOMAIN,
)
from custom_components.dominionsc.options_flow import (
    CONF_RECALC_END_DATE,
    CONF_RECALC_START_DATE,
    CONF_RECALCULATE_HISTORY,
    DominionSCOptionsFlow,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(hass: HomeAssistant, user_input: dict) -> MockConfigEntry:
    """Register a config entry directly, bypassing the flow."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=user_input,
        options={CONF_COST_MODE: COST_MODE_RATE_8},
        title=f"Dominion Energy SC ({user_input[CONF_USERNAME]})",
    )
    entry.add_to_hass(hass)
    return entry


async def _login_to_backfill(hass: HomeAssistant, user_input: dict) -> ConfigFlowResult:
    """Run the user step with a patched login and return the backfill_options form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.dominionsc.config_flow._validate_login"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["step_id"] == "backfill_options"
    return result


async def _login_to_cost_mode(hass: HomeAssistant, user_input: dict) -> ConfigFlowResult:
    """Run through login and backfill_options, return the cost_mode form result."""
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    assert result["step_id"] == "cost_mode"
    return result


# ---------------------------------------------------------------------------
# Config flow — credentials step
# ---------------------------------------------------------------------------


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    """Initial step renders the credentials form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_flow_invalid_auth(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """Invalid credentials show an error and stay on the user step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=InvalidAuth("Invalid credentials"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """Connection failure shows an error and stays on the user step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=CannotConnect("Connection error"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """API exception during login shows an unknown error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=ApiException("API error", "https://test.com"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "unknown"}


# ---------------------------------------------------------------------------
# Config flow — cost mode step (shown after successful login)
# ---------------------------------------------------------------------------


async def test_user_flow_success_rate_8(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Successful login → Rate 8 selection creates the entry with correct options."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Dominion Energy SC ({user_input[CONF_USERNAME]})"
    assert result["data"][CONF_USERNAME] == user_input[CONF_USERNAME]
    assert result["data"][CONF_PASSWORD] == user_input[CONF_PASSWORD]
    assert result["data"][CONF_SERVICE_ADDR] == "addr_123"
    assert result["options"] == {CONF_COST_MODE: COST_MODE_RATE_8}


async def test_user_flow_service_addr_stored_in_entry_data(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """CONF_SERVICE_ADDR from the API is stored in entry.data when the flow completes."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERVICE_ADDR] == "addr_123"


async def test_user_flow_no_service_addr_omits_key(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """A blank service address (the ([], "") error return of _fetch_accounts) is not
    stored, and the flow still completes."""
    mock_dominionsc_api.async_get_accounts.return_value = (["ELECTRIC"], "")

    result = await _login_to_cost_mode(hass, user_input)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SERVICE_ADDR not in result["data"]


async def test_user_flow_pilot_id_defaults_when_not_entered(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """CONF_PILOT_ID falls back to the library's default when the user leaves
    the (optional, advanced) field blank on initial setup."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PILOT_ID] == BIDGELY_PILOT_ID


async def test_user_flow_pilot_id_custom_value_stored(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """A custom pilot ID entered on initial setup is stored in entry.data and
    passed through to the login/account-fetch API calls for that same flow."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.dominionsc.config_flow._validate_login"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**user_input, CONF_PILOT_ID: "99999"})
    assert result["step_id"] == "backfill_options"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PILOT_ID] == "99999"


async def test_fetch_accounts_returns_accounts_and_service_addr(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
) -> None:
    """_fetch_accounts returns (accounts, service_addr) on success."""
    mock_dominionsc_api.async_get_accounts.return_value = (["ELECTRIC"], "my_addr")
    result = await _fetch_accounts(hass, {CONF_USERNAME: "user", CONF_PASSWORD: "pass"})
    assert result == (["ELECTRIC"], "my_addr")


async def test_user_flow_success_rate_6(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Successful login → Rate 6 selection creates the entry with correct options."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_6})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_COST_MODE: COST_MODE_RATE_6}


async def test_user_flow_success_none(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting no cost calculation creates the entry immediately."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_NONE})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_COST_MODE: COST_MODE_NONE}


async def test_user_flow_success_fixed_rate(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting fixed rate prompts for the value, then creates the entry."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_FIXED})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cost_mode_fixed_rate"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_FIXED_RATE: 0.12})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.12}


# ---------------------------------------------------------------------------
# Config flow — backfill options step
# ---------------------------------------------------------------------------


async def test_backfill_options_shows_form(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """After login, the backfill options form is shown."""
    result = await _login_to_backfill(hass, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "backfill_options"


async def test_backfill_both_off(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """
    Declining both backfill options proceeds to
    cost_mode with no backfill options set.
    """
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    assert result["step_id"] == "cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_EXTENDED_BACKFILL not in result["options"]
    assert CONF_EXTENDED_COST_BACKFILL not in result["options"]


async def test_backfill_consumption_only(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Enabling only consumption backfill sets the flag and proceeds."""
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: True, CONF_EXTENDED_COST_BACKFILL: False},
    )
    assert result["step_id"] == "cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_EXTENDED_BACKFILL] is True
    assert CONF_EXTENDED_COST_BACKFILL not in result["options"]


async def test_backfill_both_on(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Enabling both backfill options sets both flags."""
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: True, CONF_EXTENDED_COST_BACKFILL: True},
    )
    assert result["step_id"] == "cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_EXTENDED_BACKFILL] is True
    assert result["options"][CONF_EXTENDED_COST_BACKFILL] is True


async def test_backfill_cost_without_consumption_rejected(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """Requesting cost backfill without consumption backfill shows an error."""
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "backfill_options"
    assert result["errors"] == {"base": "invalid_backfill_selection"}


# ---------------------------------------------------------------------------
# Config flow — TFA paths
# ---------------------------------------------------------------------------


async def test_user_flow_with_tfa(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Full TFA flow routes to cost_mode after a successful code submission."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_options"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_METHOD: "sms"})
    assert result["step_id"] == "tfa_code"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "123456"})

    # TFA success routes to backfill options first.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "backfill_options"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    assert result["step_id"] == "cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_LOGIN_DATA in result["data"]
    assert result["options"] == {CONF_COST_MODE: COST_MODE_RATE_8}


async def test_tfa_flow_no_service_addr_omits_key(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Blank service address after TFA submission is not stored, and the flow
    still routes on to backfill options."""
    mock_dominionsc_api.async_get_accounts.return_value = (["ELECTRIC"], "")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_METHOD: "sms"})
    assert result["step_id"] == "tfa_code"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "123456"})
    assert result["step_id"] == "backfill_options"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SERVICE_ADDR not in result["data"]


async def test_tfa_options_cannot_connect(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """Connection error during TFA method selection shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    mock_tfa_handler.async_select_tfa_option.side_effect = CannotConnect("Connection error")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_METHOD: "sms"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_options"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_tfa_options_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """API exception during TFA method selection shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    mock_tfa_handler.async_select_tfa_option.side_effect = ApiException("API error", "https://test.com")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_METHOD: "sms"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_options"
    assert result["errors"] == {"base": "unknown"}


async def test_tfa_options_get_options_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """API exception when fetching TFA options shows an error form."""
    mock_tfa_handler.async_get_tfa_options.side_effect = ApiException("API error", "https://test.com")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_options"
    assert result["errors"] == {"base": "unknown"}


async def test_tfa_code_invalid(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """Invalid TFA code shows an error and stays on the tfa_code step."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    mock_tfa_handler.async_get_tfa_options.return_value = []
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["step_id"] == "tfa_code"

    mock_tfa_handler.async_submit_tfa_code.side_effect = InvalidAuth("Invalid TFA code")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "wrong"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_code"
    assert result["errors"] == {"base": "invalid_tfa_code"}


async def test_tfa_code_cannot_connect(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """Connection error during TFA code submission shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    mock_tfa_handler.async_get_tfa_options.return_value = []
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    mock_tfa_handler.async_submit_tfa_code.side_effect = CannotConnect("Connection error")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_code"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_tfa_code_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """API exception during TFA code submission shows an error."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    mock_tfa_handler.async_get_tfa_options.return_value = []
    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    mock_tfa_handler.async_submit_tfa_code.side_effect = ApiException("API error", "https://test.com")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "123456"})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa_code"
    assert result["errors"] == {"base": "unknown"}


# ---------------------------------------------------------------------------
# Config flow — duplicate entry
# ---------------------------------------------------------------------------


async def test_duplicate_entry(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """A second entry for the same username is aborted."""
    # Create the first entry through the full flow.
    result = await _login_to_cost_mode(hass, user_input)
    await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    # Attempt a second entry for the same username.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch("custom_components.dominionsc.config_flow._validate_login"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reauth flow
# ---------------------------------------------------------------------------


async def test_reauth_flow_success(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Successful reauth updates the entry without going through cost mode."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input, title="Test")
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch("custom_components.dominionsc.config_flow._validate_login"):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_flow_invalid_auth(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """Invalid credentials during reauth show an error."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input, title="Test")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=InvalidAuth("Invalid credentials"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_flow_cannot_connect(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """Connection failure during reauth shows an error."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input, title="Test")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=CannotConnect("Connection error"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_flow_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    user_input: dict,
) -> None:
    """API exception during reauth shows an unknown error."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input, title="Test")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=ApiException("API error", "https://test.com"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "unknown"}


async def test_reauth_confirm_no_input(
    hass: HomeAssistant,
    user_input: dict,
) -> None:
    """Reauth confirm with no input re-renders the form without calling login."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], None)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


async def test_reauth_flow_with_tfa(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_tfa_handler: AsyncMock,
    user_input: dict,
) -> None:
    """Reauth via TFA completes without entering cost mode selection."""
    entry = MockConfigEntry(domain=DOMAIN, data=user_input, title="Test")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    mock_tfa_handler.async_get_tfa_options.return_value = []

    with patch(
        "custom_components.dominionsc.config_flow._validate_login",
        side_effect=MfaChallenge("TFA Required", mock_tfa_handler),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["step_id"] == "tfa_code"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TFA_CODE: "123456"})

    # Reauth completes without going through cost_mode.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


# ---------------------------------------------------------------------------
# Options flow — mode selection
# ---------------------------------------------------------------------------


async def test_options_flow_none(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting None saves immediately with no sub-steps."""
    entry = _make_entry(hass, user_input)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_NONE})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_COST_MODE: COST_MODE_NONE}


async def test_options_flow_fixed_rate(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting fixed rate shows the rate form, then recalculate_history."""
    entry = _make_entry(hass, user_input)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_FIXED})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "fixed_rate"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_FIXED_RATE: 0.15})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: False})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_COST_MODE: COST_MODE_FIXED, CONF_FIXED_RATE: 0.15}


async def test_options_flow_rate_8(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting Rate 8 goes straight to recalculate_history."""
    entry = _make_entry(hass, user_input)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: False})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_COST_MODE: COST_MODE_RATE_8}


async def test_options_flow_rate_6(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting Rate 6 goes straight to recalculate_history."""
    entry = _make_entry(hass, user_input)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_6})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: False})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_COST_MODE: COST_MODE_RATE_6}


# ---------------------------------------------------------------------------
# Options flow — recalculate history
# ---------------------------------------------------------------------------


async def test_options_flow_recalculate_history_yes(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Accepting recalculation advances to the date-range step."""
    entry = _make_entry(hass, user_input)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: True})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_date_range"


async def test_options_flow_recalculate_date_range_success(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """A valid date range triggers background recalculation and saves options."""
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    mock_coordinator.async_recalculate_historic_costs = AsyncMock()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECALC_START_DATE: "2024-01-21", CONF_RECALC_END_DATE: "2024-02-20"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_COST_MODE: COST_MODE_RATE_8}
    assert mock_coordinator.async_recalculate_historic_costs.called


# ---------------------------------------------------------------------------
# Options flow — recalculation date-range default (earliest consumption)
# ---------------------------------------------------------------------------


def _make_flow_with_service_addr(hass: HomeAssistant, user_input: dict) -> tuple[MockConfigEntry, DominionSCOptionsFlow]:
    """Build an options flow instance with CONF_SERVICE_ADDR set on entry.data.

    _make_entry() doesn't set CONF_SERVICE_ADDR, which is why the existing
    recalculate_date_range tests never actually exercise the earliest-
    consumption-date lookup (it short-circuits to None immediately). These
    tests need the address present to reach the recorder query.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**user_input, CONF_SERVICE_ADDR: "123 Main St"},
        options={CONF_COST_MODE: COST_MODE_RATE_8},
        title=f"Dominion Energy SC ({user_input[CONF_USERNAME]})",
    )
    entry.add_to_hass(hass)
    flow = DominionSCOptionsFlow(entry)
    flow.hass = hass
    return entry, flow


async def test_earliest_consumption_date_no_service_addr_returns_none(hass: HomeAssistant, user_input: dict) -> None:
    """No CONF_SERVICE_ADDR on entry.data -> None without querying the recorder."""
    entry = _make_entry(hass, user_input)
    flow = DominionSCOptionsFlow(entry)
    flow.hass = hass
    flow._selected_mode = COST_MODE_RATE_8

    assert await flow._async_earliest_consumption_date() is None


async def test_earliest_consumption_date_electric_only(hass: HomeAssistant, user_input: dict) -> None:
    """Electric mode selected, gas untouched -> queries only the electric stat."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_RATE_8

    electric_cid = "dominionsc:123_main_st_electric_energy_consumption"
    earliest_ts = datetime(2025, 3, 1, tzinfo=UTC).timestamp()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={electric_cid: [{"start": earliest_ts}]})
    with patch(
        "custom_components.dominionsc.options_flow.get_instance",
        return_value=recorder,
    ):
        result = await flow._async_earliest_consumption_date()

    assert result == date(2025, 3, 1)


async def test_earliest_consumption_date_gas_only(hass: HomeAssistant, user_input: dict) -> None:
    """Electric mode is None but gas is selected -> queries only the gas stat."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_NONE
    flow._new_options = {CONF_GAS_COST_MODE: COST_MODE_RATE_32S}

    gas_cid = "dominionsc:123_main_st_gas_energy_consumption"
    earliest_ts = datetime(2025, 9, 4, tzinfo=UTC).timestamp()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={gas_cid: [{"start": earliest_ts}]})
    with patch(
        "custom_components.dominionsc.options_flow.get_instance",
        return_value=recorder,
    ):
        result = await flow._async_earliest_consumption_date()

    assert result == date(2025, 9, 4)


async def test_earliest_consumption_date_neither_selected_returns_none(hass: HomeAssistant, user_input: dict) -> None:
    """Electric is None and gas is untouched -> None without querying the recorder."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_NONE

    assert await flow._async_earliest_consumption_date() is None


async def test_earliest_consumption_date_both_accounts_takes_min(hass: HomeAssistant, user_input: dict) -> None:
    """Both electric and gas selected -> the overall earliest date wins."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_RATE_8
    flow._new_options = {CONF_GAS_COST_MODE: COST_MODE_RATE_32S}

    electric_cid = "dominionsc:123_main_st_electric_energy_consumption"
    gas_cid = "dominionsc:123_main_st_gas_energy_consumption"
    electric_ts = datetime(2025, 6, 1, tzinfo=UTC).timestamp()
    gas_ts = datetime(2024, 9, 4, tzinfo=UTC).timestamp()  # earlier than electric
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(
        return_value={
            electric_cid: [{"start": electric_ts}],
            gas_cid: [{"start": gas_ts}],
        }
    )
    with patch(
        "custom_components.dominionsc.options_flow.get_instance",
        return_value=recorder,
    ):
        result = await flow._async_earliest_consumption_date()

    assert result == date(2024, 9, 4)


async def test_earliest_consumption_date_no_rows_returns_none(hass: HomeAssistant, user_input: dict) -> None:
    """No recorded consumption yet -> None, so the caller falls back to 1st of month."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_RATE_8

    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "custom_components.dominionsc.options_flow.get_instance",
        return_value=recorder,
    ):
        result = await flow._async_earliest_consumption_date()

    assert result is None


async def test_recalculate_date_range_form_defaults_to_earliest_consumption(hass: HomeAssistant, user_input: dict) -> None:
    """The rendered date-range form's start-date default is the earliest
    recorded consumption date, not a too-narrow 1st-of-month default --
    regression test for the bug where a narrow default silently left over a
    year of gas consumption unpriced after a recalculation."""
    _, flow = _make_flow_with_service_addr(hass, user_input)
    flow._selected_mode = COST_MODE_RATE_8

    electric_cid = "dominionsc:123_main_st_electric_energy_consumption"
    earliest_ts = datetime(2025, 9, 4, tzinfo=UTC).timestamp()
    recorder = MagicMock()
    recorder.async_add_executor_job = AsyncMock(return_value={electric_cid: [{"start": earliest_ts}]})
    with patch(
        "custom_components.dominionsc.options_flow.get_instance",
        return_value=recorder,
    ):
        result = await flow.async_step_recalculate_date_range()

    assert result["type"] is FlowResultType.FORM
    data_schema = result["data_schema"]
    assert data_schema is not None
    defaults = {key: key.default() for key in data_schema.schema}
    assert defaults[CONF_RECALC_START_DATE] == "2025-09-04"


async def test_options_flow_recalculate_invalid_date_format(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """An unparseable date string shows an invalid_date_format error."""
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECALC_START_DATE: "not-a-date", CONF_RECALC_END_DATE: "2024-02-20"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_date_range"
    assert result["errors"] == {"base": "invalid_date_format"}


async def test_options_flow_recalculate_end_before_start(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """End date before start date shows an end_before_start error."""
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECALC_START_DATE: "2024-02-20", CONF_RECALC_END_DATE: "2024-01-01"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_date_range"
    assert result["errors"] == {"base": "end_before_start"}


async def test_options_flow_recalculate_end_in_future(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """End date in the future shows an end_in_future error."""
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECALC_START_DATE: "2024-01-01", CONF_RECALC_END_DATE: "2099-12-31"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_date_range"
    assert result["errors"] == {"base": "end_in_future"}


# ---------------------------------------------------------------------------
# Options flow — recalculation lock
# ---------------------------------------------------------------------------


async def test_options_flow_recalculation_in_progress(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Opening options while a recalculation is running shows an error."""
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = True
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "recalculation_in_progress"}


# ---------------------------------------------------------------------------
# Options flow — pilot ID (entry.data, not entry.options)
# ---------------------------------------------------------------------------


async def test_options_flow_pilot_id_change_updates_data_and_reloads(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Changing the pilot ID updates entry.data and schedules a reload so the
    coordinator rebuilds its DominionSC client -- a plain options refresh
    would otherwise keep using the stale client with the old pilot_id."""
    entry = _make_entry(hass, user_input)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_mock:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_COST_MODE: COST_MODE_NONE, CONF_PILOT_ID: "99999"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PILOT_ID] == "99999"
    reload_mock.assert_called_once_with(entry.entry_id)


async def test_options_flow_pilot_id_unchanged_no_reload(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Submitting the options form without changing the pilot ID does not
    touch entry.data or trigger a reload -- only a real change should."""
    entry = _make_entry(hass, user_input)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_mock:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_NONE})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PILOT_ID not in entry.data
    reload_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


async def test_validate_login_helper(hass: HomeAssistant, mock_dominionsc_api: AsyncMock) -> None:
    """The _validate_login helper instantiates the API and calls async_login."""
    await _validate_login(hass, {CONF_USERNAME: "test", CONF_PASSWORD: "test"})
    assert mock_dominionsc_api.async_login.called


def test_async_get_options_flow(hass: HomeAssistant) -> None:
    """async_get_options_flow returns a DominionSCOptionsFlow instance."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    flow = DominionSCConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, DominionSCOptionsFlow)


# ---------------------------------------------------------------------------
# Config flow — Phase 6: gas cost mode step
# ---------------------------------------------------------------------------


async def _login_to_cost_mode_with_gas(
    hass: HomeAssistant,
    user_input: dict,
    mock_api: MagicMock,
) -> ConfigFlowResult:
    """Like _login_to_cost_mode but sets mock to return ELECTRIC + GAS accounts."""
    mock_api.async_get_accounts.return_value = (["ELECTRIC", "GAS"], "addr_123")
    result = await _login_to_backfill(hass, user_input)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EXTENDED_BACKFILL: False, CONF_EXTENDED_COST_BACKFILL: False},
    )
    assert result["step_id"] == "cost_mode"
    return result


async def test_gas_cost_mode_step_shown_when_gas_account_present(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """When GAS account is detected, gas_cost_mode step appears after cost_mode."""
    result = await _login_to_cost_mode_with_gas(hass, user_input, mock_dominionsc_api)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "gas_cost_mode"


async def test_gas_cost_mode_step_skipped_when_no_gas_account(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """When only ELECTRIC account exists, gas_cost_mode step is skipped."""
    result = await _login_to_cost_mode(hass, user_input)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_GAS_COST_MODE not in result["options"]


async def test_gas_cost_mode_rate_32s_selection(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting Rate 32S in gas step saves CONF_GAS_COST_MODE = 'rate_32s'."""
    result = await _login_to_cost_mode_with_gas(hass, user_input, mock_dominionsc_api)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_RATE_8})
    assert result["step_id"] == "gas_cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_GAS_COST_MODE: COST_MODE_RATE_32S})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_GAS_COST_MODE] == COST_MODE_RATE_32S
    assert result["options"][CONF_COST_MODE] == COST_MODE_RATE_8


async def test_gas_cost_mode_default_none(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Selecting None in gas step saves CONF_GAS_COST_MODE = 'none'."""
    result = await _login_to_cost_mode_with_gas(hass, user_input, mock_dominionsc_api)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_NONE})
    assert result["step_id"] == "gas_cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_GAS_COST_MODE: COST_MODE_NONE})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_GAS_COST_MODE] == COST_MODE_NONE


async def test_gas_cost_mode_shown_after_fixed_rate(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Fixed rate + gas account: gas_cost_mode step shown after cost_mode_fixed_rate."""
    result = await _login_to_cost_mode_with_gas(hass, user_input, mock_dominionsc_api)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_COST_MODE: COST_MODE_FIXED})
    assert result["step_id"] == "cost_mode_fixed_rate"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_FIXED_RATE: 0.12})
    assert result["step_id"] == "gas_cost_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_GAS_COST_MODE: COST_MODE_RATE_32S})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_COST_MODE] == COST_MODE_FIXED
    assert result["options"][CONF_FIXED_RATE] == 0.12
    assert result["options"][CONF_GAS_COST_MODE] == COST_MODE_RATE_32S


# ---------------------------------------------------------------------------
# Options flow — Phase 6: gas cost mode field
# ---------------------------------------------------------------------------


async def test_options_flow_with_gas_account_shows_gas_field(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Options init shows gas_cost_mode field when coordinator has GAS account."""
    from unittest.mock import MagicMock

    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    mock_coordinator.data = MagicMock()
    mock_coordinator.data.accounts = {"ELECTRIC": MagicMock(), "GAS": MagicMock()}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    # The schema should include CONF_GAS_COST_MODE
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema_keys = [str(k) for k in data_schema.schema]
    assert any(CONF_GAS_COST_MODE in k for k in schema_keys)


async def test_options_flow_gas_mode_rate_32s_saved(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Gas cost mode selection in options flow is persisted.

    Selecting a gas rate plan while electric cost mode is None still routes
    through recalculate_history (not straight to CREATE_ENTRY) -- gas needs
    the option to recalculate its own previously-recorded consumption, same
    as electric. See coordinator.py's recalculation docstring.
    """
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    mock_coordinator.data = MagicMock()
    mock_coordinator.data.accounts = {"ELECTRIC": MagicMock(), "GAS": MagicMock()}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_COST_MODE: COST_MODE_NONE, CONF_GAS_COST_MODE: COST_MODE_RATE_32S},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: False})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GAS_COST_MODE] == COST_MODE_RATE_32S
    assert result["data"][CONF_COST_MODE] == COST_MODE_NONE


async def test_fetch_accounts_returns_empty_on_api_error(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
) -> None:
    """_fetch_accounts returns ([], "") when async_get_accounts raises CannotConnect."""
    from dominionsc.exceptions import CannotConnect

    mock_dominionsc_api.async_get_accounts.side_effect = CannotConnect("error")
    result = await _fetch_accounts(hass, {CONF_USERNAME: "user", CONF_PASSWORD: "pass"})
    assert result == ([], "")


async def test_fetch_accounts_returns_empty_on_invalid_auth(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
) -> None:
    """_fetch_accounts returns ([], "") when async_get_accounts raises InvalidAuth."""
    from dominionsc.exceptions import InvalidAuth

    mock_dominionsc_api.async_get_accounts.side_effect = InvalidAuth("bad creds")
    result = await _fetch_accounts(hass, {CONF_USERNAME: "user", CONF_PASSWORD: "pass"})
    assert result == ([], "")


async def test_fetch_accounts_returns_empty_on_api_exception(
    hass: HomeAssistant,
    mock_dominionsc_api: AsyncMock,
) -> None:
    """_fetch_accounts returns ([], "") when async_get_accounts raises ApiException."""
    from dominionsc.exceptions import ApiException

    mock_dominionsc_api.async_get_accounts.side_effect = ApiException("api fail", "https://test.com")
    result = await _fetch_accounts(hass, {CONF_USERNAME: "user", CONF_PASSWORD: "pass"})
    assert result == ([], "")


async def test_options_flow_gas_cost_mode_rate32s_saved(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Options flow end-to-end: gas account present, Rate 32S selected, verified in saved options."""
    # Arrange
    entry = _make_entry(hass, user_input)

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    mock_coordinator.data = MagicMock()
    mock_coordinator.data.accounts = {"ELECTRIC": MagicMock(), "GAS": MagicMock()}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    # Act
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_COST_MODE: COST_MODE_NONE, CONF_GAS_COST_MODE: COST_MODE_RATE_32S},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RECALCULATE_HISTORY: False})

    # Assert
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GAS_COST_MODE] == COST_MODE_RATE_32S


async def test_options_flow_gas_only_change_defaults_recalculate_to_true(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    user_input: dict,
) -> None:
    """Turning on gas while leaving electric mode unchanged still defaults the
    recalculate-history checkbox to True and mentions gas in the description.

    Regression test: previously the default and the description text only
    considered the electric mode, so a gas-only change showed an unchecked
    box and a "changing from Rate 8 to Rate 8" message that never mentioned
    gas at all.
    """
    entry = _make_entry(hass, user_input)
    hass.config_entries.async_update_entry(entry, options={CONF_COST_MODE: COST_MODE_RATE_8})

    mock_coordinator = MagicMock()
    mock_coordinator.recalculation_lock.locked.return_value = False
    mock_coordinator.data = MagicMock()
    mock_coordinator.data.accounts = {"ELECTRIC": MagicMock(), "GAS": MagicMock()}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = mock_coordinator

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_COST_MODE: COST_MODE_RATE_8, CONF_GAS_COST_MODE: COST_MODE_RATE_32S},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "recalculate_history"
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema_defaults = {str(k): k.default() for k in data_schema.schema if hasattr(k, "default")}
    assert schema_defaults[CONF_RECALCULATE_HISTORY] is True

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "gas" in placeholders["summary"].lower()
    assert "Rate 32S" in placeholders["summary"]
