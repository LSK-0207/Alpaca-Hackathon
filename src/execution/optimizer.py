from datetime import date, timedelta
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)


class OptionCandidate(BaseModel):
    symbol: str
    occ_symbol: str
    option_type: Literal["call", "put"]
    strike: float
    expiry: str
    bid: float
    ask: float
    delta: float
    open_interest: int
    score: float
    ev: float
    premium: float


def size_to_contracts(
    position_size_pct: float, premium_per_share: float, buying_power: float
) -> int:
    """
    Sizes number of contracts per spec §13:
        dollar_budget = position_size_pct * buying_power
        return int(dollar_budget // (premium_per_share * 100))
    Never rounds up to 1 if budget is insufficient — do not place an order
    if this returns 0 (would silently exceed risk guard sizing decision).
    """
    if premium_per_share <= 0 or buying_power <= 0 or position_size_pct <= 0:
        return 0
    dollar_budget = position_size_pct * buying_power
    return int(dollar_budget // (premium_per_share * 100.0))


def _expiry_in_window(
    expiry_str: str,
    target_date: date,
    timeframe_days: int,
) -> bool:
    """
    Returns True if the contract's expiry falls within the window:
    [target_date + timeframe_days - 3, target_date + timeframe_days + 5]
    as specified in §13 step 2.
    """
    try:
        # Support ISO format "2026-09-18" or compact "260918"
        if len(expiry_str) == 6 and expiry_str.isdigit():
            from datetime import datetime
            expiry = datetime.strptime(expiry_str, "%y%m%d").date()
        else:
            from datetime import datetime
            expiry = datetime.fromisoformat(expiry_str).date()
    except Exception:
        return False

    window_start = target_date + timedelta(days=timeframe_days - 3)
    window_end = target_date + timedelta(days=timeframe_days + 5)
    return window_start <= expiry <= window_end


def rank_candidates(
    symbol: str,
    direction: Literal["long", "short", "no_trade"],
    target_price: float,
    timeframe_days: int,
    raw_chain: List[Dict[str, Any]],
    current_date: Optional[date] = None,
) -> List[OptionCandidate]:
    """
    Selects and ranks candidate option contracts per §13:

    1. direction -> long: 'call', short: 'put'  (never both, never writing options)
    2. Expiry window: [today + timeframe_days - 3, today + timeframe_days + 5]
    3. Delta filter: abs(delta) between 0.30 and 0.50
    4. Liquidity filter (hard reject):
       - (ask - bid) / midpoint > 0.15  -> reject
       - open_interest < 50             -> reject
       - bid <= 0                       -> reject
    5. P_reach_target: per expiry group, use abs(delta) of contract closest to target_price
    6. Scoring:
       premium_paid = ask
       intrinsic_at_target = max(target_price - strike, 0)  # calls
                           = max(strike - target_price, 0)  # puts
       profit_if_correct = intrinsic_at_target - premium_paid
       loss_if_wrong = premium_paid
       EV = P_reach_target * profit_if_correct - (1 - P_reach_target) * loss_if_wrong
       score = EV / premium_paid
    7. Rank descending by score; return top 5.
    """
    if direction == "no_trade":
        return []

    today = current_date or date.today()
    target_type = "call" if direction == "long" else "put"
    candidates: List[OptionCandidate] = []

    # Step 1 & 2: Filter by option type AND expiry window AND delta AND liquidity
    filtered_raw: List[Dict[str, Any]] = []
    for item in raw_chain:
        opt_type = item.get("option_type", "").lower()
        if opt_type != target_type:
            continue

        expiry_str = str(item.get("expiry", ""))
        if not _expiry_in_window(expiry_str, today, timeframe_days):
            continue

        delta = float(item.get("delta", 0.0))
        abs_delta = abs(delta)
        if not (0.30 <= abs_delta <= 0.50):
            continue

        bid = float(item.get("bid", 0.0))
        ask = float(item.get("ask", 0.0))
        open_interest = int(item.get("open_interest", 0))

        if bid <= 0 or ask <= 0 or ask < bid:
            continue

        midpoint = (bid + ask) / 2.0
        if midpoint <= 0 or ((ask - bid) / midpoint) > 0.15:
            continue

        if open_interest < 50:
            continue

        filtered_raw.append(item)

    if not filtered_raw:
        return []

    # Step 5: Group by expiry, compute P_reach_target per expiry group
    # P_reach_target = abs(delta) of contract whose strike is closest to target_price
    expiry_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in filtered_raw:
        exp = str(item.get("expiry", ""))
        expiry_groups.setdefault(exp, []).append(item)

    p_by_expiry: Dict[str, float] = {}
    for exp, group in expiry_groups.items():
        closest = min(group, key=lambda x: abs(float(x.get("strike", 0.0)) - target_price))
        p = abs(float(closest.get("delta", 0.5)))
        p = max(0.05, min(0.95, p))  # clamp to reasonable probability range
        p_by_expiry[exp] = p

    # Step 6: Score each candidate
    for item in filtered_raw:
        strike = float(item.get("strike", 0.0))
        bid = float(item.get("bid", 0.0))
        ask = float(item.get("ask", 0.0))
        delta = float(item.get("delta", 0.0))
        open_interest = int(item.get("open_interest", 0))
        occ_symbol = item.get("occ_symbol", "")
        expiry = str(item.get("expiry", ""))

        p_reach_target = p_by_expiry.get(expiry, 0.5)
        premium_paid = ask

        if target_type == "call":
            intrinsic = max(target_price - strike, 0.0)
        else:
            intrinsic = max(strike - target_price, 0.0)

        profit_if_correct = intrinsic - premium_paid
        loss_if_wrong = premium_paid

        ev = (p_reach_target * profit_if_correct) - ((1.0 - p_reach_target) * loss_if_wrong)
        score = ev / premium_paid if premium_paid > 0 else 0.0

        candidate = OptionCandidate(
            symbol=symbol,
            occ_symbol=occ_symbol,
            option_type=target_type,
            strike=strike,
            expiry=expiry,
            bid=bid,
            ask=ask,
            delta=delta,
            open_interest=open_interest,
            score=round(score, 4),
            ev=round(ev, 4),
            premium=round(premium_paid, 2),
        )
        candidates.append(candidate)

    # Step 7: Rank descending by score, keep top 5
    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates[:5]
