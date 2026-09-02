# Code Review Findings — Fixes for Antigravity

Full repo reviewed file-by-file against `final_implementation_spec.md`. Verdict: the implementation is faithful to spec throughout — schema, formulas, agent schemas/prompts, MCP integration pattern, risk guard, optimizer, execution idempotency, and CI setup all match exactly. Three real bugs were found, all isolated to `position_monitor.py` and `mcp_client.py`, plus a few minor polish items. Nothing here requires architectural changes — these are all localized, mechanical fixes.

---

## MUST FIX before enabling the scheduled `trading_cycle.yml` workflow

### 1. A failed quote lookup is indistinguishable from "the option is worth zero" — causes false stop-losses

**Files:** `src/data/mcp_client.py` (`get_option_quote`), `src/execution/position_monitor.py` (`monitor_open_positions`)

**Problem:** `get_option_quote()` returns a zeroed dict (`bid=0, ask=0, mid_price=0`) on *any* failure — network error, tool-name mismatch, malformed response. The position monitor then computes `pct_change = (0 - entry_cost) / entry_cost = -100%`, which trips the stop-loss branch. A transient plumbing failure gets misread as a real market move, triggers a real sell-to-close order, and writes a fabricated −100% `realized_pnl` to the DB.

**Fix:**
```python
# mcp_client.py — return None instead of a zeroed dict on any failure
async def get_option_quote(self, occ_symbol: str) -> Optional[Dict[str, Any]]:
    ...
    if raw is None:
        return None   # was: return _zero
    # inside parse_quote, wherever it currently falls through to `_zero`:
    return None        # was: return _zero
```
```python
# position_monitor.py — skip evaluation (leave position open) on unknown quote
quote = await mcp.get_option_quote(occ_symbol)
if quote is None:
    logger.warning(f"[{occ_symbol}] Could not fetch quote — leaving position open this cycle.")
    continue
bid, ask = float(quote["bid"]), float(quote["ask"])
if bid <= 0 and ask <= 0:
    logger.warning(f"[{occ_symbol}] Quote returned zero bid/ask — leaving position open this cycle.")
    continue
```

### 2. Database marks a position "closed" even when the sell order failed to place

**File:** `src/execution/position_monitor.py`

**Problem:** If `mcp.place_option_order(..., side="sell")` raises, the exception is caught and logged, but execution still falls through to `db.close_position(...)`. The DB and the real Alpaca account can silently diverge — DB says closed, broker still shows it open.

**Fix:**
```python
try:
    await mcp.place_option_order(occ_symbol, qty, side="sell")
except Exception as e:
    logger.error(f"[{occ_symbol}] Sell-to-close failed: {e}. Leaving position open for retry next cycle.")
    continue   # do NOT call db.close_position — next cycle re-evaluates and retries
# only reached if the sell order call succeeded — proceed to compute exit_value/realized_pnl and close in DB
```

### 3. `market_is_open()` fails *open* on error, inverting the codebase's own fail-safe principle

**File:** `src/data/mcp_client.py`

**Problem:** On any exception determining clock status, it logs a warning and `return True`. Every other part of this codebase fails closed (see `orchestrator.py`'s own docstring: *"any unhandled exception halts the cycle — no trade is placed on a partial failure"*). A clock-check error should skip the cycle, not proceed as if markets were open.

**Fix:**
```python
except Exception as e:
    logger.warning(f"Could not determine market open status: {e}. Assuming CLOSED (fail-safe).")
    return False   # was: return True
```

---

## SHOULD FIX — polish, not correctness risk

### 4. Dashboard "Realized P&L" metric shows a meaningless delta indicator
**File:** `dashboard/app.py`, Tab 1
`st.metric("Realized P&L", f"${realized_pnl:,.2f}", delta=f"${realized_pnl:,.2f}", ...)` — the `delta` argument duplicates the main value, so Streamlit renders a redundant up/down arrow next to a number that isn't actually a change-over-time. Either remove the `delta` argument entirely, or compute a real delta (e.g., today's realized P&L vs. yesterday's) if that's wanted.

### 5. Dashboard's "live quote" section is dead code that doesn't do what it says
**File:** `dashboard/app.py`, Tab 1
The open-positions block declares `live_quotes: dict = {}`, never populates it, and just prints an informational message explaining that live quotes aren't available in this context. Not a bug — it's honest about the limitation — but the unused variable and the empty `try/except: pass` around nothing should be cleaned up so it doesn't read as unfinished work. If live unrealized P&L on open positions is wanted, it needs a direct Alpaca Data API call from the dashboard (separate from the MCP subprocess, which can't run inside Streamlit) — worth a scope decision rather than leaving the placeholder in.

### 6. Unused dependency
**File:** `requirements.txt`
`firecrawl-py` is listed but `firecrawl_research.py` calls the REST API directly via `requests`, not the SDK. Harmless, but remove it or switch to using it for consistency.

### 7. Test coverage gap — exactly where the bugs were
**Files:** `tests/test_position_monitor.py`, `tests/test_optimizer.py` (executor path untested)
Current tests only cover the pure, synchronous logic (`should_close_position`, `rank_candidates`, `size_to_contracts`) — not the async orchestration functions (`monitor_open_positions`, `execute_order`) where bugs #1 and #2 actually live. Add tests with a mocked `AlpacaMCPClient`/`DatabaseClient` that specifically assert: a `None` quote leaves the position open (not closed), and a failed sell-order call leaves the position open rather than marking it closed. This is what would have caught both bugs before they shipped, and it's the right regression guard going forward.

---

## Verified correct — no action needed
OCC symbol construction (with correct round-trip test), Wilder's RSI/MACD formulas, the optimizer's EV/ranking algorithm and its liquidity/delta filters, risk guard thresholds and cooldown logic (including correct `closed_at desc` ordering so cooldown checks the *actual* most recent 3 trades), executor idempotency guard, Gemini structured-output usage, DB schema, and the GitHub Actions workflow (concurrency group, `uv` caching, and a `uvx` pre-warm step that wasn't even in the original spec — a good addition).

## Not verifiable from static review — test before trusting the cron schedule
The MCP dynamic tool-discovery layer (`_find_tool` keyword matching, the multi-shape response parsers) is written defensively but has not been confirmed against a live `alpaca-mcp-server` process in this review. Run `python -m src.orchestrator` locally once first, and check the `Discovered tools: [...]` log line plus at least one real option-chain response, before relying on the scheduled workflow.
