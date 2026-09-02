-- Database DDL for Alpaca Options Alpha Agent
create extension if not exists "uuid-ossp";

create table if not exists decisions (
    decision_id uuid primary key default uuid_generate_v4(),
    cycle_started_at timestamptz not null,
    symbol text not null,
    signals jsonb not null,              -- {rsi, macd_line, macd_signal, macd_histogram, latest_price}
    research_summary text,               -- short Firecrawl digest fed to the Analyst
    analyst_output jsonb,                -- full structured output
    critic_output jsonb,                 -- {failure_scenario, conviction_penalty}
    final_conviction numeric,            -- analyst.confidence_score - critic.conviction_penalty, floored at 0
    risk_verdict text check (risk_verdict in ('approve','downsize','reject')),
    risk_reason text,
    position_size_pct numeric,
    candidates jsonb,                    -- ranked list of up to 5 contract candidates with scores
    chosen_contract jsonb,               -- the executed candidate, null if none executed
    alpaca_order_id text,
    skip_reason text,                    -- e.g. 'no_trade_thesis', 'no_liquid_contracts', 'size_too_small'
    created_at timestamptz not null default now()
);

create table if not exists positions (
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

create index if not exists idx_decisions_symbol_created on decisions (symbol, created_at desc);
create index if not exists idx_positions_status on positions (status);
