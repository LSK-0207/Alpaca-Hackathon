# Alpaca Options Alpha Agent

An autonomous trading agent that trades **single-leg, long-only options** (long calls on bullish theses, long puts on bearish theses) on Alpaca paper trading.

## Features
- **Deterministic Technicals**: Custom Wilder's RSI(14) and MACD(12,26,9) computed from historical daily bars.
- **Firecrawl Web Research**: Real-time ticker news and analyst digest.
- **LLM Debate (Google Gemini 2.5 Flash)**: Structured Analyst (long/short case generation) + Critic (pre-mortem penalty).
- **Deterministic Risk Guard**: Daily loss breaker (-8%), max concurrent positions (5), cooldown after 3 consecutive losses, Kelly-lite sizing.
- **Contract Optimizer**: Ranks candidate options based on expected value, delta (0.30–0.50), and strict liquidity filters.
- **Alpaca MCP Integration**: Automated market order execution over standard stdio transport.
- **Automated Position Monitor**: Enforces +50% Take Profit, -50% Stop Loss, and close at 1 day to expiration.
- **Supabase Persistence & Streamlit Dashboard**: Full decision audit trail and portfolio overview.

## Setup Instructions

1. **Clone and Install Dependencies**:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Copy `.env.example` to `.env` and fill in your API keys:
   ```bash
   cp .env.example .env
   ```

3. **Initialize Database**:
   Run the DDL in `src/db/schema.sql` on your Supabase Postgres instance.

4. **Run Tests**:
   ```bash
   pytest tests/
   ```

5. **Run Streamlit Dashboard**:
   ```bash
   streamlit run dashboard/app.py
   ```
