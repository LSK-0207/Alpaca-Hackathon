import re
from datetime import date, datetime
from typing import Dict, Any


def build_occ_symbol(underlying: str, expiry: date, option_type: str, strike: float) -> str:
    """
    Constructs an OCC standard option symbol.
    Format: {UNDERLYING}{YYMMDD}{C|P}{STRIKE * 1000, zero-padded to 8 digits}
    Example: build_occ_symbol("AAPL", date(2026, 9, 18), "call", 230.00) -> "AAPL260918C00230000"
    """
    type_char = "C" if option_type.lower() == "call" else "P"
    strike_int = round(strike * 1000)
    return f"{underlying.upper()}{expiry:%y%m%d}{type_char}{strike_int:08d}"


def parse_occ_symbol(occ_symbol: str) -> Dict[str, Any]:
    """
    Parses an OCC option symbol into underlying, expiry, option_type, and strike.
    Example: "SPY250127C00608000" -> {'underlying': 'SPY', 'expiry': date(2025, 1, 27), 'option_type': 'call', 'strike': 608.0}
    """
    pattern = r"^([A-Z]+)(\d{6})([CP])(\d{8})$"
    match = re.match(pattern, occ_symbol.upper())
    if not match:
        raise ValueError(f"Invalid OCC symbol format: {occ_symbol}")

    underlying, date_str, type_char, strike_str = match.groups()
    expiry = datetime.strptime(date_str, "%y%m%d").date()
    option_type = "call" if type_char == "C" else "put"
    strike = int(strike_str) / 1000.0

    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": option_type,
        "strike": strike,
    }
