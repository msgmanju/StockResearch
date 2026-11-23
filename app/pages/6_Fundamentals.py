import datetime as dt
import io

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

import ui_common as ui
from src import fundamental_analysis as fa

st.set_page_config(page_title="Fundamentals", layout="wide")
st.title("Fundamental analysis")
FLAG = {fa.GOOD: "🟢 good", fa.WATCH: "🟡 watch", fa.WEAK: "🔴 weak", fa.NA: "⚪ n/a"}


@st.cache_data
def fund_tickers():
    have = {r[0] for r in ui.run(ui.fund(), "SELECT DISTINCT ticker FROM fund_snapshots")}
    return [t for t in ui.all_tickers() if t in have]


@st.cache_data(show_spinner=False)
def load(ticker, as_of):
    return fa.latest(ui.fund(), ui.tech(), ticker, as_of), fa.history(ui.fund(), ui.tech(), ticker)


@st.cache_data(show_spinner=False)
def load_pe(ticker, start, end):
    return fa.pe_history(ui.fund(), ui.tech(), ticker, start, end)


@st.cache_data(show_spinner="Loading the comparison ...")
def load_peers(tickers, as_of):
    return fa.peers(ui.fund(), ui.tech(), list(tickers), as_of)


def num(v, fmt="{:.1f}", none="-"):
    return none if v is None or (isinstance(v, float) and np.isnan(v)) else fmt.format(v)


def pct(v, fmt="{:.1f}%"):
    return num(None if v is None or pd.isna(v) else v * 100, fmt)


def money(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"${v / 1e12:.2f}T" if abs(v) >= 1e12 else f"${v / 1e9:.1f}B" if abs(v) >= 1e9 else f"${v / 1e6:.0f}M"


def line_chart(d, cols, title, fmt=".1f", rules=None, height=260):
    long = d.melt("date", cols, var_name="series", value_name="value").dropna()
    if long.empty:
        return st.caption(f"{title}: no data")
    ch = alt.Chart(long).mark_line(point=len(d) < 80).encode(
        x=alt.X("date:T", title=None), y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False)),
        color=alt.Color("series:N", legend=alt.Legend(orient="bottom", title=None)),
        tooltip=["date:T", "series:N", alt.Tooltip("value:Q", format=fmt)])
    for v, label in (rules or []):
        ch += alt.Chart(pd.DataFrame({"y": [v], "label": [label]})).mark_rule(strokeDash=[4, 3], color="gray").encode(
            y="y:Q", tooltip=["label:N"])
    st.altair_chart(ch.properties(title=title, height=height), width="stretch")


def bar_chart(d, col, title, fmt=".2f", height=260):
    x = d[["date", col]].dropna()
    if x.empty:
        return st.caption(f"{title}: no data")
    ch = alt.Chart(x).mark_bar().encode(
        x=alt.X("date:T", title=None), y=alt.Y(f"{col}:Q", title=None),
        color=alt.condition(f"datum.{col} < 0", alt.value("#d62728"), alt.value("#1f77b4")),
        tooltip=["date:T", alt.Tooltip(f"{col}:Q", format=fmt)])
    st.altair_chart(ch.properties(title=title, height=height), width="stretch")


# ------------------------------------------------------------------ choose
tickers = fund_tickers()
with st.sidebar:
    ticker = st.selectbox("Stock", tickers, index=tickers.index("AAPL") if "AAPL" in tickers else 0)
    years = st.slider("Years of history", 2, 10, 5)
as_of = ui.last_session()
x, h = load(ticker, as_of)
mem = ui.tech().execute("""SELECT index_name, company, end_date FROM index_membership WHERE ticker = ?
                           ORDER BY end_date IS NULL DESC, end_date DESC""", [ticker]).df()
name = mem["company"].iloc[0] if len(mem) else ticker
now_in = sorted(mem.loc[mem["end_date"].isna(), "index_name"].unique()) if len(mem) else []
st.subheader(f"{ticker} - {name}")
if "filed" not in x:
    st.info("No SEC fundamentals for this stock.")
    st.stop()
st.caption(f"Latest report: {x.get('form')} for the period to {pd.Timestamp(x['period_end']):%Y-%m-%d}, filed "
           f"{pd.Timestamp(x['filed']):%Y-%m-%d} | price {num(x.get('close'), '{:.2f}')} on {x.get('price_date')} | "
           f"{'in ' + ', '.join(now_in) if now_in else 'not in an index now'}"
           + (" | reports in " + x["currency"] if x.get("currency") not in (None, "USD") else "")
           + (" | annual reports only (foreign filer)" if x.get("annual_only") else ""))

# ------------------------------------------------------------------ headline numbers
e1, e0 = x.get("eps_now_basis"), x.get("eps_year_ago")
c = st.columns(5)
c[0].metric("Market cap", money(x.get("market_cap")))
c[1].metric("P/E (TTM)", num(x.get("pe_ttm")), f"5-yr median {num(x.get('pe_5y_median'))}", delta_color="off",
            help=x.get("pe_note"))
c[2].metric("Price / sales", num(x.get("ps_ttm"), "{:.1f}x"))
c[3].metric("Revenue (12 months)", money(x.get("revenue_ttm")),
            f"{x['revenue_growth_ttm_yoy'] * 100:+.1f}% vs a year ago" if pd.notna(x.get("revenue_growth_ttm_yoy")) else None)
c[4].metric("EPS (12 months)", num(e1, "{:.2f}"),
            f"{(e1 / e0 - 1) * 100:+.1f}% vs a year ago" if e1 is not None and e0 and e0 > 0 else None)
c = st.columns(5)
c[0].metric("Revenue growth (latest qtr)", pct(x.get("revenue_growth_q_yoy"), "{:+.1f}%"))
c[1].metric("Operating margin", pct(x.get("op_margin_ttm")))
c[2].metric("Net margin", pct(x.get("net_margin_ttm")))
c[3].metric("Return on equity", pct(x.get("roe_ttm")))
c[4].metric("Debt / equity", num(x.get("debt_to_equity"), "{:.2f}"))

st.markdown("**Quick checks** - common rules of thumb to start your own review; not part of any strategy")
chk = pd.DataFrame(fa.checks(x), columns=["check", "value", "flag", "guide"])
chk["flag"] = chk["flag"].map(FLAG)
st.dataframe(chk[["flag", "check", "value", "guide"]], hide_index=True, width="stretch")
n = chk["flag"].value_counts()
st.caption(f"{n.get(FLAG[fa.GOOD], 0)} good, {n.get(FLAG[fa.WATCH], 0)} watch, {n.get(FLAG[fa.WEAK], 0)} weak. "
           "Banks, insurers and REITs report differently - margins and debt/equity mean less for them.")

# ------------------------------------------------------------------ history
start = as_of - dt.timedelta(days=int(365.25 * years))
hh = h[pd.to_datetime(h["period_end"]) >= pd.Timestamp(start)].copy()
hh["date"] = pd.to_datetime(hh["period_end"])
for col in ("revenue_ttm", "revenue_q", "net_income_ttm", "op_income_ttm", "equity", "total_debt"):
    hh[col + "_bn"] = hh[col] / 1e9
for col in ("revenue_growth_q_yoy", "revenue_growth_ttm_yoy", "op_margin_ttm", "net_margin_ttm", "roe_ttm"):
    hh[col + "_pct"] = hh[col] * 100

tabs = st.tabs(["Growth", "Profitability", "Valuation", "Balance sheet", "Compare stocks", "All reports"])
with tabs[0]:
    a, b = st.columns(2)
    with a:
        bar_chart(hh, "revenue_ttm_bn", "Revenue, last 12 months ($B)")
    with b:
        bar_chart(hh, "revenue_q_bn", "Revenue, each quarter ($B)")
    line_chart(hh.rename(columns={"revenue_growth_q_yoy_pct": "quarter vs a year ago",
                                  "revenue_growth_ttm_yoy_pct": "12 months vs the 12 before"}),
               ["quarter vs a year ago", "12 months vs the 12 before"], "Revenue growth (%)",
               rules=[(15, "15%"), (0, "0%")])
with tabs[1]:
    a, b = st.columns(2)
    with a:
        line_chart(hh.rename(columns={"op_margin_ttm_pct": "operating margin", "net_margin_ttm_pct": "net margin"}),
                   ["operating margin", "net margin"], "Margins, last 12 months (%)", rules=[(15, "15%"), (0, "0%")])
    with b:
        bar_chart(hh, "eps_today_basis", "EPS, last 12 months (on today's share count)")
    a, b = st.columns(2)
    with a:
        bar_chart(hh, "net_income_ttm_bn", "Net income, last 12 months ($B)")
    with b:
        line_chart(hh.rename(columns={"roe_ttm_pct": "return on equity"}), ["return on equity"],
                   "Return on equity (%)", rules=[(15, "15%")])
with tabs[2]:
    pe = load_pe(ticker, start, as_of)
    med = x.get("pe_5y_median")
    if pe["pe"].notna().any():
        pe = pe.rename(columns={"pe": "P/E"})
        line_chart(pe, ["P/E"], "P/E each day (price / EPS of the latest report filed before that day)",
                   rules=[(med, f"5-year median {med:.1f}")] if med else None, height=300)
        line_chart(pe.rename(columns={"close": "price"}), ["price"], "Price (split-adjusted)", fmt=".2f", height=220)
        q = pe["P/E"].dropna()
        st.caption(f"P/E over these {years} years: low {q.min():.1f}, median {q.median():.1f}, high {q.max():.1f}; "
                   f"now {num(x.get('pe_ttm'))}. Gaps = loss-making periods (no P/E).")
    else:
        st.info("No P/E history (losses, or no EPS reported).")
with tabs[3]:
    a, b = st.columns(2)
    with a:
        line_chart(hh.rename(columns={"equity_bn": "equity", "total_debt_bn": "debt"}), ["equity", "debt"],
                   "Equity and debt ($B)", fmt=".2f")
    with b:
        line_chart(hh.rename(columns={"debt_to_equity": "debt / equity"}), ["debt / equity"], "Debt / equity",
                   fmt=".2f", rules=[(1, "1 (watch above)"), (2, "2 (weak above)")])
    notes = hh["debt_note"].dropna()
    notes = notes[notes != "reported"]
    if len(notes):
        st.caption("Debt notes: " + "; ".join(sorted(set(notes))[:5]))
with tabs[4]:
    default = [ticker] + [t for t in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN") if t != ticker][:4]
    pick = st.multiselect("Stocks to compare", tickers, default=default, max_selections=15)
    if pick:
        p = load_peers(tuple(pick), as_of).copy()
        for col in ("revenue_growth_q_yoy", "revenue_growth_ttm_yoy", "revenue_cagr_3y", "op_margin_ttm",
                    "net_margin_ttm", "roe_ttm", "eps_growth_yoy"):
            p[col] = p[col].astype(float) * 100
        p["market_cap"] = p["market_cap"].astype(float) / 1e9
        p["revenue_ttm"] = p["revenue_ttm"].astype(float) / 1e9
        pc = st.column_config
        st.dataframe(p, hide_index=True, width="stretch", column_config={
            "market_cap": pc.NumberColumn("market cap ($B)", format="%.0f"),
            "close": pc.NumberColumn(format="%.2f"),
            "pe_ttm": pc.NumberColumn("P/E", format="%.1f"),
            "pe_5y_median": pc.NumberColumn("P/E 5y median", format="%.1f"),
            "ps_ttm": pc.NumberColumn("P/S", format="%.1f"),
            "revenue_ttm": pc.NumberColumn("revenue 12m ($B)", format="%.1f"),
            "revenue_growth_q_yoy": pc.NumberColumn("rev growth qtr", format="%.1f%%"),
            "revenue_growth_ttm_yoy": pc.NumberColumn("rev growth 12m", format="%.1f%%"),
            "revenue_cagr_3y": pc.NumberColumn("rev growth/yr 3y", format="%.1f%%"),
            "op_margin_ttm": pc.NumberColumn("op margin", format="%.1f%%"),
            "net_margin_ttm": pc.NumberColumn("net margin", format="%.1f%%"),
            "roe_ttm": pc.NumberColumn("ROE", format="%.1f%%"),
            "eps_growth_yoy": pc.NumberColumn("EPS growth", format="%+.1f%%"),
            "debt_to_equity": pc.NumberColumn("D/E", format="%.2f"),
            "period_end": pc.DateColumn("period"),
            "filed": pc.DateColumn("filed")})
        st.caption("Click a column heading to sort. Values as of the latest report filed by "
                   f"{as_of}; P/E and market cap at the {as_of} close.")
with tabs[5]:
    cols = ["period_end", "filed", "form", "revenue_ttm", "revenue_q", "revenue_growth_q_yoy",
            "revenue_growth_ttm_yoy", "op_income_ttm", "op_margin_ttm", "net_income_ttm", "net_margin_ttm",
            "eps_diluted_ttm", "split_factor", "eps_today_basis", "equity", "total_debt", "debt_to_equity",
            "roe_ttm", "debt_note", "revenue_ttm_how", "eps_source"]
    rep = h.sort_values("period_end", ascending=False)[[c for c in cols if c in h.columns]]
    st.dataframe(rep, hide_index=True, width="stretch")
    st.caption("One row per period (its latest filing). Money in the company's reporting currency. 'eps_diluted_ttm' "
               "is as reported; 'eps_today_basis' divides it by stock splits since, to match today's prices.")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(fa.checks(x), columns=["check", "value", "flag", "guide"]).to_excel(xw, sheet_name="checks", index=False)
        rep.to_excel(xw, sheet_name="reports", index=False)
    st.download_button("Download Excel", buf.getvalue(), file_name=f"fundamentals_{ticker}_{as_of}.xlsx")

st.caption("Source: SEC 10-Q/10-K filings already in your database (each value known from the day after it was "
           "filed). Not available from this data: cash, free cash flow, dividends paid, buybacks.")
