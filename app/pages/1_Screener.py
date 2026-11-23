import datetime as dt
import io

import numpy as np
import pandas as pd
import streamlit as st

import ui_common as ui
from src.strategy.report import write_range, write_screen

st.set_page_config(page_title="Screener", layout="wide")
st.title("Screener")

strats = ui.strategies()
IDX = {"(strategy default)": "Strategy default", **ui.mb.UNIVERSES}
today_live = ui.live_day()          # a trading day that is not in the database yet (market open or just closed)
live = False

# 1-Click Strategy Presets
PRESETS = [
    "Default: Top 30 Big-Cap Dips",
    "All S&P 500 Dips",
    "Support Touches",
    "Custom Setup"
]
preset = st.segmented_control("Strategy preset", PRESETS, default="Default: Top 30 Big-Cap Dips")

# Default values based on selected preset
if preset == "Default: Top 30 Big-Cap Dips":
    default_strat_idx = list(strats).index("bigcap_dip_buyer") if "bigcap_dip_buyer" in strats else 0
    default_uni_idx = list(IDX).index("(strategy default)") if "(strategy default)" in IDX else 0
    default_de = True
    default_de_val = 0.6
    default_rg = True
    default_rg_val = 5.0
elif preset == "All S&P 500 Dips":
    default_strat_idx = list(strats).index("dip_buyer") if "dip_buyer" in strats else 0
    default_uni_idx = list(IDX).index("SP500") if "SP500" in IDX else 0
    default_de = False
    default_de_val = 1.0
    default_rg = False
    default_rg_val = 10.0
elif preset == "Support Touches":
    default_strat_idx = list(strats).index("support_buyer") if "support_buyer" in strats else 0
    default_uni_idx = list(IDX).index("(strategy default)") if "(strategy default)" in IDX else 0
    default_de = False
    default_de_val = 1.0
    default_rg = False
    default_rg_val = 10.0
else:
    default_strat_idx = 0
    default_uni_idx = 0
    default_de = False
    default_de_val = 1.0
    default_rg = False
    default_rg_val = 15.0

if today_live:
    l1, l2 = st.columns([3, 1])
    live = l1.toggle(f"Live: today ({today_live:%a %b %d}) with prices right now", value=True,
                     help="Today's prices from Yahoo (about real time, can lag a few minutes). The buy price uses "
                          "the 252 stored sessions before today, exactly as in the backtest. Nothing is saved.")
    if l2.button("Refresh prices", disabled=not live):
        ui.live_quotes.clear()

c1, c2, c3, c4 = st.columns(4)
key = c1.selectbox("Strategy", list(strats), index=default_strat_idx, format_func=lambda k: strats[k])
RANGE_MAX_DAYS = 731
with c2:
    use_range = st.checkbox("Date range (optional)", value=False,
                            help="Screen every trading day between two dates and list the stocks that came within "
                                 "range of the buy price on any of them (up to 2 years; about 1 minute per year)")
    if use_range:
        r1, r2 = st.columns(2)
        d_from = r1.date_input("From", value=max(ui.first_session(), ui.last_session() - dt.timedelta(days=30)),
                               min_value=ui.first_session(), max_value=ui.last_session())
        d_to = r2.date_input("To", value=ui.last_session(), min_value=ui.first_session(),
                             max_value=ui.last_session())
        show_every = st.radio("Show", [False, True], horizontal=True,
                              format_func=lambda x: "every day it qualified" if x else "first day per stock")
        day = d_to
        if live:
            st.caption("Live is off while a date range is used.")
            live = False
    else:
        day = st.date_input("Date", value=ui.last_session(), min_value=ui.first_session(),
                            max_value=ui.last_session(), disabled=live, help="Off while 'Live' is on")
tol = c3.slider("Tolerance around the buy price (+/- %)", 0.5, 20.0, 5.0, 0.5)
uni = c4.selectbox("Index (members that day)", list(IDX), index=default_uni_idx, format_func=IDX.get)
if not uni.startswith("(") and not ui.check_index_built(uni):
    st.stop()

with st.expander("Filter constraints & options (Debt/Equity, Revenue Growth, PEG)", expanded=(preset == "Custom Setup")):
    f1, f2, f3, f4 = st.columns(4)
    with f1:
        use_de = st.checkbox("Limit debt / equity", value=default_de)
        de_max = st.number_input("Maximum debt / equity (negative equity out)", 0.0, 50.0,
                                 default_de_val, 0.1, disabled=not use_de)
        use_peg = st.checkbox("Limit PEG", value=False)
        peg_max = st.number_input("Maximum PEG (losses and falling EPS out)", 0.1, 10.0, 1.0, 0.1, disabled=not use_peg,
                                  help="1.0 = P/E no higher than the yearly EPS growth in %")
        peg_yrs = st.radio("PEG: earnings growth over", [1, 3], horizontal=True,
                           format_func=lambda y: "1 year" if y == 1 else "3 years (per year)")
    with f2:
        use_rg = st.checkbox("Limit revenue growth", value=default_rg)
        rg_min = st.number_input("Minimum revenue growth %", -100.0, 1000.0,
                                 default_rg_val, 5.0, disabled=not use_rg)
        RG_BASIS = {"revenue_growth_q_yoy": "latest quarter vs a year earlier",
                    "revenue_growth_ttm_yoy": "last 12 months vs 12 months before"}
        rg_col = st.radio("Revenue growth basis", list(RG_BASIS), format_func=RG_BASIS.get, disabled=not use_rg)
    with f3:
        only_top = st.checkbox("Only stocks passing volatility filter (52w H/L <= 3)", value=False)
        keep_missing = st.checkbox("Keep stocks with no SEC figure", value=False)
    with f4:
        check = st.checkbox("Also check each trade (bought at close)", value=False, disabled=live)
        tp = st.number_input("Take-profit % for check", 1.0, 1000.0,
                             float(ui.en.load_strategy(key).get("take_profit_pct", 20)), 5.0, disabled=not check)

SORTS = {"market_cap": "Market cap (largest first)", "pct_from_buy": "% from buy price (lowest first)",
         "rev_growth": "Revenue growth (highest first)", "peg": "PEG (lowest first)", "peak": "Peak in next year vs buy (highest first)",
         "discount_rank": "Strategy rank"}
sort_by = st.selectbox("Sort results by", list(SORTS), format_func=SORTS.get)
st.divider()

# ------------------------------------------------------------------ market at a glance
from src import market_overview as mo  # noqa: E402

with st.expander("Market at a glance - QQQ vs its 200-day average and the major indexes", expanded=False):
    mq = None
    if live:
        mq = {t: x for t, x in ui.live_quotes(tuple(mo.INDEX_ETFS)).items() if x.get("day") == today_live}
    ov = mo.overview(ui.tech(), today_live if live else day, mq)
    qrow = ov[ov["ticker"] == "QQQ"].iloc[0].to_dict() if (ov["ticker"] == "QQQ").any() else None
    level, text = mo.qqq_status(qrow)
    {"ok": st.success, "warn": st.warning, "bad": st.error}[level](text)
    if qrow and pd.notna(qrow.get("sma200", np.nan)):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("QQQ", f"{qrow['price']:.2f}", f"{qrow['1 day']:+.2f}% today" if pd.notna(qrow.get("1 day")) else None)
        m2.metric("200-day average", f"{qrow['sma200']:.2f}", f"{qrow['vs_200d']:+.1f}% vs QQQ ({qrow['sma200_trend']})",
                  delta_color="normal")
        m3.metric("50-day average", f"{qrow['sma50']:.2f}", f"{qrow['vs_50d']:+.1f}% vs QQQ")
        m4.metric("+/-2.5% band", f"{qrow['sma200'] * 0.975:.2f} - {qrow['sma200'] * 1.025:.2f}",
                  help="below the lower edge = 'defensive' in the 200-day band rule; above the upper edge = back to normal")
    cols = ["index", "ticker", "price", "1 day", "1 week", "1 month", "3 months", "YTD", "1 year", "vs_200d",
            "sma200_trend", "vs_50d", "as_of", "note"]
    pc = st.column_config
    st.dataframe(ov.reindex(columns=cols), hide_index=True, width="stretch", column_config={
        "price": pc.NumberColumn(format="%.2f"),
        **{c: pc.NumberColumn(c, format="%+.1f%%") for c in ["1 day", "1 week", "1 month", "3 months", "YTD", "1 year"]},
        "vs_200d": pc.NumberColumn("vs 200-day avg", format="%+.1f%%"),
        "sma200_trend": "200-day avg is",
        "vs_50d": pc.NumberColumn("vs 50-day avg", format="%+.1f%%"),
        "as_of": "as of", "note": "note"})
    st.caption("ETFs that track the indexes (VOO = S&P 500, DIA = Dow, IWM = Russell 2000); price changes only, no "
               "dividends. 200-day average = average of the last 200 stored closes"
               + (" - compared with the live price." if mq else f" up to {day}."))

universe = None if uni.startswith("(") else uni
if live:
    from src.strategy import live as lv
    u = (universe or ui.en.load_strategy(key)["universe"]).upper()
    members = ui.mb.members_on(ui.tech(), u, ui.last_session())
    q = ui.live_quotes(tuple(sorted(set(members) | {"QQQ"})))
    lev = ui.next_levels(tuple(sorted(members)), ui.last_session()) \
        if ui.en.load_strategy(key).get("kind") == "support_resistance" else None
    info, df = lv.screen(key, tol, universe, ui.tech(), ui.fund(), quotes_=q, day=today_live, levels=lev)
    qt = info.get("quote_time")
    if qt is None or pd.isna(qt):
        st.warning("Yahoo has no prices for today yet (before the open, or Yahoo not reachable). "
                   "Turn 'Live' off to see the last stored day.")
        st.stop()
    st.info(f"LIVE - prices as of {pd.Timestamp(qt):%H:%M} New York time (Yahoo); buy prices from the 252 "
            f"sessions to {info['values_through']}. 'close' below = last price; 'low touched buy' = today's low so "
            "far. Not saved - the database gets today's final prices from the daily update after 6 pm.")
    check = False
elif use_range:
    if d_from > d_to:
        st.error("'From' is after 'To'.")
        st.stop()
    if (d_to - d_from).days > RANGE_MAX_DAYS:
        st.error("Please pick a range of 2 years or less.")
        st.stop()
    days = [r[0] for r in ui.en.run(ui.tech(), """SELECT DISTINCT date FROM prices_daily WHERE ticker = 'QQQ'
                                                AND date BETWEEN ? AND ? ORDER BY 1""", [d_from, d_to])]
    if not days:
        st.warning("No trading days in that range.")
        st.stop()
    ck = (key, tol, universe, d_from, d_to, ui.last_session())
    cache = st.session_state.setdefault("screen_range_cache", {})
    if ck not in cache:
        bar = st.progress(0.0, text=f"Screening {len(days)} trading days ...")
        parts, inf = [], None
        for i, d in enumerate(days):
            inf, x = ui.en.screen(d, key, tol, universe, ui.tech(), ui.fund())
            if len(x):
                x = x[x["in_range"]].copy()
                x.insert(0, "date", d)
                reg = inf.get("regime") or {}
                x["regime"] = f"{'bear' if reg.get('bear') else 'normal'} (stop -{reg.get('stop_pct')}%)" if reg else None
                parts.append(x)
            bar.progress((i + 1) / len(days), text=f"Screening {d} ({i + 1}/{len(days)})")
        bar.empty()
        cache.clear()                                          # keep only the latest range in memory
        cache[ck] = (inf, pd.concat(parts, ignore_index=True) if parts else pd.DataFrame())
    info, df = cache[ck]
    info = {**info, "asked_date": days[0], "date": days[-1], "range_days": len(days)}
else:
    info, df = ui.en.screen(day, key, tol, universe, ui.tech(), ui.fund())
if use_range:
    st.caption(f"{info['strategy']} | {info['universe']} members on each day | {info['range_days']} trading days "
               f"from {info['asked_date']} to {info['date']} | stop in the 'regime' column (QQQ regime of each day)")
else:
    if info["date"] != info["asked_date"]:
        st.info(f"{info['asked_date']} was not a trading day - using {info['date']}.")
    ui.regime_banner(info["regime"])
    st.caption(f"{info['strategy']} | {info['universe']}: {info['members']} members on {info['date']}"
               + (f" | no price that day: {', '.join(info['no_price_that_day'])}" if info["no_price_that_day"] else ""))

if df.empty:
    st.warning("No stock came within range of the buy price in that date range." if use_range
               else "No members found for that day.")
    st.stop()
inr = df[df["in_range"]]
if only_top:
    inr = inr[inr["passes_hl_filter"]]
if use_de:
    inr = inr[((inr["equity"] > 0) & (inr["debt_to_equity"] <= de_max))
              | (keep_missing & inr["debt_to_equity"].isna())]
if use_peg:
    pc = f"peg_{peg_yrs}y"
    inr = inr[(inr[pc].notna() & (inr[pc] <= peg_max)) | (keep_missing & inr["eps_diluted_ttm"].isna())]
if use_rg:
    inr = inr[(inr[rg_col].notna() & (inr[rg_col] * 100 >= rg_min)) | (keep_missing & inr[rg_col].isna())]


def sort_rows(d):
    col, asc = {"market_cap": ("market_cap", False), "pct_from_buy": ("pct_from_buy", True),
                "rev_growth": (rg_col, False), "peg": (f"peg_{peg_yrs}y", True), "peak": ("peak_after_pct", False),
                "discount_rank": ("discount_rank", True)}[sort_by]
    return d.sort_values([col, "ticker"], ascending=[asc, True], na_position="last", kind="mergesort")


if use_range:
    inr = inr.sort_values(["date", "ticker"], kind="mergesort")
    inr["days_hit"] = inr.groupby("ticker")["date"].transform("count")
    if not show_every:
        inr = inr.drop_duplicates("ticker", keep="first")
inr = sort_rows(inr)
df = sort_rows(df)
filt = [f"PEG ({peg_yrs}y) <= {peg_max:g}" if use_peg else None, f"debt/equity <= {de_max:g}" if use_de else None,
        f"revenue growth >= {rg_min:g}%" if use_rg else None, "volatility filter" if only_top else None]
if use_range:
    head = (f"{inr['ticker'].nunique()} stock(s)" + (f", {len(inr)} stock-days" if show_every else "")
            + f" within +/- {tol}% of the buy price on at least one day from {info['asked_date']} to {info['date']}")
else:
    head = f"{len(inr)} stock(s) within +/- {tol}% of the buy price"
st.subheader(head + (f" and passing {', '.join(x for x in filt if x)}" if any(filt) else ""))

checks = None
CHECK_MAX = 300
if check and len(inr):
    rows = []
    todo = list(zip(inr["ticker"], inr["date"] if use_range else [info["date"]] * len(inr)))
    if len(todo) > CHECK_MAX:
        st.warning(f"Trade check limited to the first {CHECK_MAX} rows of the table.")
        todo = todo[:CHECK_MAX]
    for t, d in todo:
        try:
            rows.append({**ui.en.trade_check(t, d, key, tp, tcon=ui.tech())[0], "ticker": t, "date": d})
        except ValueError as e:
            rows.append({"ticker": t, "date": d, "exit_reason": str(e)})
    checks = pd.DataFrame(rows).reindex(columns=list(pd.DataFrame(rows).columns) + [
        c for c in ("exit_date", "exit_reason", "return_pct", "max_return_pct", "max_return_date")
        if c not in pd.DataFrame(rows).columns])

sr_kind = ui.en.load_strategy(key).get("kind") == "support_resistance"
top_kind = bool(ui.en.load_strategy(key).get("top_n_mcap"))
view = inr.reindex(columns=(["date", "ticker", "days_hit", "regime"] if use_range else ["ticker"])
                   + (["mcap_rank"] if top_kind else []) + ["market_cap", "close", "buy_price", "pct_from_buy", "peak_after", "peak_after_pct",
                            "peak_after_date", "low_touched_buy", "hl_ratio",
                            "passes_hl_filter", "discount_rank", "top_n", "support", "resistance"]
                   + (["near_52w_pct", "near_which", "support_touches", "upside_pct"] if sr_kind else []) + ["pe_ttm", f"peg_{peg_yrs}y", f"eps_growth_{peg_yrs}y",
                            "revenue_growth_q_yoy", "revenue_growth_ttm_yoy", "debt_to_equity", "debt_note", "fund_filed"]).copy()
view["pct_from_buy"] *= 100
view["peak_after_pct"] *= 100
view[f"eps_growth_{peg_yrs}y"] *= 100
view["revenue_growth_q_yoy"] *= 100
view["revenue_growth_ttm_yoy"] *= 100
view["market_cap"] /= 1e9

# Add visual status badge
view["status"] = view["pct_from_buy"].apply(
    lambda p: "🟢 In buy zone" if p <= 0 else ("🟡 Near buy zone" if p <= 2.0 else "⚪ Watching")
)
if "ticker" in view.columns:
    cols = list(view.columns)
    cols.remove("status")
    cols.insert(cols.index("ticker") + 1, "status")
    view = view[cols]
if checks is not None:
    on = ["ticker", "date"] if use_range else ["ticker"]
    view = view.merge(checks[on + ["exit_date", "exit_reason", "return_pct", "max_return_pct",
                                   "max_return_date"]], on=on, how="left")
    ok = checks.dropna(subset=["return_pct"]) if "return_pct" in checks else checks.iloc[0:0]
    if len(ok):
        c1, c2, c3 = st.columns(3)
        c1.metric("Average return", ui.pct(ok["return_pct"].mean()))
        c2.metric("Winners", f"{(ok['return_pct'] > 0).sum()} / {len(ok)}")
        c3.metric("Average best point reached", ui.pct(ok["max_return_pct"].mean()))
st.dataframe(view, hide_index=True, width="stretch", column_config={
    "status": st.column_config.TextColumn("status", width="small",
                                          help="🟢 In Buy Zone: price touched or below buy level. 🟡 Near: within 2%."),
    "date": st.column_config.DateColumn("date", help="the day the stock was within range of the buy price"
                                        + ("" if not use_range else " (first such day in the range)"
                                           if not show_every else "")),
    "mcap_rank": st.column_config.NumberColumn("size rank", format="%d",
                                               help="rank by market cap among the index members that day (1 = largest; "
                                                    "close x shares known before the day)"),
    "days_hit": st.column_config.NumberColumn("days in range", format="%d",
                                              help="number of days in the date range it qualified (with the filters)"),
    "pct_from_buy": st.column_config.NumberColumn("% from buy", format="%.1f%%"),
    "close": st.column_config.NumberColumn(format="%.2f"),
    "buy_price": st.column_config.NumberColumn("buy price", format="%.2f"),
    "peak_after": st.column_config.NumberColumn("peak in next year", format="%.2f",
                                                help="highest price in the year AFTER this day (hindsight - "
                                                     "not known on the day)"),
    "peak_after_pct": st.column_config.NumberColumn("peak vs buy", format="%+.1f%%",
                                                    help="peak in the next year / buy price - 1"),
    "peak_after_date": st.column_config.DateColumn("peak date"),
    "hl_ratio": st.column_config.NumberColumn("52w H/L", format="%.2f"),
    "passes_hl_filter": "H/L <= 3",
    "low_touched_buy": "low touched buy",
    "discount_rank": st.column_config.NumberColumn("rank" if sr_kind else "discount rank", format="%d",
                                                   help="Support Buyer: support closest to the 52-week low/average "
                                                        "first (or strongest support - see config)" if sr_kind else None),
    "near_52w_pct": st.column_config.NumberColumn("support vs 52w low/avg", format="%.1f%%",
                                                  help="distance of the support from the nearer of the 52-week low "
                                                       "and the 52-week average close"),
    "near_which": "nearer to",
    "top_n": st.column_config.CheckboxColumn(f"top {ui.en.load_strategy(key)['max_positions']}",
                                             help="discount rank within the strategy's number of stocks at a time"),
    "support": st.column_config.NumberColumn(format="%.2f"),
    "resistance": st.column_config.NumberColumn(format="%.2f"),
    "pe_ttm": st.column_config.NumberColumn("P/E", format="%.1f"),
    f"peg_{peg_yrs}y": st.column_config.NumberColumn("PEG", format="%.2f",
                                                    help=f"P/E / EPS growth ({peg_yrs}y) in %"),
    f"eps_growth_{peg_yrs}y": st.column_config.NumberColumn(f"EPS growth {peg_yrs}y" + ("/yr" if peg_yrs == 3 else ""),
                                                           format="%+.1f%%"),
    "support_touches": st.column_config.NumberColumn("support touches", format="%d"),
    "upside_pct": st.column_config.NumberColumn("upside to resistance", format="%.1f%%"),
    "revenue_growth_q_yoy": st.column_config.NumberColumn("rev growth (qtr)", format="%.1f%%",
                                                          help="latest quarter vs the same quarter a year earlier"),
    "revenue_growth_ttm_yoy": st.column_config.NumberColumn("rev growth (12m)", format="%.1f%%",
                                                            help="last 12 months vs the 12 months before"),
    "market_cap": st.column_config.NumberColumn("market cap ($B)", format="%.1f",
                                                help="Yahoo's share count x this day's close"),
    "debt_to_equity": st.column_config.NumberColumn("D/E", format="%.2f"),
    "debt_note": "D/E note",
    "fund_filed": st.column_config.DateColumn("report filed"),
    "return_pct": st.column_config.NumberColumn("return", format="%+.1f%%"),
    "max_return_pct": st.column_config.NumberColumn("best point", format="%+.1f%%"),
})

if use_range:
    st.caption("Peak in next year = highest price in the year after each row's date (hindsight - not known on "
               "the day; not used by any strategy). For dates less than a year ago it is the peak SO FAR.")
elif not live and "peak_after_complete" in df:
    end = info["date"] + pd.Timedelta(days=365)
    st.caption(f"Peak in next year = highest price from the session after {info['date']} to {end:%Y-%m-%d} "
               "(hindsight - not known on the day; not used by any strategy)."
               + ("" if df["peak_after_complete"].iloc[0] else
                  " That year is not over yet, so this is the peak SO FAR, up to the last stored day."))
asof = df["market_cap_asof"].dropna() if "market_cap_asof" in df else pd.Series(dtype=object)
if asof.empty:
    st.caption("Market cap not loaded yet - run ./start.sh (or python scripts/load_market_cap.py --fetch).")
else:
    st.caption(f"Market cap = Yahoo's share count (downloaded {max(asof)}) x the close"
               + (" - for a past date this uses today's share count, so it is approximate." if not live else "."))

buf = io.BytesIO()
from pathlib import Path
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "screen.xlsx"
    if use_range:
        write_range(p, {**info, "tolerance_pct": tol, "filters": ", ".join(x for x in filt if x) or "-",
                        "rows": "every day it qualified" if show_every else "first day per stock"}, inr, checks)
    else:
        write_screen(p, info, df, checks)
    buf.write(p.read_bytes())
fname = (f"screen_{key}_{info['asked_date']}_to_{info['date']}.xlsx" if use_range
         else f"screen_{key}_{info['date']}.xlsx")
st.download_button("Download Excel", buf.getvalue(), file_name=fname)

if not use_range:
    with st.expander(f"All {len(df)} members that day"):
        st.dataframe(df[["ticker", "market_cap", "close", "buy_price", "pct_from_buy", "hl_ratio", "passes_hl_filter",
                         "discount_rank", "note"]], hide_index=True, width="stretch")
st.caption("Open a stock in the Trade check or Stock page (sidebar) for details.")
