"""Tests for dominionsc constants."""
from custom_components.dominionsc.const import (
    DOMAIN, COMMON_NAME, CONF_LOGIN_DATA, CONF_COST_MODE,
    CONF_FIXED_RATE, COST_MODE_NONE, COST_MODE_FIXED,
    COST_MODE_RATE_8, COST_MODE_RATE_6, DEFAULT_FIXED_RATE,
    CONF_EXTENDED_BACKFILL, CONF_EXTENDED_COST_BACKFILL,
    EXTENDED_BACKFILL_DAYS, LOOKBACK_DAYS, clean_service_addr,
)

def test_constants_exist() -> None:
    assert DOMAIN == "dominionsc"
    assert COMMON_NAME == "Dominion Energy SC"

def test_clean_service_addr_basic() -> None:
    assert clean_service_addr("12345") == "12345"
    assert clean_service_addr("a-b-c") == "a_b_c"
    assert clean_service_addr("_start") == "start"
    assert clean_service_addr("end_") == "end"
    # line 38 branch: digits at start get underscore prepended by regex
    assert clean_service_addr("1a2") == "1a2"

