import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

import ui_common as ui

st.set_page_config(page_title="Stock", layout="wide")
st.title("Stock")
with st.sidebar:
    tickers = ui.all_tickers()
    ticker = st.selectbox("Stock", tickers, index=tickers.index("COST") if "COST" in tickers else 0)
    end = st.date_input("To", value=ui.last_session(), max_value=ui.last_session())
    start = st.date_input("From", value=end - dt.timedelta(days=730))
    s = ui.en.load_strategy("dip_buyer")

px = ui.tech().execute("""
    SELECT p.date, p.close, p.high, p.low, t.low_52w, t.high_52w, t.avg_close_52w, sr.support, sr.resistance
    FROM prices_daily p LEFT JOIN technicals_daily t USING (ticker, date)
    LEFT JOIN sr_daily sr USING (ticker, date)
    WHERE p.ticker = ? AND p.date BETWEEN ? AND ? ORDER BY p.date""", [ticker, start, end]).df()
if px.empty:
    st.warning("No prices in that range.")
    st.stop()
px["buy price"] = s["buy_low_weight"] * px["low_52w"] + s["buy_avg_weight"] * px["avg_close_52w"]
px["date"] = pd.to_datetime(px["date"])
mem = ui.tech().execute("SELECT index_name, company, start_date, end_date FROM index_membership WHERE ticker = ?",
                        [ticker]).df()
st.caption(" | ".join(f"{m.index_name}: {m.start_date} to {m.end_date or 'today'} ({m.company})"
                      for m in mem.itertuples()) or "not an index member")

last = px.iloc[-1]
c = st.columns(5)
with c[0].container(border=True):
    st.metric("Latest close", f"${last['close']:.2f}", str(last["date"].date()))
with c[1].container(border=True):
    st.metric("Buy price (v3)", f"${last['buy price']:.2f}" if pd.notna(last["buy price"]) else "-",
              ui.pct((last["close"] / last["buy price"] - 1) * 100) + " from buy" if pd.notna(last["buy price"]) else None)
with c[2].container(border=True):
    st.metric("52w low / high", f"${last['low_52w']:.2f} / ${last['high_52w']:.2f}" if pd.notna(last["low_52w"]) else "-")
with c[3].container(border=True):
    st.metric("Support", f"${last['support']:.2f}" if pd.notna(last["support"]) else "-")
with c[4].container(border=True):
    st.metric("Resistance", f"${last['resistance']:.2f}" if pd.notna(last["resistance"]) else "-")

long = px.melt("date", ["close", "buy price", "low_52w", "avg_close_52w", "support", "resistance"],
               var_name="line", value_name="price").dropna()
chart = alt.Chart(long).mark_line().encode(
    x=alt.X("date:T", title=None), y=alt.Y("price:Q", scale=alt.Scale(zero=False)),
    color=alt.Color("line:N", scale=alt.Scale(
        domain=["close", "buy price", "low_52w", "avg_close_52w", "support", "resistance"],
        range=["#1f77b4", "#17becf", "#d62728", "#7f7f7f", "#2ca02c", "#ff7f0e"])),
    strokeDash=alt.condition("datum.line == 'close'", alt.value([1, 0]), alt.value([4, 3])),
    tooltip=["date:T", "line:N", alt.Tooltip("price:Q", format=".2f")])
st.altair_chart(chart.properties(height=420), width="stretch")

st.subheader("Fundamentals - one row per SEC report (used from the day after it was filed)")
f = ui.fund().execute("""SELECT filed, period_end, revenue_ttm / 1e9 AS revenue_ttm_bn, eps_diluted_ttm,
       revenue_growth_q_yoy * 100 AS rev_growth_qtr_pct, op_margin_ttm * 100 AS op_margin_pct, debt_to_equity,
       revenue_ttm_how, form FROM fund_snapshots WHERE ticker = ? ORDER BY filed DESC""", [ticker]).df()
if f.empty:
    st.info("No SEC fundamentals for this stock (foreign filer without quarterly reports, or no SEC data).")
else:
    st.dataframe(f, hide_index=True, width="stretch", column_config={
        "revenue_ttm_bn": st.column_config.NumberColumn("revenue TTM (bn)", format="%.2f"),
        "eps_diluted_ttm": st.column_config.NumberColumn("EPS TTM", format="%.2f"),
        "rev_growth_qtr_pct": st.column_config.NumberColumn("rev growth qtr", format="%.1f%%"),
        "op_margin_pct": st.column_config.NumberColumn("op margin", format="%.1f%%"),
        "debt_to_equity": st.column_config.NumberColumn("D/E", format="%.2f")})
    st.caption("EPS is per share as reported at the time; after a stock split, divide by the split ratio to "
               "compare with today's split-adjusted prices (the screener's P/E already does this).")
