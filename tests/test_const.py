"""Tests for dominionsc constants."""

from custom_components.dominionsc.const import (
    COMMON_NAME,
    DOMAIN,
    clean_service_addr,
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
