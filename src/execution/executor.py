import asyncio
import logging
from typing import Any, Dict, Optional

from src.data.mcp_client import AlpacaMCPClient, get_mcp_client
from src.db.client import DatabaseClient, get_db_client
from src.execution.optimizer import size_to_contracts  # re-exported here per spec §13

logger = logging.getLogger(__name__)

__all__ = ["execute_order", "size_to_contracts"]


async def execute_order(
    occ_symbol: str,
    qty: int,
    decision_id: str,
    mcp_client: Optional[AlpacaMCPClient] = None,
    db_client: Optional[DatabaseClient] = None,
) -> Dict[str, Any]:
    """
    Places a single-leg option market order per spec §14:
    - Idempotency: checks no positions row already exists for this decision_id
      before placing. Guards against retried/overlapping cycles.
    - Order shape exactly per §6 (no 'legs' array, ever):
        {symbol, qty, side: "buy", type: "market", time_in_force: "day"}
    - After the order call returns, polls order status briefly to confirm fill
      before returning — does not assume success from order acceptance alone.
    """
    if qty <= 0:
        raise ValueError(f"Invalid quantity {qty} for order execution.")

    mcp = mcp_client or get_mcp_client()
    db = db_client or get_db_client()

    # Idempotency guard — belt-and-suspenders on top of the workflow-level
    # concurrency group that prevents overlapping cycles (spec §14, §16).
    if db.is_connected() and db.check_existing_position_for_decision(decision_id):
        logger.warning(
            f"Idempotency check: position already exists for decision_id={decision_id}. "
            f"Skipping duplicate order for {occ_symbol}."
        )
        return {"order_id": None, "status": "skipped_duplicate", "raw": None}

    logger.info(
        f"Placing market buy order: {qty} contract(s) of {occ_symbol} "
        f"(decision_id={decision_id})"
    )

    try:
        order_result = await mcp.place_option_order(
            occ_symbol=occ_symbol, qty=qty, side="buy"
        )
    except Exception as e:
        logger.error(
            f"[{decision_id}][{occ_symbol}] MCP order placement failed: {e}",
            exc_info=True,
        )
        raise

    logger.info(f"Order placement response for {occ_symbol}: {order_result}")

    # Extract the order ID from MCP response
    order_id: str = ""
    if isinstance(order_result, dict):
        order_id = str(
            order_result.get("id")
            or order_result.get("order_id")
            or order_result.get("orderId")
            or ""
        )
    elif isinstance(order_result, list):
        # Some MCP responses return a list of TextContent
        import json
        for item in order_result:
            text = getattr(item, "text", None)
            if text:
                try:
                    parsed = json.loads(text)
                    order_id = str(
                        parsed.get("id")
                        or parsed.get("order_id")
                        or parsed.get("orderId")
                        or ""
                    )
                    if order_id:
                        break
                except Exception:
                    pass
    elif hasattr(order_result, "id"):
        order_id = str(getattr(order_result, "id", ""))

    # Brief poll to confirm fill status before returning
    fill_status = "submitted"
    if order_id:
        try:
            await asyncio.sleep(2)  # brief wait for fill
            status_data = await mcp.get_order_status(order_id)
            fill_status = status_data.get("status", "submitted")
            logger.info(
                f"Order {order_id} for {occ_symbol} fill status: {fill_status}"
            )
        except Exception as e:
            logger.warning(
                f"Could not poll order status for {order_id} ({occ_symbol}): {e}"
            )
    else:
        logger.warning(
            f"No order_id extracted from MCP response for {occ_symbol}. "
            f"Cannot confirm fill. Raw response: {order_result}"
        )

    return {
        "order_id": order_id,
        "status": fill_status,
        "raw": order_result,
    }
