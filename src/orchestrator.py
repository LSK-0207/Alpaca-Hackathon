"""
Orchestrator — full autonomous trading cycle per spec §16.

Run with:
    python -m src.orchestrator
"""
import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

from src.agents.debate import run_analyst, run_critic
from src.data.firecrawl_research import research_async
from src.data.mcp_client import AlpacaMCPClient, get_mcp_client
from src.db.client import DatabaseClient, get_db_client
from src.execution.executor import execute_order
from src.execution.optimizer import rank_candidates, size_to_contracts
from src.execution.position_monitor import monitor_open_positions
from src.risk.risk_guard import AccountState, evaluate_risk
from src.signals.technicals import compute_signals

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orchestrator")

# Minimum bar history needed for MACD(12,26,9): 35 bars; we pull 60 for margin
_MIN_BARS_REQUIRED = 35
_BARS_TO_FETCH = 60


def load_watchlist() -> List[str]:
    """Loads symbols from config/watchlist.yaml."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"
    if not config_path.exists():
        logger.warning(
            f"Watchlist config not found at {config_path}. Using fallback list."
        )
        return ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"]

    with open(config_path, "r") as f:
        data = yaml.safe_load(f)
        return data.get("symbols", [])


def _parse_account_state(account_data: Dict[str, Any], open_positions: List) -> AccountState:
    """
    Extracts AccountState from live Alpaca account data.
    Field names match Alpaca's account API response.
    """
    try:
        buying_power = float(account_data.get("buying_power", 100_000.0))
    except (TypeError, ValueError):
        buying_power = 100_000.0

    # daily_pnl_pct: (equity - last_equity) / last_equity
    # Alpaca provides 'equity' and 'last_equity' in account data
    try:
        equity = float(account_data.get("equity", 0.0))
        last_equity = float(account_data.get("last_equity", equity))
        if last_equity > 0:
            daily_pnl_pct = (equity - last_equity) / last_equity
        else:
            daily_pnl_pct = 0.0
    except (TypeError, ValueError):
        daily_pnl_pct = 0.0

    return AccountState(
        daily_pnl_pct=daily_pnl_pct,
        open_position_count=len(open_positions),
        buying_power=buying_power,
    )


async def run_cycle() -> None:
    """
    Single autonomous trading cycle per spec §16 pseudocode.

    Flow:
      1. Market clock check via MCP — exit early if closed
      2. Pull account state from MCP get_account_info
      3. For each symbol in watchlist:
         a. Compute RSI/MACD signals from live historical bars (MCP)
         b. Firecrawl research digest
         c. Analyst agent (Gemini)
         d. Critic agent (Gemini) — only if Analyst chose a direction
         e. Risk guard (deterministic, no LLM)
         f. Strike selection & ranking (optimizer, using live option chain from MCP)
         g. Execute top-ranked contract (executor)
      4. Position monitor — check all open positions for exit conditions
    Any unhandled exception halts the cycle — no trade is placed on a partial failure.
    """
    cycle_started_at = datetime.now(timezone.utc).isoformat()
    logger.info(f"=== Trading cycle starting at {cycle_started_at} ===")

    # Create a fresh MCP client per cycle — never reuse a previously-disconnected singleton
    mcp: AlpacaMCPClient = AlpacaMCPClient()
    db: DatabaseClient = get_db_client()

    try:
        await mcp.connect()

        # 1. Market clock check
        is_open = await mcp.market_is_open()
        if not is_open:
            logger.info("Market is closed. Trading cycle terminating early.")
            return

        # 2. Pull live account state
        try:
            account_data = await mcp.get_account_info()
            logger.info(f"Account data retrieved. Keys: {list(account_data.keys())}")
        except Exception as e:
            logger.error(f"Failed to fetch account info from MCP: {e}", exc_info=True)
            account_data = {}

        open_positions_list: List[Dict[str, Any]] = []
        if db.is_connected():
            try:
                open_positions_list = db.get_open_positions()
            except Exception as e:
                logger.error(f"Failed to fetch open positions from DB: {e}")
        account = _parse_account_state(account_data, open_positions_list)
        logger.info(
            f"AccountState: daily_pnl_pct={account.daily_pnl_pct:.4f}, "
            f"open_positions={account.open_position_count}, "
            f"buying_power=${account.buying_power:,.2f}"
        )

        recent_outcomes: List[str] = (
            db.get_recent_trade_outcomes() if db.is_connected() else []
        )
        watchlist = load_watchlist()
        logger.info(f"Watchlist: {watchlist}")

        for symbol in watchlist:
            logger.info(f"--- Processing {symbol} ---")

            # A. Compute signals from live historical bars
            signals: Dict[str, Any] = {}
            try:
                close_prices = await mcp.get_historical_bars(
                    symbol, timeframe="1Day", limit=_BARS_TO_FETCH
                )
                if len(close_prices) < _MIN_BARS_REQUIRED:
                    logger.warning(
                        f"[{symbol}] Only {len(close_prices)} bars returned "
                        f"(need {_MIN_BARS_REQUIRED}). Skipping symbol."
                    )
                    continue
                signals = compute_signals(close_prices)
                logger.info(f"[{symbol}] Signals: {signals}")
            except Exception as e:
                logger.error(
                    f"[{symbol}] Failed to compute signals: {e}", exc_info=True
                )
                continue

            # B. Web research via Firecrawl (async — does not block event loop)
            try:
                research_summary = await research_async(symbol)
            except Exception as e:
                logger.warning(f"[{symbol}] Firecrawl research failed: {e}. Continuing with empty summary.")
                research_summary = ""

            # C. Analyst Agent
            try:
                analyst = run_analyst(symbol, signals, research_summary)
                logger.info(
                    f"[{symbol}] Analyst: direction={analyst.direction}, "
                    f"confidence={analyst.confidence_score}, "
                    f"target_price={analyst.target_price}"
                )
            except Exception as e:
                logger.error(
                    f"[{symbol}] Analyst agent failed: {e}", exc_info=True
                )
                continue

            if analyst.direction == "no_trade":
                logger.info(f"[{symbol}] Analyst decided no_trade — skipping.")
                if db.is_connected():
                    try:
                        db.insert_decision({
                            "cycle_started_at": cycle_started_at,
                            "symbol": symbol,
                            "signals": signals,
                            "research_summary": research_summary,
                            "analyst_output": analyst.model_dump(),
                            "skip_reason": "no_trade_thesis",
                        })
                    except Exception as e:
                        logger.error(f"[{symbol}] DB insert_decision (no_trade) failed: {e}")
                continue

            # D. Critic Agent (pre-mortem) — only called when direction != no_trade
            try:
                critic = run_critic(analyst)
                logger.info(
                    f"[{symbol}] Critic: penalty={critic.conviction_penalty}"
                )
            except Exception as e:
                logger.error(
                    f"[{symbol}] Critic agent failed: {e}", exc_info=True
                )
                continue

            final_conviction = max(
                0.0,
                float(analyst.confidence_score - critic.conviction_penalty),
            )
            logger.info(
                f"[{symbol}] Final conviction: {final_conviction} "
                f"(analyst={analyst.confidence_score} - critic={critic.conviction_penalty})"
            )

            # E. Risk Guard — deterministic, no LLM
            risk_verdict = evaluate_risk(account, final_conviction, recent_outcomes)
            logger.info(
                f"[{symbol}] Risk verdict: {risk_verdict.verdict} — {risk_verdict.reason}"
            )

            if risk_verdict.verdict == "reject":
                if db.is_connected():
                    try:
                        db.insert_decision({
                            "cycle_started_at": cycle_started_at,
                            "symbol": symbol,
                            "signals": signals,
                            "research_summary": research_summary,
                            "analyst_output": analyst.model_dump(),
                            "critic_output": critic.model_dump(),
                            "final_conviction": final_conviction,
                            "risk_verdict": risk_verdict.verdict,
                            "risk_reason": risk_verdict.reason,
                            "skip_reason": risk_verdict.reason,
                        })
                    except Exception as e:
                        logger.error(f"[{symbol}] DB insert_decision (risk_reject) failed: {e}")
                continue

            # F. Strike Selection & Ranking — live option chain from MCP
            try:
                raw_chain = await mcp.get_option_chain_or_snapshots(symbol)
                logger.info(
                    f"[{symbol}] Option chain: {len(raw_chain)} contracts retrieved."
                )
            except Exception as e:
                logger.error(
                    f"[{symbol}] Failed to fetch option chain: {e}", exc_info=True
                )
                if db.is_connected():
                    try:
                        db.insert_decision({
                            "cycle_started_at": cycle_started_at,
                            "symbol": symbol,
                            "signals": signals,
                            "research_summary": research_summary,
                            "analyst_output": analyst.model_dump(),
                            "critic_output": critic.model_dump(),
                            "final_conviction": final_conviction,
                            "risk_verdict": risk_verdict.verdict,
                            "risk_reason": risk_verdict.reason,
                            "candidates": [],
                            "skip_reason": "no_liquid_contracts",
                        })
                    except Exception as db_e:
                        logger.error(f"[{symbol}] DB insert_decision (chain_error) failed: {db_e}")
                continue

            candidates = rank_candidates(
                symbol=symbol,
                direction=analyst.direction,
                target_price=analyst.target_price,
                timeframe_days=analyst.timeframe_days,
                raw_chain=raw_chain,
            )
            logger.info(
                f"[{symbol}] Optimizer: {len(candidates)} candidate(s) after filters."
            )

            if not candidates:
                logger.info(f"[{symbol}] No liquid candidates — skipping.")
                if db.is_connected():
                    try:
                        db.insert_decision({
                            "cycle_started_at": cycle_started_at,
                            "symbol": symbol,
                            "signals": signals,
                            "research_summary": research_summary,
                            "analyst_output": analyst.model_dump(),
                            "critic_output": critic.model_dump(),
                            "final_conviction": final_conviction,
                            "risk_verdict": risk_verdict.verdict,
                            "risk_reason": risk_verdict.reason,
                            "candidates": [],
                            "skip_reason": "no_liquid_contracts",
                        })
                    except Exception as e:
                        logger.error(f"[{symbol}] DB insert_decision (no_contracts) failed: {e}")
                continue

            top = candidates[0]
            size_pct = risk_verdict.position_size_pct or 0.05
            qty = size_to_contracts(size_pct, top.premium, account.buying_power)

            logger.info(
                f"[{symbol}] Top contract: {top.occ_symbol} "
                f"(premium={top.premium}, score={top.score}, delta={top.delta}) "
                f"→ qty={qty} contract(s)"
            )

            if qty < 1:
                logger.info(
                    f"[{symbol}] Size too small for budget "
                    f"(budget={size_pct * account.buying_power:.2f}, "
                    f"contract_cost={top.premium * 100:.2f}). Skipping."
                )
                if db.is_connected():
                    try:
                        db.insert_decision({
                            "cycle_started_at": cycle_started_at,
                            "symbol": symbol,
                            "signals": signals,
                            "research_summary": research_summary,
                            "analyst_output": analyst.model_dump(),
                            "critic_output": critic.model_dump(),
                            "final_conviction": final_conviction,
                            "risk_verdict": risk_verdict.verdict,
                            "risk_reason": risk_verdict.reason,
                            "position_size_pct": size_pct,
                            "candidates": [c.model_dump() for c in candidates],
                            "skip_reason": "size_too_small",
                        })
                    except Exception as e:
                        logger.error(f"[{symbol}] DB insert_decision (size_small) failed: {e}")
                continue

            # G. Execution — top-ranked candidate executes automatically (no confirmation step)
            decision_id: Optional[str] = None
            if db.is_connected():
                try:
                    decision_id = db.insert_decision({
                        "cycle_started_at": cycle_started_at,
                        "symbol": symbol,
                        "signals": signals,
                        "research_summary": research_summary,
                        "analyst_output": analyst.model_dump(),
                        "critic_output": critic.model_dump(),
                        "final_conviction": final_conviction,
                        "risk_verdict": risk_verdict.verdict,
                        "risk_reason": risk_verdict.reason,
                        "position_size_pct": size_pct,
                        "candidates": [c.model_dump() for c in candidates],
                        "chosen_contract": top.model_dump(),
                    })
                    logger.info(f"[{symbol}] Decision row inserted: {decision_id}")
                except Exception as e:
                    logger.error(f"[{symbol}] DB insert_decision (execution) failed: {e}")

            try:
                order = await execute_order(
                    occ_symbol=top.occ_symbol,
                    qty=qty,
                    decision_id=decision_id or "local",
                    mcp_client=mcp,
                    db_client=db,
                )
                logger.info(
                    f"[{symbol}] Order result: order_id={order.get('order_id')}, "
                    f"status={order.get('status')}"
                )

                if order.get("status") == "skipped_duplicate":
                    logger.warning(f"[{symbol}] Duplicate order skipped by idempotency guard.")
                    continue

                if db.is_connected() and decision_id:
                    if order.get("order_id"):
                        try:
                            db.update_decision_order_id(decision_id, order["order_id"])
                        except Exception as e:
                            logger.error(f"[{symbol}] Failed to update order_id on decision: {e}")

                    try:
                        db.insert_position({
                            "decision_id": decision_id,
                            "symbol": symbol,
                            "occ_symbol": top.occ_symbol,
                            "option_type": top.option_type,
                            "qty": qty,
                            "entry_cost": top.premium,
                        })
                        logger.info(f"[{symbol}] Position row inserted.")
                    except Exception as e:
                        logger.error(f"[{symbol}] Failed to insert position row: {e}")

            except Exception as e:
                logger.error(
                    f"[{symbol}] Order execution failed: {e}", exc_info=True
                )

            # Sleep to respect Gemini API free tier rate limit (15 Requests Per Minute)
            await asyncio.sleep(5)

        # 4. Position Monitor — checks every open position, closes as needed
        logger.info("=== Running position monitor ===")
        try:
            closed = await monitor_open_positions(mcp_client=mcp, db_client=db)
            if closed:
                logger.info(f"Position monitor closed {len(closed)} position(s): {closed}")
        except Exception as e:
            logger.error(f"Position monitor failed: {e}", exc_info=True)

    except Exception as e:
        logger.error(
            f"Unhandled exception during trading cycle: {e}", exc_info=True
        )
        # Fail-safe: any unhandled exception here means no further orders this cycle.
        # The MCP session is still closed in finally.
    finally:
        try:
            await mcp.disconnect()
        except Exception as e:
            logger.warning(f"Error during MCP disconnect: {e}")
        logger.info(f"=== Trading cycle complete (started: {cycle_started_at}) ===")


if __name__ == "__main__":
    asyncio.run(run_cycle())
