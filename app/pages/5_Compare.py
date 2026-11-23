"""Compare strategies side by side: growth curves, falls from the peak, yearly returns, and single trades on a chart."""
import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

import ui_common as ui
from src.strategy import backtest as bt

st.set_page_config(page_title="Compare strategies", layout="wide")
st.title("Compare strategies")
st.caption("Runs several backtests with the same money, stocks and stop, and shows them on the same charts. "
           "Every strategy uses exactly the same data, known on each day.")

strats = ui.strategies()
IDX = ui.mb.UNIVERSES
RUN_COLS = {"run": False, "name": "", "strategy": "dip_buyer", "take_profit_pct": 20.0, "positions": None,
            "top_n": None, "qqq_cash": "", "sell_all_200": False, "de_max_row": None, "rev_growth_min": None, "rank_by": "", "near_pct": None}
DEFAULT_RUNS = pd.DataFrame([{**RUN_COLS, **r} for r in [
    {"run": True, "name": "FINAL: Big-Cap, 30 largest (S&P 500 + NASDAQ-100), 10 stocks, D/E 0.6, growth 5%, +20%",
     "strategy": "bigcap_dip_buyer", "positions": 10.0, "de_max_row": 0.6, "rev_growth_min": 5.0},
    {"run": True, "name": "Dip Buyer, +20% target", "strategy": "dip_buyer"},
    {"run": False, "name": "Big-Cap, top 30, +20%, cash in QQQ", "strategy": "bigcap_dip_buyer"},
    {"run": False, "name": "Big-Cap, top 30, +100%, cash in QQQ", "strategy": "bigcap_dip_buyer", "take_profit_pct": 100.0},
    {"run": False, "name": "Big-Cap, top 30, 10 positions, +20%", "strategy": "bigcap_dip_buyer", "positions": 10.0},
    {"run": False, "name": "Big-Cap, top 30, +20%, QQQ / SGOV below 200d", "strategy": "bigcap_dip_buyer",
     "qqq_cash": "QQQ / SGOV below 200d"},
    {"run": False, "name": "Big-Cap, 10 stocks, D/E 0.6, growth 5%, sell all below 200d + SGOV",
     "strategy": "bigcap_dip_buyer", "positions": 10.0, "qqq_cash": "QQQ / SGOV below 200d", "sell_all_200": True,
     "rev_growth_min": 5.0},
    {"run": False, "name": "Support Buyer +50%, nearest 52w low/avg first", "strategy": "support_buyer", "take_profit_pct": 50.0,
     "rank_by": "near_52w", "near_pct": 0.0},
]])

with st.form("cmp"):
    st.subheader("1. What to compare")
    saved = st.session_state.get("cmp_runs")
    if saved is not None:                       # rows from before strategy 3: add its columns
        saved = saved.reindex(columns=list(RUN_COLS))
        saved["qqq_cash"] = saved["qqq_cash"].fillna("").replace({"yes": "QQQ", "no": "cash"})
        saved["sell_all_200"] = saved["sell_all_200"].fillna(False).astype(bool)
        saved["strategy"] = saved["strategy"].map(lambda k: ui.en.strategy_key(k) if isinstance(k, str) else k)
    runs = st.data_editor(
        saved if saved is not None else DEFAULT_RUNS, num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            "run": st.column_config.CheckboxColumn("run", width="small"),
            "name": st.column_config.TextColumn("name (shown on the charts)", width="large"),
            "strategy": st.column_config.SelectboxColumn(
                "strategy", options=list(strats), required=True,
                help="; ".join(f"{k} = {v}" for k, v in strats.items())),
            "take_profit_pct": st.column_config.NumberColumn("take-profit %", min_value=1.0, max_value=1000.0,
                                                             help="sold when the high reaches buy price + this %"),
            "positions": st.column_config.NumberColumn(
                "positions", min_value=1, max_value=200, step=1, help="stocks at a time; empty = the value below"),
            "top_n": st.column_config.NumberColumn(
                "Big-Cap: largest N companies", min_value=0, max_value=500, step=1,
                help="only the N largest companies by market cap that day; empty = the strategy's setting "
                     "(Big-Cap Dip Buyer: 30), 0 = off"),
            "qqq_cash": st.column_config.SelectboxColumn(
                "money not in stocks", options=["", "cash", "QQQ", "QQQ / SGOV below 200d", "SGOV"],
                help="empty = the strategy's setting (Big-Cap Dip Buyer: QQQ). 'QQQ / SGOV below 200d' = QQQ, but "
                     "SGOV (T-bills) while QQQ is below its 200-day average"),
            "sell_all_200": st.column_config.CheckboxColumn(
                "sell all below 200d", help="sell every stock when QQQ closes below its 200-day average; "
                                            "no buys until it closes above again"),
            "de_max_row": st.column_config.NumberColumn(
                "max D/E", min_value=0.0, max_value=50.0, help="this row's debt / equity limit; empty = the setting below"),
            "rev_growth_min": st.column_config.NumberColumn(
                "min revenue growth %", min_value=-100.0, max_value=1000.0,
                help="12 months vs the 12 before, from SEC reports filed before the day; empty = no limit"),
            "rank_by": st.column_config.SelectboxColumn(
                "Support Buyer: which first", options=["", "near_52w", "touches"],
                help="near_52w = support closest to the 52-week low or average; touches = strongest support"),
            "near_pct": st.column_config.NumberColumn(
                "Support Buyer: only within % of 52w low/avg", min_value=0.0, max_value=100.0,
                help="0 or empty = no limit (just the order)")})
    st.subheader("2. The same for every run")
    c1, c2, c3, c4 = st.columns(4)
    uni = c1.selectbox("Index", ["(own)"] + list(IDX), index=0,
                       format_func=lambda k: "each strategy's own (Big-Cap: S&P 500 + NASDAQ-100)" if k == "(own)"
                       else IDX[k])
    stop = c2.number_input("Stop-loss %", 1.0, 99.0, 30.0, 1.0)
    hold = c3.number_input("Time exit after (days)", 1, 3650, 365, 1)
    hl = c4.checkbox("Volatility filter: 52w high / low <= 3", value=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        use_pe = st.checkbox("Limit P/E", value=False)
        pe_max = st.number_input("Maximum P/E", 1.0, 500.0, 30.0, 1.0)
    with c2:
        use_de = st.checkbox("Limit debt / equity", value=False)
        de_max = st.number_input("Maximum debt / equity", 0.0, 50.0, 1.0, 0.1)
    start = c3.date_input("Start", dt.date(2017, 1, 1), min_value=dt.date(2016, 11, 1), max_value=ui.last_session())
    end = c4.date_input("End", ui.last_session(), max_value=ui.last_session())
    c1, c2, c3 = st.columns(3)
    capital = c1.number_input("Starting capital ($)", 1000.0, 100_000_000.0, 50_000.0, 1000.0)
    maxpos = c2.number_input("Maximum positions", 1, 200, 30, 1)
    cost = c3.number_input("Cost per buy / sell (%)", 0.0, 2.0, 0.05, 0.01)
    go = st.form_submit_button("Run comparison", type="primary")

if go:
    st.session_state["cmp_runs"] = runs[[c for c in RUN_COLS if c in runs.columns]]
    todo = runs[runs["run"].fillna(False) & runs["strategy"].notna()].reset_index(drop=True)
    if todo.empty:
        st.warning("Tick at least one row.")
        st.stop()
    if not ui.check_index_built("BOTH" if uni == "(own)" else uni):
        st.stop()
    results = {}
    bar = st.progress(0.0)
    for i, r in todo.iterrows():
        name = (r["name"] or f"run {i + 1}").strip()
        bar.progress(i / len(todo), f"running {name} ({i + 1} of {len(todo)}) ...")
        pos_n = int(r["positions"]) if pd.notna(r.get("positions")) else int(maxpos)
        pick = r.get("qqq_cash") or ""
        qc = {"cash": False, "QQQ": True, "QQQ / SGOV below 200d": True, "SGOV": False}.get(pick)
        sw = {"cash": "none", "QQQ": "none", "QQQ / SGOV below 200d": "sgov_below_200",
              "SGOV": "sgov_always"}.get(pick)
        s = bt.Settings(start=start, end=end, capital=capital, max_positions=pos_n, cost_pct=cost,
                        top_n_mcap=int(r["top_n"]) if pd.notna(r.get("top_n")) else None, cash_in_qqq=qc,
                        idle_switch=sw, sell_all_below_200=bool(r.get("sell_all_200")) if pd.notna(r.get("sell_all_200")) else False,
                        rev_growth_min=float(r["rev_growth_min"]) if pd.notna(r.get("rev_growth_min")) else None,
                        take_profit_pct=float(r["take_profit_pct"] or 20), stop_pct=stop, hold_days=int(hold),
                        universe=None if uni == "(own)" else uni, hl_filter=hl, pe_max=pe_max if use_pe else None,
                        de_max=float(r["de_max_row"]) if pd.notna(r.get("de_max_row")) else (de_max if use_de else None),
                        strategy=r["strategy"],
                        rank_by=r.get("rank_by") or None,
                        near_pct=float(r["near_pct"]) if pd.notna(r.get("near_pct")) else None)
        results[name] = bt.run_backtest(ui.tech(), s, fcon=ui.fund())
    bar.empty()
    st.session_state["cmp_res"] = results

results = st.session_state.get("cmp_res")
if not results:
    st.info("Edit the rows above if you like (add a row to try another setting) and press **Run comparison**. "
            "Each run takes about 10-20 seconds.")
    st.stop()

# ------------------------------------------------------------------ summary
st.divider()
first = next(iter(results.values()))
rows = []
for name, res in results.items():
    S = res["summary"]
    rows.append({"name": name, "final value": S["final_value"], "yearly growth %": S["cagr_pct"],
                 "worst fall %": S["max_drawdown_pct"], "trades": S["trades_closed"], "win rate %": S["win_rate_pct"],
                 "avg trade %": S["avg_trade_return_pct"], "avg days held": S["avg_days_held"],
                 "avg invested %": S["avg_invested_pct"]})
S0 = first["summary"]
rows.append({"name": "QQQ buy & hold", "final value": S0["qqq_final_value"], "yearly growth %": S0["qqq_cagr_pct"],
             "worst fall %": S0["qqq_max_drawdown_pct"]})
rule = next((r["summary"] for r in results.values() if "idle_rule_only_final_value" in r["summary"]), None)
if rule:
    rows.append({"name": "QQQ / SGOV 200-day rule alone (no stocks)", "final value": rule["idle_rule_only_final_value"],
                 "yearly growth %": rule["idle_rule_only_cagr_pct"]})
with_div = next((r["summary"] for r in results.values() if "qqq_with_dividends_final_value" in r["summary"]), None)
if with_div:
    rows.append({"name": "QQQ buy & hold, with dividends", "final value": with_div["qqq_with_dividends_final_value"],
                 "yearly growth %": with_div["qqq_with_dividends_cagr_pct"]})
summary = pd.DataFrame(rows)
st.subheader(f"Results {S0['start']} to {S0['end']} - ${S0['starting_capital']:,.0f} start")
st.dataframe(summary, hide_index=True, width="stretch", column_config={
    "final value": st.column_config.NumberColumn(format="$%,.0f"),
    "yearly growth %": st.column_config.NumberColumn(format="%.1f%%"),
    "worst fall %": st.column_config.NumberColumn(format="%.1f%%"),
    "win rate %": st.column_config.NumberColumn(format="%.0f%%"),
    "avg trade %": st.column_config.NumberColumn(format="%+.2f%%"),
    "avg days held": st.column_config.NumberColumn(format="%.0f"),
    "avg invested %": st.column_config.NumberColumn(format="%.0f%%")})

# ------------------------------------------------------------------ growth and falls
curves = []
for name, res in results.items():
    c = res["curve"][["date", "equity"]].assign(series=name)
    curves.append(c)
q = first["curve"][["date", "qqq_equity"]].rename(columns={"qqq_equity": "equity"}).assign(series="QQQ buy & hold")
cv = pd.concat(curves + [q], ignore_index=True)
cv["date"] = pd.to_datetime(cv["date"])
cv["fall from peak %"] = cv.groupby("series")["equity"].transform(lambda x: (x / x.cummax() - 1) * 100)
names = list(results) + ["QQQ buy & hold"]
palette = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#e377c2", "#17becf"]
colors = alt.Scale(domain=names, range=palette[:len(names) - 1] + ["#aaaaaa"])
log = st.toggle("Log scale (equal % moves look equal)", value=False)
sel = alt.selection_point(fields=["series"], bind="legend")
growth = alt.Chart(cv).mark_line().encode(
    x=alt.X("date:T", title=None),
    y=alt.Y("equity:Q", title="portfolio value ($)", scale=alt.Scale(type="log" if log else "linear", zero=False)),
    color=alt.Color("series:N", scale=colors, legend=alt.Legend(orient="top", title=None)),
    opacity=alt.condition(sel, alt.value(1), alt.value(0.15)),
    tooltip=["date:T", "series:N", alt.Tooltip("equity:Q", format="$,.0f")]).add_params(sel).properties(height=380)
st.markdown("**Portfolio value** - click a name in the legend to highlight it")
st.altair_chart(growth, width="stretch")
falls = alt.Chart(cv).mark_line().encode(
    x=alt.X("date:T", title=None), y=alt.Y("fall from peak %:Q", title="% below its peak"),
    color=alt.Color("series:N", scale=colors, legend=None),
    opacity=alt.condition(sel, alt.value(1), alt.value(0.15)),
    tooltip=["date:T", "series:N", alt.Tooltip("fall from peak %:Q", format=".1f")]).add_params(sel).properties(height=220)
st.markdown("**Falls from the peak** (how deep each one was under water)")
st.altair_chart(falls, width="stretch")

# ------------------------------------------------------------------ yearly returns
yr = []
for name, res in results.items():
    y = res["yearly"][["year", "portfolio_return_pct"]].rename(columns={"portfolio_return_pct": "return %"})
    yr.append(y.assign(series=name))
yr.append(first["yearly"][["year", "qqq_return_pct"]].rename(columns={"qqq_return_pct": "return %"})
          .assign(series="QQQ buy & hold"))
yr = pd.concat(yr, ignore_index=True)
st.markdown("**Return by year**")
st.altair_chart(alt.Chart(yr).mark_bar().encode(
    x=alt.X("year:O", title=None), xOffset="series:N", y=alt.Y("return %:Q"),
    color=alt.Color("series:N", scale=colors, legend=alt.Legend(orient="top", title=None)),
    tooltip=["year:O", "series:N", alt.Tooltip("return %:Q", format="+.1f")]).properties(height=300), width="stretch")
st.dataframe(yr.pivot(index="year", columns="series", values="return %")[names].round(1), width="stretch")

# ------------------------------------------------------------------ how trades ended
ex = []
for name, res in results.items():
    t = res["trades"]
    if len(t):
        why = t["reason"].str.replace(r" \(gap\)| \(same day as (stop|target)\)", "", regex=True)
        why = why.where(~(why.str.startswith("resistance") & (t["sell_price"] < t["buy_price"])),
                        "resistance, below buy price")
        why = why.str.replace(" (below buy price)", "", regex=False)
        ex.append(why.value_counts().rename_axis("exit").reset_index(name="trades").assign(series=name))
if ex:
    ex = pd.concat(ex, ignore_index=True)
    st.markdown("**How the trades ended**")
    st.altair_chart(alt.Chart(ex).mark_bar().encode(
        y=alt.Y("series:N", title=None, sort=names), x=alt.X("trades:Q", stack="normalize", title="share of trades"),
        color=alt.Color("exit:N", legend=alt.Legend(orient="top", title=None)),
        tooltip=["series:N", "exit:N", "trades:Q"]).properties(height=40 * len(results) + 40), width="stretch")

# ------------------------------------------------------------------ one trade on a chart
st.divider()
st.subheader("Look at single trades")
c1, c2, c3 = st.columns([2, 2, 3])
pick = c1.selectbox("Run", list(results))
tr = results[pick]["trades"]
if tr.empty:
    st.info("No closed trades in this run.")
    st.stop()
tickers = sorted(tr["ticker"].unique())
tk = c2.selectbox("Stock", tickers, index=tickers.index("NVDA") if "NVDA" in tickers else 0)
tt = tr[tr["ticker"] == tk].sort_values("buy_date").reset_index(drop=True)
labels = [f"{r.buy_date} -> {r.sell_date}  {r.return_pct:+.1f}%  ({r.reason})" for r in tt.itertuples()]
k = c3.selectbox(f"{len(tt)} trade(s) in {tk}", range(len(tt)), format_func=lambda i: labels[i])
t = tt.iloc[k]
a = t["buy_date"] - dt.timedelta(days=45)
b = t["sell_date"] + dt.timedelta(days=45)
px = ui.tech().execute("""SELECT p.date, p.close, p.high, p.low, sr.support, sr.resistance
                          FROM prices_daily p LEFT JOIN sr_daily sr USING (ticker, date)
                          WHERE p.ticker = ? AND p.date BETWEEN ? AND ? ORDER BY p.date""", [tk, a, b]).df()
px["date"] = pd.to_datetime(px["date"])
base = alt.Chart(px).encode(x=alt.X("date:T", title=None))
band = base.mark_area(opacity=0.15, color="#1f77b4").encode(y=alt.Y("low:Q", scale=alt.Scale(zero=False), title="price"),
                                                             y2="high:Q")
lines = px.melt("date", ["close", "support", "resistance"], var_name="line", value_name="price").dropna()
ln = alt.Chart(lines).mark_line().encode(
    x="date:T", y=alt.Y("price:Q", scale=alt.Scale(zero=False)),
    color=alt.Color("line:N", scale=alt.Scale(domain=["close", "support", "resistance"],
                                              range=["#1f77b4", "#2ca02c", "#ff7f0e"]), legend=alt.Legend(orient="top")),
    strokeDash=alt.condition("datum.line == 'close'", alt.value([1, 0]), alt.value([4, 3])),
    tooltip=["date:T", "line:N", alt.Tooltip("price:Q", format=".2f")])
pts = pd.DataFrame([{"date": pd.Timestamp(t["buy_date"]), "price": t["buy_price"], "what": "buy"},
                    {"date": pd.Timestamp(t["sell_date"]), "price": t["sell_price"], "what": "sell"}])
mk = alt.Chart(pts).mark_point(size=160, filled=True).encode(
    x="date:T", y="price:Q",
    color=alt.Color("what:N", scale=alt.Scale(domain=["buy", "sell"], range=["#2ca02c", "#d62728"]), legend=None),
    shape=alt.Shape("what:N", scale=alt.Scale(domain=["buy", "sell"], range=["triangle-up", "triangle-down"]),
                    legend=None),
    tooltip=["what:N", "date:T", alt.Tooltip("price:Q", format=".2f")])
st.altair_chart((band + ln + mk).properties(height=380), width="stretch")
st.caption(f"Bought {t['buy_date']} at {t['buy_price']:.2f}, sold {t['sell_date']} at {t['sell_price']:.2f} "
           f"({t['reason']}); {t['days_held']} days; return {t['return_pct']:+.1f}%. Shaded = each day's high-low range; "
           "support and resistance are the levels known before each day's open.")
