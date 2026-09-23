"""Fixtures for Dominion Energy SC tests."""

import sys
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_socket
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from custom_components.dominionsc.const import CONF_LOGIN_DATA

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup() -> None:
    """
    Keep sockets enabled on Windows so the asyncio event loop can start.

    pytest-homeassistant-custom-component disables sockets during setup with
    ``allow_unix_socket=True``. On Linux the ProactorEventLoop self-pipe uses an
    AF_UNIX socketpair (allowed), but on Windows the ProactorEventLoop falls back
    to an AF_INET socketpair, which pytest_socket blocks. Neutralizing
    ``disable_socket`` on Windows lets the session-scoped event-loop runner
    fixture create its self-pipe. This runs ``tryfirst`` so the patch is in place
    before the plugin's own setup hook executes.
    """
    if sys.platform == "win32":
        pytest_socket.disable_socket = lambda *args, **kwargs: None  # type: ignore[assignment]
        pytest_socket.enable_socket()


@pytest.fixture(autouse=True)
def mock_recorder(hass: HomeAssistant) -> Generator[None]:
    """
    Automatically mock recorder for all tests.

    This avoids the 'assert not [True]' error by preventing
    the real recorder component from trying to initialize
    a database after 'hass' has already started.
    """
    with (
        patch("homeassistant.components.recorder.get_instance"),
        patch("homeassistant.components.recorder.async_setup", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""
    return


@pytest.fixture(autouse=True)
def expected_lingering_timers() -> bool:
    """
    Temporary ability to bypass test failures.

    Parametrize to True to bypass the pytest failure.
    """
    return True


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch("custom_components.dominionsc.async_setup_entry", return_value=True) as mock_setup_entry:
        yield mock_setup_entry


@pytest.fixture
def mock_dominionsc_api() -> Generator[MagicMock]:
    """Mock DominionSC API client."""
    with patch("custom_components.dominionsc.config_flow.DominionSC") as mock_api:
        api_instance = MagicMock()
        api_instance.async_login = AsyncMock()
        api_instance.async_get_accounts = AsyncMock(return_value=(["ELECTRIC"], "addr_123"))
        mock_api.return_value = api_instance
        yield api_instance


@pytest.fixture
def mock_tfa_handler() -> MagicMock:
    """Mock TFA handler."""
    handler = MagicMock()
    handler.async_get_tfa_options = AsyncMock(return_value={"sms": "sms", "email": "email"})
    handler.async_select_tfa_option = AsyncMock()
    handler.async_submit_tfa_code = AsyncMock(return_value={"tfa_token": "test"})
    return handler


@pytest.fixture
def user_input() -> dict[str, str]:
    """Return valid user input."""
    return {
        CONF_USERNAME: "test@example.com",
        CONF_PASSWORD: "test_password",
    }


@pytest.fixture
def user_input_with_login_data() -> dict[str, str | dict[str, str]]:
    """Return user input with login data."""
    return {
        CONF_USERNAME: "test@example.com",
        CONF_PASSWORD: "test_password",
        CONF_LOGIN_DATA: {"tfa_token": "test"},
    }
