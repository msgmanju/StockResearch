import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

import ui_common as ui

st.set_page_config(page_title="Trade check", layout="wide")
st.title("Trade check")
strats = ui.strategies()
with st.sidebar:
    key = st.selectbox("Strategy", list(strats), format_func=lambda k: strats[k])
    s = ui.en.load_strategy(key)
    tickers = ui.all_tickers()
    ticker = st.selectbox("Stock", tickers, index=tickers.index("COST") if "COST" in tickers else 0)
    day = st.date_input("Buy date (bought at the close)", value=dt.date(2022, 6, 16),
                        min_value=dt.date(2015, 11, 2), max_value=ui.last_session())
    tp = st.number_input("Take-profit %", 1.0, 1000.0, float(s["take_profit_pct"]), 5.0)
    own_stop = st.checkbox("Set my own stop (otherwise from the QQQ regime)")
    stop = st.number_input("Stop-loss %", 1.0, 99.0, float(s["stop_normal_pct"]), 1.0, disabled=not own_stop)
    hold = st.number_input("Time exit after (days)", 1, 3650, int(s["hold_days"]), 1)

day = ui.en.session_on_or_before(ui.tech(), day, ticker) or day
try:
    r, held = ui.en.trade_check(ticker, day, key, tp, stop if own_stop else None, hold, tcon=ui.tech())
except ValueError as e:
    st.error(str(e))
    st.stop()

ui.regime_banner(ui.en.regime(ui.tech(), day, s))
c = st.columns(5)
with c[0].container(border=True):
    st.metric("Bought", f"${r['entry_price']:.2f}", str(r["buy_date"]))
with c[1].container(border=True):
    st.metric("Sold" if r["status"] == "closed" else "Latest", f"${r['exit_price']:.2f}", str(r["exit_date"]))
with c[2].container(border=True):
    st.metric("Return", ui.pct(r["return_pct"], 2), f"divs {ui.pct(r['return_with_dividends_pct'], 2)}")
if "max_return_pct" in r:
    with c[3].container(border=True):
        st.metric("Highest point", ui.pct(r["max_return_pct"], 2), str(r["max_return_date"]))
    with c[4].container(border=True):
        st.metric("Lowest point", ui.pct(r["max_drawdown_pct"], 2), str(r["max_drawdown_date"]))

st.info(f"**{r['exit_reason']}** after {r['days_held']} days  •  Stop -{r['stop_pct']:.0f}% = ${r['stop_price']:.2f}  •  "
        f"Target +{r['take_profit_pct']:.0f}% = ${r['take_profit_price']:.2f}  •  "
        f"Time exit on/after {r['time_exit_on_or_after']}", icon=":material/info:")

if len(held):
    d = pd.concat([pd.DataFrame([{"date": r["buy_date"], "close": r["entry_price"], "high": r["entry_price"],
                                  "low": r["entry_price"]}]), held[["date", "close", "high", "low"]]])
    d["date"] = pd.to_datetime(d["date"])
    base = alt.Chart(d).encode(x=alt.X("date:T", title=None))
    band = base.mark_area(opacity=0.15).encode(y=alt.Y("low:Q", title="price", scale=alt.Scale(zero=False)), y2="high:Q")
    line = base.mark_line().encode(y="close:Q", tooltip=["date:T", "close:Q", "high:Q", "low:Q"])
    lv = pd.DataFrame({"level": ["entry", "stop", "target"],
                       "price": [r["entry_price"], r["stop_price"], r["take_profit_price"]]})
    rules = alt.Chart(lv).mark_rule(strokeDash=[4, 4]).encode(
        y="price:Q", color=alt.Color("level:N", scale=alt.Scale(domain=["entry", "stop", "target"],
                                                               range=["#888888", "#d62728", "#2ca02c"])))
    ex = pd.DataFrame({"date": [pd.Timestamp(r["exit_date"])], "price": [r["exit_price"]]})
    dot = alt.Chart(ex).mark_point(size=120, filled=True, color="black").encode(x="date:T", y="price:Q")
    st.altair_chart((band + line + rules + dot).properties(height=380), width="stretch")
    with st.expander("Every day held"):
        st.dataframe(held, hide_index=True, width="stretch")
