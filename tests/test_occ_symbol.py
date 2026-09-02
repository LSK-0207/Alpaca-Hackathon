from datetime import date
import pytest
from src.execution.occ_symbol import build_occ_symbol, parse_occ_symbol


def test_build_occ_symbol():
    # AAPL Call strike 230.00 expiry 2026-09-18
    sym = build_occ_symbol("AAPL", date(2026, 9, 18), "call", 230.00)
    assert sym == "AAPL260918C00230000"

    # SPY Call strike 608.00 expiry 2025-01-27
    sym_spy = build_occ_symbol("SPY", date(2025, 1, 27), "call", 608.00)
    assert sym_spy == "SPY250127C00608000"

    # TSLA Put strike 195.50 expiry 2024-12-20
    sym_tsla = build_occ_symbol("TSLA", date(2024, 12, 20), "put", 195.50)
    assert sym_tsla == "TSLA241220P00195500"


def test_parse_occ_symbol():
    parsed = parse_occ_symbol("SPY250127C00608000")
    assert parsed["underlying"] == "SPY"
    assert parsed["expiry"] == date(2025, 1, 27)
    assert parsed["option_type"] == "call"
    assert parsed["strike"] == 608.0

    parsed_put = parse_occ_symbol("TSLA241220P00195500")
    assert parsed_put["underlying"] == "TSLA"
    assert parsed_put["expiry"] == date(2024, 12, 20)
    assert parsed_put["option_type"] == "put"
    assert parsed_put["strike"] == 195.50


def test_occ_roundtrip():
    original_sym = "AAPL260918C00230000"
    parsed = parse_occ_symbol(original_sym)
    rebuilt = build_occ_symbol(
        underlying=parsed["underlying"],
        expiry=parsed["expiry"],
        option_type=parsed["option_type"],
        strike=parsed["strike"],
    )
    assert rebuilt == original_sym
