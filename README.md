# Alpaca Options Alpha Agent

An autonomous trading agent that trades **single-leg, long-only options** (long calls on bullish theses, long puts on bearish theses) on Alpaca paper trading.

## Features
- **Deterministic Technicals**: Custom Wilder's RSI(14) and MACD(12,26,9) computed from live historical daily bars via Alpaca MCP.
- **Firecrawl Web Research**: Real-time ticker news and analyst digest (async, non-blocking).
- **LLM Debate (Google Gemini 2.5 Flash)**: Structured Analyst (long/short case generation) + Critic (pre-mortem penalty) with structured output via `response_schema`.
- **Deterministic Risk Guard**: Daily loss breaker (−8%), max concurrent positions (5), cooldown after 3 consecutive losses, Kelly-lite sizing.
- **Contract Optimizer**: Ranks candidates by expected value, with delta (0.30–0.50) filter, expiry window filter, and strict liquidity gates.
- **Alpaca MCP Integration**: Automated market order execution over stdio transport via `uvx alpaca-mcp-server`.
- **Idempotent Execution**: Checks DB for existing position before placing each order — safe against cycle retries.
- **Automated Position Monitor**: Enforces +50% Take Profit, −50% Stop Loss, and force-close at 1 day to expiration.
- **Supabase Persistence & Streamlit Dashboard**: Full decision audit trail and portfolio overview.

## Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) — used both for fast dependency installation and as the `uvx` tool runner that launches `alpaca-mcp-server` at runtime.

  Install `uv` / `uvx`:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh   # Linux/macOS
  # OR on Windows (PowerShell):
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

## Setup Instructions

1. **Clone and Install Dependencies**:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   # source .venv/bin/activate   # Linux/macOS
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Copy `.env.example` to `.env` and fill in your API keys:
   ```bash
   cp .env.example .env
   # Edit .env with your keys
   ```
   Required keys:
   - `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` — paper trading account
   - `GEMINI_API_KEY` — Google AI Studio
   - `FIRECRAWL_API_KEY` — firecrawl.dev
   - `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` — Supabase project

3. **Initialize Database**:
   Run the DDL in your Supabase project's SQL editor:
   ```
   src/db/schema.sql
   ```

4. **Run Tests** (no API keys needed):
   ```bash
   pytest tests/ -v
   ```

5. **Run One Trading Cycle** (manual / local):
   ```bash
   python -m src.orchestrator
   ```

6. **Run Streamlit Dashboard**:
   ```bash
   streamlit run dashboard/app.py
   ```

## Architecture

```
Each cycle:
  Market clock → Account state → For each symbol:
    Historical bars → RSI/MACD signals
    Firecrawl research (async)
    Analyst LLM (Gemini) → direction/confidence
    Critic LLM (Gemini) → pre-mortem penalty
    Risk guard (deterministic) → approve/reject + size %
    Option chain → rank candidates by EV
    Execute top candidate → idempotency-checked market order
  Position monitor → close positions at ±50% or 1-day-to-expiry
```

## Automated Scheduling

GitHub Actions runs the cycle every 30 minutes during US market hours (weekdays).
See `.github/workflows/trading_cycle.yml`. Add the 6 secrets to your repo settings before enabling.
