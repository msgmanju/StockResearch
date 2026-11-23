import datetime as dt
import io
import tempfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import ui_common as ui
from src.strategy import backtest as bt

st.set_page_config(page_title="Backtest", layout="wide")
st.title("Backtest - fixed starting capital")
st.caption("Each day: sell what hit its stop / target / time exit, then buy index members whose low touched the v3 "
           "buy price and that pass the filters (deepest discount first) while there are free slots. "
           "Only data known that day is used.")

IDX = ui.mb.UNIVERSES
strats = ui.strategies()
skey = st.selectbox("Strategy", list(strats), format_func=lambda k: strats[k])
_s = ui.en.load_strategy(skey)
sr_kind = _s.get("kind") == "support_resistance" and _s.get("sell", "resistance") == "resistance"
if _s.get("kind") == "support_resistance":
    st.caption("Support Buyer: buy when the day's low touches the support level (known before the open); strongest "
               "support first. " + ("Sell when the high reaches resistance." if sr_kind else
                                    "Sell at the take-profit %, the stop, or the time exit - set below."))
with st.form("bt"):
    st.subheader("1. Which stocks to pick from")
    c1, c2, c3 = st.columns(3)
    uni = c1.radio("Index (members on each day)", list(IDX), format_func=IDX.get,
                   index=list(IDX).index(_s["universe"]) if _s.get("top_n_mcap") and _s.get("universe") in IDX else 0)
    with c2:
        use_pe = st.checkbox("Limit P/E", value=bool(_s.get("use_pe_limit", True)))
        pe_max = st.number_input("Maximum P/E (at the buy price; loss-makers always out)", 1.0, 500.0, 30.0, 1.0)
        hl = st.checkbox("Volatility filter: 52w high / low <= 3", value=True)
    with c3:
        use_de = st.checkbox("Limit debt / equity", value=True)
        de_max = st.number_input("Maximum debt / equity (negative equity always out)", 0.0, 50.0, float(_s.get("de_max", 1.0)), 0.1)
        keep_missing = st.checkbox("Keep stocks with no SEC figure for a filter", value=False)
        topn = st.checkbox("Only stocks ranked within the top N (discount; Support Buyer: strongest support)", value=False)
    c1, c2, c3 = st.columns(3)
    with c1:
        use_top = st.checkbox("Only the largest companies by market cap", value=bool(_s.get("top_n_mcap")),
                              help="Market cap on each day = close x shares known before that day (SEC filings). "
                                   "Ranked among the index members that day; share classes (GOOG/GOOGL) count as one.")
        top_n = st.number_input("How many largest companies (N)", 1, 500, int(_s.get("top_n_mcap") or 30), 1)
        out_top = st.checkbox("Sell a holding when its company drops out of the top N",
                              value=bool(_s.get("sell_if_out_of_top", False)),
                              help="Off = keep it until its normal exit (take-profit, stop, time exit)")
    with c2:
        use_rg = st.checkbox("Limit revenue growth", value="rev_growth_min" in _s)
        rg_min = st.number_input("Minimum revenue growth %", -100.0, 1000.0, float(_s.get("rev_growth_min", 10.0)), 5.0)
        rg_basis = st.radio("Measured as", ["ttm", "q"], horizontal=True, format_func=lambda x: {
            "ttm": "12 months vs the 12 before", "q": "latest quarter vs a year earlier"}[x])
    st.caption("Fundamentals come from the latest SEC report filed before the buy day.")

    st.subheader("2. Buy and sell rules")
    c1, c2, c3, c4 = st.columns(4)
    if sr_kind:
        upside = c1.number_input("Minimum upside: resistance above support (%)", 0.0, 100.0,
                                 float(ui.en.load_strategy(skey).get("min_upside_pct", 5)), 0.5)
        tp = 20.0
        sr_target = c1.radio("Sell at", ["daily", "fixed", "daily_above_buy"], format_func=lambda x: {
            "daily": "each day's resistance (your rule)", "fixed": "resistance known on the buy day",
            "daily_above_buy": "each day's resistance, only above the buy price"}[x])
    else:
        tp = c1.number_input("Take-profit %", 1.0, 1000.0, float(_s.get("take_profit_pct", 20)), 5.0,
                             help="e.g. 20, 50 or 100 - sold when the high reaches buy price + this %")
        upside = 5.0
        sr_target = "daily"
    stop = c2.number_input("Stop-loss %", 1.0, 99.0, 30.0, 1.0)
    hold = c3.number_input("Time exit after (days)", 1, 3650, 365, 1)
    bear = c4.checkbox("Use the bear-year stops instead (-18% / -30%)", value=False)
    exit_plan, scale = "fixed", {}
    if not sr_kind:
        exit_plan = st.radio("How to sell winners", ["fixed", "scale"], horizontal=True, format_func=lambda x: {
            "fixed": "all at the take-profit", "scale": "scale out in steps + trailing stop (take-profit not used)"}[x])
        with st.expander("Scale-out steps (used only with 'scale out in steps')"):
            e1, e2, e3, e4, e5 = st.columns(5)
            scale = dict(scale1_pct=e1.number_input("Step 1 at +%", 1.0, 500.0, 15.0, 1.0),
                         scale1_sell=e2.number_input("Sell % of shares (stop -> buy price)", 0.0, 100.0, 25.0, 5.0),
                         scale2_pct=e3.number_input("Step 2 at +%", 1.0, 1000.0, 30.0, 1.0),
                         scale2_sell=e4.number_input("Sell % of shares (rest: trailing stop)", 0.0, 100.0, 50.0, 5.0),
                         trail_pct=e5.number_input("Trailing stop %", 1.0, 90.0, 10.0, 1.0))
            st.caption("Step 1: sell part and move the stop up to the buy price. Step 2: sell another part; the "
                       "rest is sold when it falls this % below its highest high since step 2. The time exit only "
                       "applies before step 2. If a day hits a stop and a step, the stop is assumed first.")
        if exit_plan != "scale":
            scale = {}
    rank_by, near = None, None
    if _s.get("kind") == "support_resistance":
        c1, c2 = st.columns(2)
        rank_by = c1.radio("Which first when slots are short", ["near_52w", "touches"],
                           index=0 if _s.get("rank_by", "touches") == "near_52w" else 1, format_func=lambda x: {
                               "near_52w": "support closest to the 52-week low or 52-week average",
                               "touches": "strongest support (most touches)"}[x])
        near = c2.number_input("Only supports within this % of the 52-week low or average (0 = no limit)",
                               0.0, 100.0, float(_s.get("near_pct", 5)), 1.0,
                               help="used with 'closest to the 52-week low or average'")
    st.markdown("**Optional extras** (not part of your rules - off by default)")
    c1, c2 = st.columns(2)
    cooldown = c1.number_input("Don't buy a stock again for N days after it was stopped out (0 = off)", 0, 3650, 0, 30)
    mkt = c2.checkbox("No new buys while QQQ is below its 200-day average", value=False)
    sell_all = c2.checkbox("Sell everything when QQQ closes below its 200-day average (no buys until it is back above)",
                           value=False, help="Sold at the close of the day QQQ closes below its 200-day average. "
                                             "Pair it with 'QQQ, but SGOV while QQQ is below its 200-day average' below.")

    st.subheader("3. Money")
    c1, c2, c3, c4, c5 = st.columns(5)
    start = c1.date_input("Start", dt.date(2017, 1, 1), min_value=dt.date(2016, 11, 1), max_value=ui.last_session())
    end = c2.date_input("End", ui.last_session(), max_value=ui.last_session())
    capital = c3.number_input("Starting capital ($)", 1000.0, 100_000_000.0, 50_000.0, 1000.0)
    maxpos = c4.number_input("Maximum positions (stocks at a time)", 1, 200, int(_s.get("max_positions", 30)), 1)
    cost = c5.number_input("Cost per buy / sell (%)", 0.0, 2.0, 0.05, 0.01)
    c1, c2 = st.columns([3, 1])
    sizing = c1.radio("Position size", ["equity", "fixed"], horizontal=True, format_func=lambda x: {
        "equity": "total portfolio value / positions (grows with gains)",
        "fixed": "starting capital / positions (always the same)"}[x])
    divs = c2.checkbox("Add dividends to cash", value=True)
    IDLE = {"cash": "cash (earns nothing)", "qqq": "QQQ",
            "qqq_sgov": "QQQ, but SGOV (T-bills) while QQQ is below its 200-day average",
            "sgov": "SGOV (T-bills) always"}
    idle_default = ("qqq_sgov" if _s.get("idle_switch") == "sgov_below_200" else "sgov"
                    if _s.get("idle_switch") == "sgov_always" else "qqq" if _s.get("cash_in_qqq") else "cash")
    idle_pick = st.radio("Money that is not in stocks", list(IDLE), index=list(IDLE).index(idle_default),
                         format_func=IDLE.get, horizontal=True,
                         help="Idle money earns that fund's daily return (QQQ with its dividends if 'Add dividends' is "
                              "on; T-bills always with their interest). Buys and sells are settled against it at the "
                              "day's close, and the cost per buy / sell is charged on each move. The 200-day switch "
                              "happens at the close of the day QQQ crosses its 200-day average. SGOV started in May "
                              "2020 - BIL (1-3 month T-bills) stands in before that.")
    go = st.form_submit_button("Run backtest", type="primary")

if not go and "bt_res" not in st.session_state:
    st.info("Choose the settings above and press **Run backtest** (takes about 10-20 seconds).")
    st.stop()
if go and not ui.check_index_built(uni):
    st.stop()
if go:
    s = bt.Settings(start=start, end=end, capital=capital, max_positions=int(maxpos), sizing=sizing,
                    take_profit_pct=tp, stop_pct=stop, stop_rule="bear_years" if bear else "fixed",
                    hold_days=int(hold), universe=uni, hl_filter=hl, only_top_n=topn, cost_pct=cost, dividends=divs,
                    pe_max=pe_max if use_pe else None, de_max=de_max if use_de else None, keep_missing=keep_missing,
                    cooldown_days=int(cooldown), market_filter=mkt, strategy=skey, min_upside_pct=upside, sr_target=sr_target,
                    rank_by=rank_by, near_pct=near, top_n_mcap=int(top_n) if use_top else 0,
                    sell_if_out_of_top=out_top, rev_growth_min=rg_min if use_rg else None, rev_growth_basis=rg_basis,
                    cash_in_qqq=idle_pick in ("qqq", "qqq_sgov"), sell_all_below_200=sell_all, exit_plan=exit_plan,
                    **scale,
                    idle_switch={"qqq_sgov": "sgov_below_200", "sgov": "sgov_always"}.get(idle_pick, "none"))
    bar = st.progress(0.0, "running...")
    st.session_state["bt_res"] = bt.run_backtest(ui.tech(), s, progress=lambda x: bar.progress(x, "running..."),
                                                 fcon=ui.fund())
    bar.empty()
res = st.session_state["bt_res"]
S = res["summary"]
st.divider()
st.subheader("Results")

c = st.columns(5)
with c[0].container(border=True):
    st.metric("Final portfolio", f"${S['final_value']:,.0f}", f"{S['total_return_pct']:+.1f}% total")
with c[1].container(border=True):
    diff_cagr = S['cagr_pct'] - S.get('qqq_cagr_pct', 0)
    st.metric("CAGR", f"{S['cagr_pct']:.1f}%", f"{diff_cagr:+.1f}% vs QQQ")
with c[2].container(border=True):
    st.metric("Max drawdown", f"{S['max_drawdown_pct']:.1f}%", f"QQQ {S['qqq_max_drawdown_pct']:.1f}%", delta_color="off")
with c[3].container(border=True):
    st.metric("Trades closed", S["trades_closed"], f"win rate {S['win_rate_pct']:.0f}%", delta_color="off")
with c[4].container(border=True):
    st.metric("Avg trade return", f"{S['avg_trade_return_pct']:+.1f}%", f"{S['avg_days_held']:.0f} days held", delta_color="off")
sett = res["settings"]
st.caption(f"{strats.get(sett.strategy, sett.strategy)} | Universe {sett.universe} | "
           f"P/E <= {sett.pe_max if sett.pe_max is not None else 'no limit'} | "
           f"debt/equity <= {sett.de_max if sett.de_max is not None else 'no limit'} | "
           + (f"target = resistance ({sett.sr_target}, min upside {sett.min_upside_pct:g}%) | "
              if bt.sells_at_resistance(sett.s) else f"target +{sett.take_profit_pct:g}% | ")
           + f"stop -{sett.stop_pct:g}% | {sett.max_positions} positions")
if S.get("warning"):
    st.warning(S["warning"])
idle_on = sett.cash_in_qqq or sett.idle_switch == "sgov_always"
if sett.top_n_mcap or idle_on or sett.rev_growth_min is not None:
    st.caption(" | ".join(x for x in [
        f"only the {sett.top_n_mcap} largest companies" + (" (sold when they drop out)" if sett.sell_if_out_of_top
                                                          else "") if sett.top_n_mcap else None,
        f"revenue growth ({'12 months' if sett.rev_growth_basis == 'ttm' else 'quarter'}) >= {sett.rev_growth_min:g}%"
        if sett.rev_growth_min is not None else None,
        (f"idle money in {S['idle_money']} earned ${S['gain_on_idle_money']:,.0f} ("
         + ", ".join(f"{k} ${v:,.0f} over {S['idle_days'].get(k, 0)} days" for k, v in S["idle_gain_by_fund"].items())
         + f"; costs ${S['idle_switch_costs']:,.0f}"
         + (f"; {S['idle_switches']} switches" if "idle_switches" in S else "") + ")") if idle_on else None,
        (f"the parking rule alone (no stocks): ${S['idle_rule_only_final_value']:,.0f} "
         f"({S['idle_rule_only_cagr_pct']:.1f}% a year)") if "idle_rule_only_final_value" in S else None,
        (f"QQQ with dividends: ${S['qqq_with_dividends_final_value']:,.0f} ({S['qqq_with_dividends_cagr_pct']:.1f}% "
         "a year)") if idle_on else None] if x))
st.caption(f"{S['start']} to {S['end']} | QQQ buy & hold would be ${S['qqq_final_value']:,.0f} | "
           f"average invested {S['avg_invested_pct']:.0f}% of the portfolio | dividends ${S['dividends_received']:,.0f} | "
           f"exits: {', '.join(f'{k} {v}' for k, v in S.get('exits', {}).items())}")

cv = res["curve"][["date", "equity", "qqq_equity"]].rename(columns={"equity": "Strategy", "qqq_equity": "QQQ buy & hold"})
cv["date"] = pd.to_datetime(cv["date"])
long = cv.melt("date", var_name="series", value_name="value")
st.altair_chart(alt.Chart(long).mark_line().encode(
    x=alt.X("date:T", title=None), y=alt.Y("value:Q", title="portfolio value ($)"),
    color=alt.Color("series:N", scale=alt.Scale(domain=["Strategy", "QQQ buy & hold"], range=["#00C805", "#3B82F6"])),
    tooltip=["date:T", "series:N", alt.Tooltip("value:Q", format=",.0f")]).properties(height=380),
    width="stretch")

t1, t2, t3 = st.tabs(["📅 Yearly returns", "📋 Closed trades log", "💼 Open holdings"])
with t1:
    st.dataframe(res["yearly"], hide_index=True, width="stretch", column_config={
        "year": st.column_config.NumberColumn(format="%d"),
        "portfolio_return_pct": st.column_config.NumberColumn("strategy", format="%+.1f%%"),
        "qqq_return_pct": st.column_config.NumberColumn("QQQ", format="%+.1f%%"),
        "value_at_year_end": st.column_config.NumberColumn("value at year end", format="$%,.0f")})
with t2:
    st.dataframe(res["trades"], hide_index=True, width="stretch", column_config={
        "return_pct": st.column_config.NumberColumn("return", format="%+.1f%%"),
        "price_return_pct": st.column_config.NumberColumn("price return", format="%+.1f%%"),
        "profit": st.column_config.NumberColumn(format="$%,.0f"),
        "cost": st.column_config.NumberColumn(format="$%,.0f")})
with t3:
    st.dataframe(res["open"], hide_index=True, width="stretch")

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "bt.xlsx"
    bt.write_excel(p, res)
    data = p.read_bytes()
st.download_button("Download Excel (settings, results, yearly, every trade, daily value)", data,
                   file_name=f"backtest_{S['start']}_{S['end']}.xlsx")
st.caption("Prices are split-adjusted, so shares are fractional. Buys fill at the buy price (or the open if the "
           "stock opened below it); stops and targets fill at their level (or the open on a gap); sells are checked "
           "from the day after the buy.")
