from datetime import date, timedelta
import pytest
from src.execution.optimizer import rank_candidates, size_to_contracts


def test_size_to_contracts():
    # Buying power $10,000, 5% budget = $500. Premium $2.00/share = $200/contract -> 2 contracts
    assert size_to_contracts(0.05, 2.00, 10_000.0) == 2

    # Budget $50, Premium $1.00/share = $100/contract -> 0 contracts (never round up)
    assert size_to_contracts(0.005, 1.00, 10_000.0) == 0

    # Zero premium -> 0 contracts
    assert size_to_contracts(0.05, 0.0, 10_000.0) == 0

    # Zero buying power -> 0 contracts
    assert size_to_contracts(0.05, 2.00, 0.0) == 0


def _make_call(
    occ: str,
    strike: float,
    bid: float,
    ask: float,
    delta: float,
    oi: int,
    expiry: str,
) -> dict:
    return {
        "symbol": "AAPL",
        "occ_symbol": occ,
        "option_type": "call",
        "strike": strike,
        "expiry": expiry,
        "bid": bid,
        "ask": ask,
        "delta": delta,
        "open_interest": oi,
    }


def test_rank_candidates_filters_and_ordering():
    """
    Tests that delta filter, liquidity filter, and OI filter all reject correctly,
    and that valid candidates are sorted descending by score.
    """
    # Use today + 10 days as expiry so it falls within the window for timeframe_days=10
    today = date.today()
    in_window_expiry = (today + timedelta(days=10)).isoformat()

    raw_chain = [
        # Valid Call 1: strike 230, target 240, ask 3.00, bid 2.90, delta 0.40, OI 200
        _make_call("AAPL260918C00230000", 230.0, 2.90, 3.00, 0.40, 200, in_window_expiry),
        # Reject by Delta: delta 0.15 (< 0.30)
        _make_call("AAPL260918C00250000", 250.0, 0.50, 0.55, 0.15, 300, in_window_expiry),
        # Reject by Liquidity spread >15%: bid 2.00, ask 3.00 → spread/mid = 40%
        _make_call("AAPL260918C00235000_wide", 235.0, 2.00, 3.00, 0.35, 150, in_window_expiry),
        # Reject by Open Interest < 50
        _make_call("AAPL260918C00232500", 232.5, 2.20, 2.30, 0.38, 20, in_window_expiry),
        # Valid Call 2: strike 235, target 240, ask 1.50, bid 1.45, delta 0.32, OI 500
        _make_call("AAPL260918C00235000", 235.0, 1.45, 1.50, 0.32, 500, in_window_expiry),
    ]

    candidates = rank_candidates(
        symbol="AAPL",
        direction="long",
        target_price=240.0,
        timeframe_days=10,
        raw_chain=raw_chain,
        current_date=today,
    )

    # Only the 2 valid calls should survive
    assert len(candidates) == 2, f"Expected 2 candidates, got {len(candidates)}: {candidates}"
    assert all(c.option_type == "call" for c in candidates)
    # Sorted descending by score
    assert candidates[0].score >= candidates[1].score


def test_rank_candidates_expiry_window_filter():
    """
    Contracts outside the expiry window [today+7, today+15] (for timeframe_days=10)
    must be rejected; contracts inside must pass.
    """
    today = date.today()
    timeframe = 10
    # Window: [today + 7, today + 15]
    inside = (today + timedelta(days=10)).isoformat()   # inside window
    too_early = (today + timedelta(days=3)).isoformat()  # before window_start (today+7)
    too_late = (today + timedelta(days=20)).isoformat()  # after window_end (today+15)

    # All three contracts pass delta/liquidity/OI filters
    contract_inside = _make_call("X_IN", 100.0, 2.90, 3.00, 0.40, 200, inside)
    contract_early = _make_call("X_EARLY", 100.0, 2.90, 3.00, 0.40, 200, too_early)
    contract_late = _make_call("X_LATE", 100.0, 2.90, 3.00, 0.40, 200, too_late)
    contract_inside["symbol"] = "SPY"
    contract_early["symbol"] = "SPY"
    contract_late["symbol"] = "SPY"

    candidates_all = rank_candidates(
        symbol="SPY",
        direction="long",
        target_price=110.0,
        timeframe_days=timeframe,
        raw_chain=[contract_inside, contract_early, contract_late],
        current_date=today,
    )
    # Only the inside-window contract should survive
    assert len(candidates_all) == 1, (
        f"Expected 1 candidate after expiry window filter, got {len(candidates_all)}"
    )
    assert candidates_all[0].occ_symbol == "X_IN"


def test_rank_candidates_no_trade_returns_empty():
    assert rank_candidates("AAPL", "no_trade", 230.0, 10, []) == []


def test_rank_candidates_puts():
    """Direction 'short' should filter to put contracts only."""
    today = date.today()
    in_window = (today + timedelta(days=10)).isoformat()

    chain = [
        {
            "symbol": "SPY",
            "occ_symbol": "SPY_P500",
            "option_type": "put",
            "strike": 500.0,
            "expiry": in_window,
            "bid": 3.90,
            "ask": 4.00,
            "delta": -0.40,
            "open_interest": 300,
        },
        {
            "symbol": "SPY",
            "occ_symbol": "SPY_C500",
            "option_type": "call",
            "strike": 500.0,
            "expiry": in_window,
            "bid": 3.90,
            "ask": 4.00,
            "delta": 0.40,
            "open_interest": 300,
        },
    ]
    candidates = rank_candidates(
        symbol="SPY",
        direction="short",
        target_price=480.0,
        timeframe_days=10,
        raw_chain=chain,
        current_date=today,
    )
    assert len(candidates) == 1
    assert candidates[0].option_type == "put"
