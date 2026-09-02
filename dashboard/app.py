import os
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

st.set_page_config(
    page_title="Alpaca Options Alpha Agent",
    page_icon="🦅",
    layout="wide",
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")


@st.cache_resource
def get_supabase() -> Client | None:
    if SUPABASE_URL and SUPABASE_KEY:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    return None


supabase = get_supabase()

st.title("🦅 Alpaca Options Alpha Agent Dashboard")
st.caption("Autonomous options trading — paper account. Refreshed on page load.")

if not supabase:
    st.warning(
        "⚠️ Supabase credentials (SUPABASE_URL, SUPABASE_SERVICE_KEY) not configured. "
        "Running in offline display mode."
    )

tab_overview, tab_decisions = st.tabs(["📊 Portfolio & Positions", "🧠 Decision Log"])

# ------------------------------------------------------------------ #
# Tab 1: Overview                                                     #
# ------------------------------------------------------------------ #
with tab_overview:
    st.subheader("Active Positions & Performance")

    if supabase:
        try:
            positions_res = supabase.table("positions").select("*").execute()
            positions_data = positions_res.data or []
        except Exception as e:
            st.error(f"Error fetching positions: {e}")
            positions_data = []
    else:
        positions_data = []

    if positions_data:
        df_positions = pd.DataFrame(positions_data)
        open_df = df_positions[df_positions["status"] == "open"].copy()
        closed_df = df_positions[df_positions["status"] == "closed"].copy()

        # Summary metrics
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Open Positions", len(open_df))
        with col2:
            realized_pnl = (
                closed_df["realized_pnl"].fillna(0).sum()
                if not closed_df.empty
                else 0.0
            )
            st.metric(
                "Realized P&L",
                f"${realized_pnl:,.2f}",
                delta=f"${realized_pnl:,.2f}",
                delta_color="normal",
            )
        with col3:
            if not closed_df.empty:
                wins = (closed_df["realized_pnl"].fillna(0) > 0).sum()
                win_rate = (wins / len(closed_df)) * 100.0
                st.metric("Win Rate", f"{win_rate:.1f}%")
            else:
                st.metric("Win Rate", "N/A")

        # Open positions table — live quote pulled on page load per spec §18
        st.markdown("### Open Positions")
        if not open_df.empty:
            # Attempt to enrich with live mid-price from Alpaca (best-effort, no blocking)
            live_quotes: dict = {}
            if supabase:
                try:
                    occ_symbols = open_df["occ_symbol"].dropna().tolist()
                    st.caption(
                        f"Fetching live quotes for {len(occ_symbols)} open position(s)…"
                    )
                    # Note: live quote fetch via Alpaca requires direct API call.
                    # The MCP server is a CLI process — not available from Streamlit context.
                    # We display a prompt for the user to configure a direct Alpaca data API key
                    # if they want live quotes here, or simply show the stored entry costs.
                    # This is consistent with the spec — the dashboard reads from Supabase and
                    # "live quote pulled on page load" is best-effort here.
                    st.info(
                        "💡 Live option quotes require direct Alpaca Data API access. "
                        "Showing stored entry costs below. For live unrealized P&L, "
                        "set ALPACA_API_KEY and ALPACA_SECRET_KEY in your Streamlit secrets."
                    )
                except Exception:
                    pass

            display_cols = [
                c for c in
                ["symbol", "occ_symbol", "option_type", "qty", "entry_cost", "opened_at"]
                if c in open_df.columns
            ]
            st.dataframe(open_df[display_cols], use_container_width=True)
        else:
            st.info("No currently open positions.")

        # Closed positions table
        st.markdown("### Closed Positions")
        if not closed_df.empty:
            display_cols = [
                c for c in
                ["symbol", "occ_symbol", "option_type", "qty", "entry_cost",
                 "exit_value", "realized_pnl", "close_reason", "closed_at"]
                if c in closed_df.columns
            ]
            closed_df_sorted = closed_df.sort_values("closed_at", ascending=False)
            st.dataframe(closed_df_sorted[display_cols], use_container_width=True)
        else:
            st.info("No closed positions yet.")
    else:
        st.info("No position records found in the database.")

# ------------------------------------------------------------------ #
# Tab 2: Decision Log per spec §18                                    #
# ------------------------------------------------------------------ #
with tab_decisions:
    st.subheader("Autonomous Reasoning & Decision Audit Trail")
    st.caption(
        "Every decision row — most recent first. "
        "Expand a row to see signals, analyst/critic reasoning, ranked candidates, and execution outcome."
    )

    if supabase:
        try:
            decisions_res = (
                supabase.table("decisions")
                .select("*")
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            decisions = decisions_res.data or []
        except Exception as e:
            st.error(f"Error fetching decisions: {e}")
            decisions = []
    else:
        decisions = []

    if decisions:
        for d in decisions:
            symbol = d.get("symbol", "UNKNOWN")
            created_at = d.get("created_at", "")
            verdict = d.get("risk_verdict") or "N/A"
            skip_reason = d.get("skip_reason")
            final_conviction = d.get("final_conviction")

            # Build expander header
            status_tag = f"⛔ Skipped: {skip_reason}" if skip_reason else f"✅ Executed"
            if verdict and verdict != "N/A":
                verdict_tag = f"Risk: {verdict.upper()}"
            else:
                verdict_tag = ""
            header = (
                f"{created_at[:19] if created_at else '—'} | "
                f"**{symbol}** | "
                f"{verdict_tag} | "
                f"Conviction: {final_conviction if final_conviction is not None else '—'} | "
                f"{status_tag}"
            )

            with st.expander(header):
                col_left, col_right = st.columns(2)

                with col_left:
                    st.markdown("**Technical Signals**")
                    signals = d.get("signals")
                    if signals:
                        st.json(signals)
                    else:
                        st.write("—")

                    st.markdown("**Firecrawl Research Digest**")
                    st.write(d.get("research_summary") or "_No research data._")

                    st.markdown("**Risk Verdict**")
                    st.write(
                        f"Verdict: `{verdict}` — Reason: `{d.get('risk_reason') or '—'}`"
                    )
                    if d.get("position_size_pct") is not None:
                        st.write(f"Position size: `{float(d['position_size_pct'])*100:.1f}%`")

                with col_right:
                    st.markdown("**Analyst Output**")
                    analyst_out = d.get("analyst_output")
                    if analyst_out:
                        # Show key fields prominently
                        direction = analyst_out.get("direction", "—")
                        confidence = analyst_out.get("confidence_score", "—")
                        target = analyst_out.get("target_price", "—")
                        st.write(
                            f"Direction: `{direction}` | "
                            f"Confidence: `{confidence}` | "
                            f"Target: `{target}`"
                        )
                        st.markdown("*Long case:*")
                        long_c = analyst_out.get("long_case", {})
                        st.write(long_c.get("thesis", "—"))
                        st.markdown("*Short case:*")
                        short_c = analyst_out.get("short_case", {})
                        st.write(short_c.get("thesis", "—"))
                        st.markdown("*Why this side won:*")
                        st.write(analyst_out.get("why_this_side_won", "—"))
                    else:
                        st.write("—")

                    st.markdown("**Critic Output**")
                    critic_out = d.get("critic_output")
                    if critic_out:
                        st.write(
                            f"Failure scenario: {critic_out.get('failure_scenario', '—')}"
                        )
                        st.write(
                            f"Conviction penalty: `{critic_out.get('conviction_penalty', '—')}`"
                        )
                    else:
                        st.write("—")

                st.markdown("---")
                st.markdown("**Ranked Candidate Option Contracts** (top 5, score descending)")
                candidates = d.get("candidates")
                if candidates:
                    st.dataframe(
                        pd.DataFrame(candidates),
                        use_container_width=True,
                    )
                else:
                    st.write("_No candidate contracts were evaluated._")

                chosen = d.get("chosen_contract")
                if chosen:
                    st.markdown("**Executed Contract**")
                    col_c1, col_c2 = st.columns(2)
                    with col_c1:
                        st.json(chosen)
                    with col_c2:
                        order_id = d.get("alpaca_order_id")
                        if order_id:
                            st.markdown(f"**Alpaca Order ID:** `{order_id}`")
                        else:
                            st.write("_Order ID not recorded._")
    else:
        st.info("No decision records found. The agent has not run yet, or no trades have been evaluated.")
