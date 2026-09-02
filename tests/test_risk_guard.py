import pytest
from src.risk.risk_guard import AccountState, RiskVerdict, evaluate_risk


def test_daily_loss_breaker():
    account = AccountState(daily_pnl_pct=-0.085, open_position_count=1, buying_power=50000.0)
    verdict = evaluate_risk(account, final_conviction=80.0, recent_trade_outcomes=["win", "win"])
    assert verdict.verdict == "reject"
    assert verdict.reason == "daily_loss_breaker_active"


def test_max_positions_reached():
    account = AccountState(daily_pnl_pct=0.01, open_position_count=5, buying_power=50000.0)
    verdict = evaluate_risk(account, final_conviction=80.0, recent_trade_outcomes=["win"])
    assert verdict.verdict == "reject"
    assert verdict.reason == "max_positions_reached"


def test_cooldown_after_consecutive_losses():
    account = AccountState(daily_pnl_pct=-0.02, open_position_count=1, buying_power=50000.0)

    # Low conviction after 3 losses -> reject
    verdict_low = evaluate_risk(
        account,
        final_conviction=65.0,
        recent_trade_outcomes=["loss", "loss", "loss"],
    )
    assert verdict_low.verdict == "reject"
    assert verdict_low.reason == "cooldown_after_losses"

    # High conviction (>= 75) after 3 losses -> approve
    verdict_high = evaluate_risk(
        account,
        final_conviction=80.0,
        recent_trade_outcomes=["loss", "loss", "loss"],
    )
    assert verdict_high.verdict == "approve"
    assert verdict_high.reason == "within_limits"


def test_normal_approval_and_sizing():
    account = AccountState(daily_pnl_pct=0.02, open_position_count=2, buying_power=100000.0)

    # Conviction 60: size_pct = 0.05 * 0.6 = 0.03 (3%)
    verdict = evaluate_risk(account, final_conviction=60.0, recent_trade_outcomes=["win", "win"])
    assert verdict.verdict == "approve"
    assert verdict.position_size_pct == pytest.approx(0.03, rel=1e-3)

    # Conviction 100: size_pct capped at 0.05 * 1.0 = 0.05 (5%), max cap is 0.08
    verdict_max = evaluate_risk(account, final_conviction=100.0, recent_trade_outcomes=["win"])
    assert verdict_max.verdict == "approve"
    assert verdict_max.position_size_pct == pytest.approx(0.05, rel=1e-3)

    # Very low conviction (e.g. 10 -> size_pct 0.005 < 0.01) -> reject conviction_too_low
    verdict_tiny = evaluate_risk(account, final_conviction=10.0, recent_trade_outcomes=["win"])
    assert verdict_tiny.verdict == "reject"
    assert verdict_tiny.reason == "conviction_too_low"
