"""StockResearch - start page.   Run:  streamlit run app/Home.py"""
import streamlit as st
import ui_common as ui

st.set_page_config(page_title="StockResearch", layout="wide", page_icon=":material/candlestick_chart:")
st.title("StockResearch")
st.caption("S&P 500 + NASDAQ-100 (+ S&P 100 tag), Nov 2015 onwards. Prices audited, fundamentals from SEC filings.")

con = ui.tech()

# 1. Market Regime Banner (Placed prominently at the top)
reg = ui.en.regime(con, ui.last_session(), ui.en.load_strategy("dip_buyer"))
ui.regime_banner(reg)

# 2. Database update status
from src import update_status as us  # noqa: E402
_target, _newest, _behind = us.price_status(con)
_last = us.last_update(con)
if not _behind:
    st.success(f"Data is up to date: prices to {_newest:%a %b %d, %Y} (last completed trading day)."
               + (f"  Last update {_last[0]:%b %d %H:%M}: {_last[2]}" if _last else ""))
else:
    st.warning(f"Data is NOT up to date: the last completed trading day is {_target:%a %b %d}, but "
               f"{len(_behind)} index member(s) have older prices (newest stored: {_newest}). "
               "Close the app (Ctrl+C in Terminal) and run `./start.sh` - it updates everything, then reopens the app.")
if _last and _last[2] != "OK":
    st.info(f"Last update ({_last[0]:%b %d %H:%M}) needs review: {_last[3]}  - details in logs/update/")

# 3. Market Universe KPI Metrics (Bordered cards)
c1, c2, c3, c4, c5 = st.columns(5)
with c1.container(border=True):
    st.metric("Latest trading day", str(ui.last_session()))
with c2.container(border=True):
    st.metric("Stocks with prices", len(ui.all_tickers()))
n = dict(ui.run(con, "SELECT index_name, count(DISTINCT ticker) FROM index_membership WHERE end_date IS NULL GROUP BY 1"))
with c3.container(border=True):
    st.metric("NASDAQ-100 members", n.get("NDX", 0))
with c4.container(border=True):
    st.metric("S&P 500 members", n.get("SP500", 0))
with c5.container(border=True):
    st.metric("S&P 100 tag", n.get("SP100", 0) or "not built")

# 4. Interactive Quick-Launch Cards (Clickable navigation)
st.subheader("Trading terminal launchpad")
r1, r2, r3 = st.columns(3)
with r1.container(border=True):
    st.page_link("pages/1_Screener.py", label="Morning dip screener", icon=":material/filter_alt:")
    st.caption("Screen today's candidate stocks trading near the buy price with debt/equity and revenue growth filters.")

with r2.container(border=True):
    st.page_link("pages/2_Trade_check.py", label="Trade simulator", icon=":material/troubleshoot:")
    st.caption("Inspect a single trade: entry price, stop-loss, take-profit target, and highest point reached.")

with r3.container(border=True):
    st.page_link("pages/3_Stock.py", label="Stock deep dive", icon=":material/show_chart:")
    st.caption("Full price history with buy levels, 52-week channels, support/resistance, and SEC filings.")

r4, r5, r6 = st.columns(3)
with r4.container(border=True):
    st.page_link("pages/4_Backtest.py", label="Strategy backtest", icon=":material/monitoring:")
    st.caption("Day-by-day portfolio simulation with fixed capital, stop/target rules, and idle cash in QQQ.")

with r5.container(border=True):
    st.page_link("pages/5_Compare.py", label="Compare strategies", icon=":material/compare_arrows:")
    st.caption("Run multiple strategies side-by-side with identical capital to compare CAGRs and drawdowns.")

with r6.container(border=True):
    st.page_link("pages/6_Fundamentals.py", label="Fundamental analysis", icon=":material/account_balance:")
    st.caption("Audited balance sheets, margins, debt-to-equity ratios, and peer company comparisons.")

st.caption("Rules defined in `config/strategies.toml` • Read-only database connection")

