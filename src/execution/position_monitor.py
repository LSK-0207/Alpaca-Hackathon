from datetime import date, datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from src.data.mcp_client import AlpacaMCPClient, get_mcp_client
from src.db.client import DatabaseClient, get_db_client
from src.execution.occ_symbol import parse_occ_symbol

logger = logging.getLogger(__name__)


def should_close_position(
    entry_cost: float,
    current_mid: float,
    expiry_date: date,
    current_date: date,
) -> Optional[str]:
    """
    Evaluates exit conditions for a single position per spec §15:
    - 1 trading day left to expiry (days_to_expiry <= 1) -> 'expiry'
      Force-close to avoid pin risk / assignment complexity near expiration.
    - +50% gain on premium -> 'target_hit'
    - -50% loss on premium -> 'stop_hit'
    - otherwise None (leave open)

    These are polling-based checks (not broker-side bracket orders) per spec §15,
    since Alpaca's take_profit/stop_loss params are documented for multi-leg orders
    only, and this build exclusively places single-leg orders.
    """
    days_to_expiry = (expiry_date - current_date).days
    if days_to_expiry <= 1:
        return "expiry"

    if entry_cost > 0:
        pct_change = (current_mid - entry_cost) / entry_cost
        if pct_change >= 0.50:
            return "target_hit"
        elif pct_change <= -0.50:
            return "stop_hit"

    return None


async def monitor_open_positions(
    mcp_client: Optional[AlpacaMCPClient] = None,
    db_client: Optional[DatabaseClient] = None,
    current_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Iterates through all open positions in the database and closes any that
    meet exit criteria per spec §15.

    For each position:
      1. Fetches live quote via MCP get_option_quote
      2. Computes mid_price = (bid + ask) / 2
      3. Evaluates pct_change = (mid_price - entry_cost) / entry_cost
      4. Closes if: days_to_expiry <= 1, pct_change >= +50%, or pct_change <= -50%
      5. Places sell-to-close market order (side='sell', same OCC symbol)
      6. Updates positions row with exit_value, realized_pnl, close_reason, status='closed'
    """
    mcp = mcp_client or get_mcp_client()
    db = db_client or get_db_client()
    today = current_date or date.today()

    if not db.is_connected():
        logger.warning("Database not connected. Skipping position monitor.")
        return []

    try:
        open_positions = db.get_open_positions()
    except Exception as e:
        logger.error(f"Failed to fetch open positions from DB: {e}")
        return []

    closed_positions: List[Dict[str, Any]] = []

    for pos in open_positions:
        pos_id = pos.get("position_id")
        occ_symbol = pos.get("occ_symbol")
        entry_cost = float(pos.get("entry_cost", 0.0))
        qty = int(pos.get("qty", 1))

        if not occ_symbol or not pos_id:
            logger.warning(f"Skipping position with missing occ_symbol or position_id: {pos}")
            continue

        try:
            parsed = parse_occ_symbol(occ_symbol)
            expiry_date = parsed["expiry"]
        except Exception as e:
            logger.error(
                f"[{occ_symbol}] Failed to parse OCC symbol: {e}"
            )
            continue

        try:
            # Fetch live quote from MCP
            quote = await mcp.get_option_quote(occ_symbol)
            bid = float(quote.get("bid", 0.0))
            ask = float(quote.get("ask", 0.0))

            if bid <= 0 and ask <= 0:
                # If quote fetch failed / returned zero, use mid_price key if present
                current_mid = float(quote.get("mid_price", entry_cost))
                logger.warning(
                    f"[{occ_symbol}] Quote bid/ask are both 0 — using mid_price={current_mid}. "
                    f"Will still evaluate expiry gate."
                )
            else:
                current_mid = (bid + ask) / 2.0

            reason = should_close_position(entry_cost, current_mid, expiry_date, today)

            if reason:
                logger.info(
                    f"[{occ_symbol}] Closing position {pos_id} — reason: {reason} "
                    f"(entry={entry_cost:.2f}, mid={current_mid:.2f}, "
                    f"expiry={expiry_date}, today={today})"
                )

                # Place sell-to-close market order
                try:
                    await mcp.place_option_order(occ_symbol, qty, side="sell")
                    logger.info(f"[{occ_symbol}] Sell-to-close order placed for {qty} contract(s).")
                except Exception as e:
                    logger.error(
                        f"[{occ_symbol}] Failed to place sell-to-close order: {e}",
                        exc_info=True,
                    )
                    # Still update DB — we don't want to re-attempt closes that already fired
                    # In production, reconcile against broker state

                exit_value = current_mid
                realized_pnl = (exit_value - entry_cost) * qty * 100.0
                closed_at = datetime.now(timezone.utc).isoformat()

                db.close_position(
                    position_id=pos_id,
                    exit_value=exit_value,
                    realized_pnl=realized_pnl,
                    close_reason=reason,
                    closed_at=closed_at,
                )
                closed_positions.append(
                    {
                        "position_id": pos_id,
                        "occ_symbol": occ_symbol,
                        "reason": reason,
                        "realized_pnl": round(realized_pnl, 2),
                    }
                )

        except Exception as e:
            logger.error(
                f"[{pos_id}][{occ_symbol}] Unhandled error evaluating position: {e}",
                exc_info=True,
            )

    logger.info(
        f"Position monitor complete. Evaluated {len(open_positions)} open position(s), "
        f"closed {len(closed_positions)}."
    )
    return closed_positions
