# Final Implementation Specification — Alpaca Options Alpha Agent
**This document supersedes `implementation_spec.md` in full.** It reflects the lean, fully-autonomous scope agreed after competitive research and a complexity audit. Every formula, threshold, schema, and integration pattern below is fixed — nowhere in this system should the coding agent need to invent a design decision. Where an external library's exact API surface matters, it has been verified against current documentation (cited inline) rather than recalled from memory, specifically to prevent hallucinated function signatures.

---

## 1. Objective & Non-Negotiables

An autonomous agent that trades **single-leg, long-only options** (long calls on bullish theses, long puts on bearish theses — never writes/sells options, so max loss per trade is always capped at the premium paid) on Alpaca paper trading, for the Options Alpha Agents track. Two LLM calls per candidate symbol per cycle (Analyst, then Critic), a deterministic risk guard, a deterministic ranked-candidate contract selector, execution via Alpaca's MCP server, and a position monitor that manages exits. No human approval step exists anywhere in the trading path — the system must run unattended for the entire judged week.

## 2. Tech Stack (final)

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| Trading interface | Alpaca MCP Server (`alpaca-mcp-server`, PyPI), stdio transport, dynamic tool discovery |
| MCP client | `mcp` Python SDK |
| LLM | Google Gemini only — `gemini-2.5-flash`, via the `google-genai` SDK. No second provider. |
| Web research | Firecrawl, one-shot call per symbol per cycle — no monitors, no recurring infrastructure |
| Database | Supabase (hosted Postgres, free tier) |
| Scheduling | GitHub Actions — one cron schedule, one workflow |
| Dashboard | Streamlit, deployed on Streamlit Community Cloud |
| Math | `pandas`, `numpy` — indicators are hand-written per §9, no external TA library needed |

## 3. Repository Structure

```
alpaca-alpha-agent/
├── .github/workflows/
│   ├── trading_cycle.yml
│   └── tests.yml
├── config/
│   └── watchlist.yaml
├── src/
│   ├── data/
│   │   ├── mcp_client.py        # session mgmt, tool discovery, typed wrapper functions
│   │   └── firecrawl_research.py
│   ├── signals/
│   │   └── technicals.py        # RSI(14), MACD(12,26,9)
│   ├── agents/
│   │   ├── prompts.py
│   │   ├── schemas.py           # pydantic models — Analyst, Critic
│   │   └── debate.py            # Gemini calls + validation
│   ├── risk/
│   │   └── risk_guard.py
│   ├── execution/
│   │   ├── occ_symbol.py        # OCC option symbol construction
│   │   ├── optimizer.py         # ranked candidate selection
│   │   ├── executor.py          # places order, idempotency
│   │   └── position_monitor.py  # TP/SL/expiry exit logic
│   ├── db/
│   │   ├── schema.sql
│   │   └── client.py
│   └── orchestrator.py
├── dashboard/
│   └── app.py
├── tests/
│   ├── test_risk_guard.py
│   ├── test_occ_symbol.py
│   ├── test_optimizer.py
│   └── test_position_monitor.py
├── .env.example
├── requirements.txt
└── README.md
```

## 4. Environment Variables

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_PAPER_TRADE=true
GEMINI_API_KEY=
FIRECRAWL_API_KEY=
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
```

---

## 5. Database — Full DDL

Two tables. Every column exists because a specific downstream consumer (the agent's own reasoning, the risk guard's audit trail, or the dashboard) needs it — nothing speculative.

```sql
create extension if not exists "uuid-ossp";

create table decisions (
    decision_id uuid primary key default uuid_generate_v4(),
    cycle_started_at timestamptz not null,
    symbol text not null,
    signals jsonb not null,              -- {rsi, macd_line, macd_signal, macd_histogram, latest_price}
    research_summary text,               -- short Firecrawl digest fed to the Analyst
    analyst_output jsonb,                -- full structured output, see §11
    critic_output jsonb,                 -- {failure_scenario, conviction_penalty}
    final_conviction numeric,            -- analyst.confidence_score - critic.conviction_penalty, floored at 0
    risk_verdict text check (risk_verdict in ('approve','downsize','reject')),
    risk_reason text,
    position_size_pct numeric,
    candidates jsonb,                    -- ranked list of up to 5 contract candidates with scores, see §13
    chosen_contract jsonb,               -- the executed candidate, null if none executed
    alpaca_order_id text,
    skip_reason text,                    -- e.g. 'no_trade_thesis', 'no_liquid_contracts', 'size_too_small'
    created_at timestamptz not null default now()
);

create table positions (
    position_id uuid primary key default uuid_generate_v4(),
    decision_id uuid not null references decisions(decision_id),
    symbol text not null,
    occ_symbol text not null,
    option_type text not null check (option_type in ('call','put')),
    qty integer not null,
    entry_cost numeric not null,         -- premium paid, per share (multiply by qty*100 for total $)
    opened_at timestamptz not null default now(),
    closed_at timestamptz,
    exit_value numeric,                  -- premium received on close, per share
    realized_pnl numeric,                -- (exit_value - entry_cost) * qty * 100
    close_reason text check (close_reason in ('target_hit','stop_hit','expiry')),
    status text not null default 'open' check (status in ('open','closed'))
);

create index on decisions (symbol, created_at desc);
create index on positions (status);
```

**What's deliberately not here, and why:** no `watchlist` table (static config file), no per-agent-role normalized table (only 2 agent calls now, so `analyst_output`/`critic_output` as JSON columns on one row is simpler than a join), no memory/journal table (query `decisions` directly for a symbol's history when needed), no shadow-position tracking (cut from scope), no portfolio-Greeks columns (not enforced as hard gates in this build — see §12).

---

## 6. Alpaca MCP Integration

**Connection pattern** (stdio transport, one session per cycle):
```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

server_params = StdioServerParameters(
    command="uvx",
    args=["alpaca-mcp-server"],   # confirm exact invocation in the package's current README before first run
    env={
        "ALPACA_API_KEY": os.environ["ALPACA_API_KEY"],
        "ALPACA_SECRET_KEY": os.environ["ALPACA_SECRET_KEY"],
        "ALPACA_PAPER_TRADE": "true",
    },
)

async with stdio_client(server_params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tool_catalog = {t.name: t for t in (await session.list_tools()).tools}
        # ... use tool_catalog to look up and call tools by name; do not hardcode names beyond
        # what's confirmed below.
```
Confirmed tool names to anchor against: `get_account_info`, `place_option_market_order`. Everything else (option snapshots/quotes, positions, clock) — resolve from `list_tools()` at runtime and wrap in `mcp_client.py` so the rest of the codebase calls clean Python functions, never raw MCP calls directly.

### Placing a single-leg option order — exact shape
This is the part most likely to get over-complicated by analogy to multi-leg spreads. **It should not be.** Alpaca's own documentation shows a single-leg option order is a plain order using the option's OCC symbol as the `symbol` field — the `legs` parameter (`List[OptionLegRequest]`) is **only used for multi-leg orders (2-4 legs)**, which this build never places:
```json
{
  "symbol": "AAPL260918C00230000",
  "qty": "1",
  "side": "buy",
  "type": "market",
  "time_in_force": "day"
}
```
`place_option_market_order` (or whatever the MCP tool wraps this as) should be called with exactly this shape for every trade in this build — no `legs` array, ever.

### OCC option symbol construction (`execution/occ_symbol.py`)
Format: `{UNDERLYING}{YYMMDD}{C|P}{STRIKE * 1000, zero-padded to 8 digits}`
```python
def build_occ_symbol(underlying: str, expiry: date, option_type: str, strike: float) -> str:
    type_char = "C" if option_type == "call" else "P"
    strike_int = round(strike * 1000)
    return f"{underlying}{expiry:%y%m%d}{type_char}{strike_int:08d}"

# build_occ_symbol("AAPL", date(2026, 9, 18), "call", 230.00) == "AAPL260918C00230000"
```
Write `tests/test_occ_symbol.py` as a round-trip test against known real examples (e.g. `SPY250127C00608000` = SPY, 2025-01-27, Call, strike $608.00).

### Reading option data
Option snapshots (via whichever discovered MCP tool wraps `OptionsSnapshot`) contain latest trade, latest bid/ask, **Greeks (delta, gamma, theta, vega, rho), and implied volatility** per contract. Delta is what the strike-selection algorithm in §13 depends on — confirm the discovered tool's response includes a `greeks.delta` (or equivalently named) field before building the optimizer, since this is a hard dependency.

---

## 7. Firecrawl Integration

One function, called once per candidate symbol per cycle: `research(symbol: str) -> str`, returning a short (2-4 sentence) digest of recent news/analyst commentary for that ticker. No monitors, no recurring watch jobs, no separate trigger path. If Firecrawl returns nothing useful, pass an empty string to the Analyst prompt — do not block the cycle on this.

---

## 8. Gemini Integration — Exact API Usage

Verified against current `google-genai` SDK docs. Structured output requires **both** `response_mime_type='application/json'` **and** `response_schema` — `response_mime_type` alone is only a soft hint and does not guarantee valid JSON.

```python
from google import genai
from google.genai import types
from pydantic import BaseModel

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=prompt,
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=AnalystOutput,   # a pydantic BaseModel, see §11
        temperature=0,                    # deterministic-leaning output for structured extraction
    ),
)
result: AnalystOutput = response.parsed   # SDK gives you the validated pydantic instance directly
```
`response.parsed` is only populated when `response_schema` is a pydantic model — use this, not manual `json.loads(response.text)`. This removes the entire "Groq wraps JSON in markdown fences" failure mode from the original spec by construction, since it's not a prompt-compliance issue here but an SDK-enforced schema.

Do not duplicate the schema in the prompt text itself (redundant and documented to reduce output quality) — the schema is enforced by `response_schema`, so the prompt should describe the *task*, not the *shape*.

---

## 9. Signal Computation — Exact Formulas

Two indicators only. No regime classifier, no volatility percentile — the Analyst agent weighs these directly in its reasoning rather than passing through a hard-coded gate.

**RSI(14), Wilder's smoothing:**
```
delta[t] = close[t] - close[t-1]
gain[t] = max(delta[t], 0)
loss[t] = max(-delta[t], 0)
avg_gain[14] = mean(gain[1..14])          # seed
avg_loss[14] = mean(loss[1..14])          # seed
avg_gain[t] = (avg_gain[t-1] * 13 + gain[t]) / 14     # t > 14
avg_loss[t] = (avg_loss[t-1] * 13 + loss[t]) / 14     # t > 14
RS = avg_gain[t] / avg_loss[t]
RSI[t] = 100 - (100 / (1 + RS))
```

**MACD(12, 26, 9):**
```
EMA[t, N] = close[t] * k + EMA[t-1, N] * (1 - k),   k = 2 / (N + 1)
EMA[N, N] = mean(close[1..N])   # seed with SMA

macd_line[t]   = EMA[t, 12] - EMA[t, 26]
signal_line[t] = EMA(macd_line, 9)[t]
histogram[t]   = macd_line[t] - signal_line[t]
```

Both need at least 26+9 = 35 trading days of historical daily bars, pulled via the MCP-discovered bars tool. Pull 60 days to have margin.

---

## 10. Watchlist Config

`config/watchlist.yaml` — a static list of 15-20 liquid, optionable symbols (large-cap tech, index ETFs). Not a database table.

---

## 11. Agent Layer

### Pydantic schemas (`agents/schemas.py`)
```python
from pydantic import BaseModel, Field
from typing import Literal

class Case(BaseModel):
    thesis: str
    evidence: list[str]

class AnalystOutput(BaseModel):
    long_case: Case
    short_case: Case
    direction: Literal["long", "short", "no_trade"]
    target_price: float
    timeframe_days: int
    confidence_score: int = Field(ge=0, le=100)
    why_this_side_won: str   # must reference specific evidence from the case NOT chosen

class CriticOutput(BaseModel):
    failure_scenario: str
    conviction_penalty: int = Field(ge=0, le=100)
```

### Analyst agent (`agents/debate.py`)
System prompt (full text — use verbatim, do not paraphrase during implementation):
> You are the analyst on a disciplined trading desk. For {symbol}, you must build BOTH a long case and a short case before deciding anything — you are not allowed to skip either side. Use only the data given; every piece of evidence must reference a specific number from the signals or a specific fact from the research summary, never a generic claim. After building both cases, decide which side wins and explain why using evidence from the losing case specifically — you must show you seriously considered being wrong. If neither case is meaningfully stronger, or the setup is too ambiguous to act on, set direction to "no_trade".
>
> Signals: {signals_json}
> Research: {research_summary}

Call with `response_schema=AnalystOutput`. **This structural requirement — long_case and short_case both mandatory, `why_this_side_won` required to cite the losing side's evidence — is the actual bias mitigation.** An instruction to "be unbiased" alone does not reliably change model behavior; forcing the model to construct and reference the counter-case does.

### Critic agent (pre-mortem)
System prompt:
> Assume the following trade has already been placed and has failed: {analyst_output_json}. Identify the single strongest, most specific reason it failed — a scenario genuinely different from the short_case/long_case already considered, not a restatement of it. Rate how much this new information should reduce confidence in the original thesis, from 0 (irrelevant, ignore it) to 100 (this alone should kill the trade).

Call with `response_schema=CriticOutput`.

**Blending:** `final_conviction = max(0, analyst_output.confidence_score - critic_output.conviction_penalty)`

If `analyst_output.direction == "no_trade"`, skip the Critic call entirely and persist the decision row with `skip_reason='no_trade_thesis'` — don't spend a Gemini call critiquing a trade that isn't happening.

---

## 12. Risk Guard (`risk/risk_guard.py`) — pure code, no LLM, deterministic

Pulled fresh from the live account (via MCP) every cycle:

| Rule | Value |
|---|---|
| Max concurrent open positions | 5 |
| Max single-position size | 8% of current buying power |
| Daily loss circuit breaker | halt new entries if today's realized + unrealized P&L ≤ **−8%** of start-of-day equity |
| Cooldown | after 3 consecutive losing trades, require `final_conviction ≥ 75` to re-enter |
| Sizing (Kelly-lite) | `position_size_pct = min(0.05 * (final_conviction / 100), 0.08)` |

Portfolio-level Greeks caps (delta/theta/vega bands) are **not enforced** in this build — deliberately cut for scope. Greeks are still visible on the dashboard as information, sourced from the option snapshot, but nothing gates on them.

```python
def evaluate(account_state, final_conviction, recent_trade_outcomes) -> RiskVerdict:
    if account_state.daily_pnl_pct <= -0.08:
        return RiskVerdict(verdict="reject", reason="daily_loss_breaker_active")
    if account_state.open_position_count >= 5:
        return RiskVerdict(verdict="reject", reason="max_positions_reached")
    if last_n_outcomes(recent_trade_outcomes, 3) == ["loss","loss","loss"] and final_conviction < 75:
        return RiskVerdict(verdict="reject", reason="cooldown_after_losses")
    size_pct = min(0.05 * (final_conviction / 100), 0.08)
    if size_pct < 0.01:   # too small to be worth a contract at most account sizes
        return RiskVerdict(verdict="reject", reason="conviction_too_low", position_size_pct=size_pct)
    return RiskVerdict(verdict="approve", reason="within_limits", position_size_pct=size_pct)
```

---

## 13. Strike Selection & Ranking (`execution/optimizer.py`)

Given the Analyst's `direction`, `target_price`, `timeframe_days` for a symbol:

1. **Direction mapping:** `direction == "long"` → search calls. `direction == "short"` → search puts. (Never both, never writing options.)
2. **Expiry window:** target expiries within `[today + timeframe_days - 3, today + timeframe_days + 5]` trading days — pick nearest available listed expiry(ies) in that window.
3. **Delta filter:** candidate contracts with `abs(delta)` between **0.30 and 0.50** (avoids both deep-OTM lottery tickets and deep-ITM stock-substitutes).
4. **Liquidity filter (hard reject):** `(ask - bid) / midpoint > 0.15`, or `open_interest < 50`, or `bid <= 0`.
5. **Probability proxy:** for each expiry under consideration, find the contract whose strike is closest to `target_price` and use its `abs(delta)` as `P_reach_target` for all candidates at that expiry (delta ≈ risk-neutral probability of finishing ITM — standard options-market approximation).
6. **Per-candidate scoring:**
```
premium_paid = candidate.ask
intrinsic_at_target = max(target_price - candidate.strike, 0)          # calls
                     = max(candidate.strike - target_price, 0)          # puts
profit_if_correct = intrinsic_at_target - premium_paid
loss_if_wrong = premium_paid   # long options: max loss is always the premium

EV = P_reach_target * profit_if_correct - (1 - P_reach_target) * loss_if_wrong
score = EV / premium_paid       # normalizes so cheaper contracts with equal EV rank higher
```
7. **Rank** all surviving candidates by `score` descending. **Keep the top 5** — this full ranked list (not just the winner) is what gets written to `decisions.candidates` and shown on the dashboard, satisfying the transparency requirement without a blocking approval step.
8. If zero candidates survive the liquidity/delta filters, persist `skip_reason='no_liquid_contracts'` and stop for that symbol this cycle.

**Sizing to contracts** (`execution/executor.py`):
```python
def size_to_contracts(position_size_pct, premium_per_share, buying_power) -> int:
    dollar_budget = position_size_pct * buying_power
    return int(dollar_budget // (premium_per_share * 100))
```
If this returns 0, persist `skip_reason='size_too_small'` and do not place an order — do not round up to 1 contract, since that would silently exceed the risk guard's sizing decision.

---

## 14. Execution (`execution/executor.py`)

The top-ranked candidate from §13 executes automatically — no confirmation step. Order shape exactly as specified in §6. **Idempotency:** before placing, check no `positions` row already exists for this `decision_id` (guards against a retried/overlapping cycle double-firing — see §16 for the workflow-level concurrency guard that should make this belt-and-suspenders, not the only defense).

After the order call returns, poll the MCP order-status tool briefly to confirm a fill before writing the `positions` row — do not assume success from the order acceptance alone.

---

## 15. Position Monitor (`execution/position_monitor.py`)

Runs every cycle, after the candidate-evaluation loop, against every row in `positions` where `status = 'open'`:

```python
for position in open_positions():
    quote = mcp.get_option_quote(position.occ_symbol)
    pct_change = (quote.mid_price - position.entry_cost) / position.entry_cost
    days_to_expiry = (position.expiry_date - today).days

    if days_to_expiry <= 1:
        close(position, reason="expiry")
    elif pct_change >= 0.50:
        close(position, reason="target_hit")
    elif pct_change <= -0.50:
        close(position, reason="stop_hit")
    # else: leave open
```
**Defaults:** close at **+50%** of premium (take profit) or **−50%** of premium (stop loss), and force-close with **1 trading day left to expiry** regardless of P&L (avoids pin risk / assignment complexity near expiration). These are implemented as our own polling-based checks, not broker-side bracket orders — Alpaca's native `take_profit`/`stop_loss` order parameters are documented specifically for multi-leg orders, and this build only ever places single-leg orders, so relying on our own monitor is the more certain path rather than an unverified assumption about single-leg bracket support.

`close()` places a sell-to-close market order via the same MCP order tool (`side: "sell"`, same OCC symbol), then updates the `positions` row with `exit_value`, `realized_pnl = (exit_value - entry_cost) * qty * 100`, `closed_at`, `close_reason`, `status='closed'`.

---

## 16. Orchestrator (`orchestrator.py`) — full cycle

```python
def run_cycle():
    if not market_is_open():   # MCP clock check
        return
    cycle_started_at = now()
    open_mcp_session()
    try:
        account = get_account_state()
        recent_outcomes = get_recent_trade_outcomes()

        for symbol in load_watchlist():
            signals = compute_signals(symbol)          # §9
            research = firecrawl.research(symbol)       # §7

            analyst = analyst_agent.run(symbol, signals, research)   # §11
            if analyst.direction == "no_trade":
                db.insert_decision(cycle_started_at, symbol, signals, research,
                                    analyst, None, None, skip_reason="no_trade_thesis")
                continue

            critic = critic_agent.run(analyst)           # §11
            final_conviction = max(0, analyst.confidence_score - critic.conviction_penalty)

            risk_verdict = risk_guard.evaluate(account, final_conviction, recent_outcomes)  # §12

            if risk_verdict.verdict == "reject":
                db.insert_decision(cycle_started_at, symbol, signals, research,
                                    analyst, critic, risk_verdict, skip_reason=risk_verdict.reason)
                continue

            candidates = optimizer.rank_candidates(symbol, analyst.direction,
                                                     analyst.target_price, analyst.timeframe_days)  # §13
            if not candidates:
                db.insert_decision(..., candidates=[], skip_reason="no_liquid_contracts")
                continue

            top = candidates[0]
            qty = size_to_contracts(risk_verdict.position_size_pct, top.premium, account.buying_power)
            if qty < 1:
                db.insert_decision(..., candidates=candidates, skip_reason="size_too_small")
                continue

            decision_id = db.insert_decision(..., candidates=candidates, chosen_contract=top)
            order = executor.place(top.occ_symbol, qty)          # §14
            db.update_decision_order_id(decision_id, order.id)
            db.insert_position(decision_id, symbol, top.occ_symbol, top.option_type,
                                qty, top.premium)

        position_monitor.run()   # §15 — checks every open position, closes as needed

    except Exception as e:
        log_error(cycle_started_at, e)
        # fail-safe: any unhandled exception here means no further orders this cycle —
        # never place a trade on a partially-failed cycle.
    finally:
        close_mcp_session()
```

---

## 17. Scheduling

Single workflow, single cron schedule. Concurrency group prevents overlapping runs; `astral-sh/setup-uv` caching avoids reinstalling the MCP server's environment every run.

`.github/workflows/trading_cycle.yml`:
```yaml
name: trading-cycle
concurrency:
  group: trading-cycle-executor
  cancel-in-progress: false
on:
  schedule:
    - cron: '*/30 13-20 * * 1-5'   # every 30 min, ~US market hours UTC, weekdays — adjust for DST
  workflow_dispatch: {}
jobs:
  run-cycle:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - uses: astral-sh/setup-uv@v3
        with: { enable-cache: true }
      - run: uv pip install --system -r requirements.txt
      - run: python -m src.orchestrator
        env:
          ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          FIRECRAWL_API_KEY: ${{ secrets.FIRECRAWL_API_KEY }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

`.github/workflows/tests.yml` — separate, lightweight, runs `pytest` on every push (no secrets needed, no market interaction):
```yaml
name: tests
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - run: pytest tests/
```

---

## 18. Dashboard (Streamlit)

Two views, both reading directly from Supabase:
1. **Overview:** open positions (with live quote pulled on page load), realized P&L to date, win rate.
2. **Decision log:** every row from `decisions`, most recent first. Expanding a row shows: signals, research summary, the Analyst's full long_case/short_case/why_this_side_won, the Critic's failure_scenario, the risk verdict and reason, the full ranked candidate list with scores (this is where the "priority list of options" transparency lives — visible after the fact, not gating the trade), and the chosen contract with its order/outcome.

---

## 19. Testing Requirements

All automated, `pytest`, no manual/human-gated test mode of any kind:
- `test_risk_guard.py`: breaker active, max positions reached, cooldown-with-low-conviction (reject), cooldown-with-high-conviction (approve), normal approve path, sizing formula boundary values
- `test_occ_symbol.py`: round-trip against known real OCC symbols
- `test_optimizer.py`: hand-built synthetic option chain → verify liquidity filter rejects correctly, delta filter rejects correctly, ranking order matches hand-computed EV
- `test_position_monitor.py`: +50% triggers target_hit, −50% triggers stop_hit, 1-day-to-expiry triggers expiry close, none of the above leaves position open

## 20. Coding Standards

- Type hints everywhere; pydantic models for all agent I/O and DB row shapes
- Every external call (MCP, Gemini, Firecrawl, Supabase) in explicit try/except with structured logging (symbol, decision context, stage)
- No bare `except:`
- No secrets in code — env vars only
- Prompts live only in `agents/prompts.py` as named constants

---

## 21. Build Order

1. Repo scaffold + `watchlist.yaml`
2. Supabase schema (§5) + `db/client.py` helpers, smoke-tested
3. `mcp_client.py` — connect, discover tools, confirm `get_account_info` and option snapshot tool names against live discovery
4. `occ_symbol.py` + its test — verify against real known symbols before anything depends on it
5. `technicals.py` (§9) + manual sanity check against a known symbol's published RSI/MACD
6. `firecrawl_research.py`
7. `agents/schemas.py`, `agents/prompts.py`, `agents/debate.py` — test the Analyst/Critic pair on 2-3 symbols with real data, read the output by hand before trusting it
8. `risk_guard.py` + tests
9. `optimizer.py` + tests
10. `executor.py` — dry run one real paper order end-to-end, confirm it fills and the `positions` row is correct
11. `position_monitor.py` + tests
12. `orchestrator.py` wiring all of the above — one full manual local cycle, verify every table gets correct rows
13. GitHub Actions workflows (§17), verify one scheduled + one manual dispatch run
14. Dashboard (§18)
15. Let it run for the remainder of the judged week; fix bugs, do not re-tune strategy parameters based on short-term noise
16. Submission: repo, paper account ID, demo video walking through one decision-log entry end-to-end
