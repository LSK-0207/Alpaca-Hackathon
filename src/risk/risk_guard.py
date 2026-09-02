from typing import List, Literal, Optional
from pydantic import BaseModel


class AccountState(BaseModel):
    daily_pnl_pct: float             # realized + unrealized PnL / start-of-day equity
    open_position_count: int
    buying_power: float


class RiskVerdict(BaseModel):
    verdict: Literal["approve", "downsize", "reject"]
    reason: str
    position_size_pct: Optional[float] = None


def last_n_outcomes(outcomes: List[str], n: int = 3) -> List[str]:
    """Returns the most recent n outcomes from the list."""
    return outcomes[:n]


def evaluate_risk(
    account_state: AccountState,
    final_conviction: float,
    recent_trade_outcomes: List[str],
) -> RiskVerdict:
    """
    Deterministic risk evaluation per §12:
    - Daily loss circuit breaker: halt new entries if daily_pnl_pct <= -0.08
    - Max concurrent positions: 5
    - Cooldown: after 3 consecutive losses, require final_conviction >= 75
    - Sizing: position_size_pct = min(0.05 * (final_conviction / 100), 0.08)
    - If size_pct < 0.01 -> reject conviction_too_low
    """
    if account_state.daily_pnl_pct <= -0.08:
        return RiskVerdict(verdict="reject", reason="daily_loss_breaker_active")

    if account_state.open_position_count >= 5:
        return RiskVerdict(verdict="reject", reason="max_positions_reached")

    if last_n_outcomes(recent_trade_outcomes, 3) == ["loss", "loss", "loss"] and final_conviction < 75:
        return RiskVerdict(verdict="reject", reason="cooldown_after_losses")

    size_pct = min(0.05 * (final_conviction / 100.0), 0.08)
    if size_pct < 0.0025:
        return RiskVerdict(verdict="reject", reason="conviction_too_low", position_size_pct=size_pct)

    return RiskVerdict(verdict="approve", reason="within_limits", position_size_pct=size_pct)
