"""
Fundamental analysis page (app/pages/6_Fundamentals.py): history, P/E over time, quick checks, peers.

Everything comes from the SEC data already in the databases (fund_snapshots: one row per 10-Q/10-K, values
known on the filing date) and the stored prices. Display only - no strategy or backtest uses this module.

  history       one row per reporting period (the latest filing for that period): revenue, growth, margins,
                EPS on today's share basis (divided by every split after the filing), debt, ROE
  pe_history    daily P/E = close / EPS of the latest report filed BEFORE that day (same rule as the screener)
  latest        today's view of one stock: latest report + P/E, market cap, P/S
  checks        rule-of-thumb flags (good / watch / weak) - guides, not rules of any strategy
  peers         latest values for several stocks side by side, incl. 3-year revenue growth per year
Not available from these SEC tags: cash, free cash flow, dividends paid, buybacks.
"""
import datetime as dt

import numpy as np
import pandas as pd

from src.calculate import fundamentals as fu
from src.common import run
from src.strategy import engine as en

GOOD, WATCH, WEAK, NA = "good", "watch", "weak", "n/a"


def _splits(tcon, ticker):
    return en.splits_of(tcon, ticker)


def snapshots(fcon, ticker):
    return fcon.execute("SELECT * FROM fund_snapshots WHERE ticker = ? ORDER BY filed", [ticker]).df()


def history(fcon, tcon, ticker):
    """One row per period_end (its latest filing), oldest first, with derived ratios."""
    s = snapshots(fcon, ticker)
    if s.empty:
        return s
    s = s.sort_values(["period_end", "filed"]).drop_duplicates("period_end", keep="last").reset_index(drop=True)
    sp = _splits(tcon, ticker)
    s["split_factor"] = [fu.split_factor_after(pd.Timestamp(f).date(), sp) for f in s["filed"]]
    s["eps_today_basis"] = s["eps_diluted_ttm"] / s["split_factor"]
    s["net_margin_ttm"] = np.where(s["revenue_ttm"] > 0, s["net_income_ttm"] / s["revenue_ttm"], np.nan)
    s["roe_ttm"] = np.where(s["equity"] > 0, s["net_income_ttm"] / s["equity"], np.nan)
    return s


def pe_history(fcon, tcon, ticker, start=None, end=None):
    """Daily close and P/E (EPS of the latest report filed before the day, split-adjusted like the close)."""
    px = tcon.execute("""SELECT date, close FROM prices_daily WHERE ticker = ?
                         AND (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) ORDER BY date""",
                      [ticker, start, start, end, end]).df()
    s = snapshots(fcon, ticker)
    if px.empty or s.empty:
        return pd.DataFrame(columns=["date", "close", "eps", "pe"])
    sp = _splits(tcon, ticker)
    s = s.sort_values("filed")[["filed", "eps_diluted_ttm"]].dropna()
    s["eps"] = [e / fu.split_factor_after(pd.Timestamp(f).date(), sp) for f, e in zip(s["filed"], s["eps_diluted_ttm"])]
    s["usable_from"] = pd.to_datetime(s["filed"]) + pd.Timedelta(days=1)       # used from the day after filing
    px["date"] = pd.to_datetime(px["date"])
    d = pd.merge_asof(px.sort_values("date"), s[["usable_from", "eps"]].sort_values("usable_from"),
                      left_on="date", right_on="usable_from", direction="backward")
    d["pe"] = np.where(d["eps"] > 0, d["close"] / d["eps"], np.nan)
    return d[["date", "close", "eps", "pe"]]


def latest(fcon, tcon, ticker, day=None):
    """Latest report known on `day` (default: the last stored session) plus price-based ratios on that day."""
    day = day or run(tcon, "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]
    snap = en.fundamentals_known(fcon, [ticker], day + dt.timedelta(days=1))   # includes reports filed that day
    px = run(tcon, "SELECT date, close FROM prices_daily WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
             [ticker, day])
    out = {"ticker": ticker, "price_date": px[0][0] if px else None, "close": px[0][1] if px else None}
    if snap.empty:
        return out
    r = snap.iloc[0].to_dict()
    out.update(r)
    sp = _splits(tcon, ticker)
    filed = pd.Timestamp(r["filed"]).date()
    close = out["close"]
    out["pe_ttm"] = fu.pe_ratio(close, r["eps_diluted_ttm"], filed, sp) if close and pd.notna(r["eps_diluted_ttm"]) else None
    mc = en.market_caps(tcon, [ticker])
    out["market_cap"] = (close * mc[ticker][0] * fu.split_factor_after(mc[ticker][1], sp)
                         if ticker in mc and close else None)
    out["pe_note"] = None
    ni_ = r.get("net_income_ttm")
    if out["pe_ttm"] is None and pd.isna(r.get("eps_diluted_ttm")) and out["market_cap"] and pd.notna(ni_) and ni_ > 0:
        out["pe_ttm"] = out["market_cap"] / ni_                  # no EPS tagged (e.g. ARES): same ratio, whole company
        out["pe_note"] = "market cap / net income (no EPS in the SEC data)"
    rev = r.get("revenue_ttm")
    out["ps_ttm"] = out["market_cap"] / rev if out["market_cap"] and rev and rev > 0 else None
    ni, eq = r.get("net_income_ttm"), r.get("equity")
    out["net_margin_ttm"] = ni / rev if pd.notna(ni) and rev and rev > 0 else None
    out["roe_ttm"] = ni / eq if pd.notna(ni) and pd.notna(eq) and eq > 0 else None
    # EPS a year earlier (same basis) and 3-year revenue growth per year, from the history
    h = history(fcon, tcon, ticker)
    h = h[pd.to_datetime(h["filed"]) <= pd.Timestamp(day)]
    pe_end = pd.Timestamp(r["period_end"])
    out["eps_year_ago"] = _value_near(h, pe_end - pd.DateOffset(years=1), "eps_today_basis")
    out["eps_now_basis"] = r["eps_diluted_ttm"] / fu.split_factor_after(filed, sp) if pd.notna(r["eps_diluted_ttm"]) else None
    rev3 = _value_near(h, pe_end - pd.DateOffset(years=3), "revenue_ttm")
    out["revenue_cagr_3y"] = (rev / rev3) ** (1 / 3) - 1 if rev and rev3 and rev3 > 0 and rev > 0 else None
    pe = pe_history(fcon, tcon, ticker, start=day - dt.timedelta(days=5 * 366), end=day)
    out["pe_5y_median"] = float(pe["pe"].median()) if pe["pe"].notna().sum() > 250 else None
    return out


def _value_near(h, when, col, tol_days=45):
    """Value of `col` for the period ending within tol_days of `when` (None if there is no such period)."""
    if h.empty:
        return None
    gap = (pd.to_datetime(h["period_end"]) - when).abs()
    i = gap.idxmin()
    v = h.loc[i, col]
    return float(v) if gap[i] <= pd.Timedelta(days=tol_days) and pd.notna(v) else None


def _band(v, good, watch, higher_is_better=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return NA
    if higher_is_better:
        return GOOD if v >= good else WATCH if v >= watch else WEAK
    return GOOD if v <= good else WATCH if v <= watch else WEAK


def checks(x):
    """[(check, value text, flag, what it means)] - common rules of thumb, not strategy rules."""
    def p(v):
        return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v * 100:.1f}%"
    out = []
    g12 = x.get("revenue_growth_ttm_yoy")
    out.append(("Revenue growth (12 months)", p(g12), _band(g12, 0.15, 0.05),
                "good >= 15%, watch 5-15%, weak < 5%"))
    gq = x.get("revenue_growth_q_yoy")
    out.append(("Revenue growth (latest quarter)", p(gq), _band(gq, 0.15, 0.05),
                "same bands; a quarter far below the 12-month rate can mean growth is slowing"))
    c3 = x.get("revenue_cagr_3y")
    out.append(("Revenue growth per year (3 years)", p(c3), _band(c3, 0.10, 0.03), "good >= 10%, watch 3-10%"))
    om = x.get("op_margin_ttm")
    out.append(("Operating margin", p(om), _band(om, 0.15, 0.05), "good >= 15%, watch 5-15%, weak < 5%"))
    nm = x.get("net_margin_ttm")
    out.append(("Net margin", p(nm), _band(nm, 0.10, 0.0), "good >= 10%, watch 0-10%, weak = loss"))
    roe = x.get("roe_ttm")
    eq = x.get("equity")
    out.append(("Return on equity", p(roe) if eq is None or eq > 0 else "negative equity",
                _band(roe, 0.15, 0.05) if eq is None or eq > 0 else WEAK, "good >= 15%, watch 5-15%"))
    de = x.get("debt_to_equity")
    out.append(("Debt / equity", "-" if de is None or pd.isna(de) else f"{de:.2f}",
                WEAK if eq is not None and eq <= 0 else _band(de, 1.0, 2.0, higher_is_better=False),
                "good <= 1, watch 1-2, weak > 2 or negative equity (same colours as your Excel screener)"))
    e1, e0 = x.get("eps_now_basis"), x.get("eps_year_ago")
    ch = (e1 / e0 - 1) if e1 is not None and e0 and e0 > 0 else None
    out.append(("EPS vs a year ago", p(ch) if ch is not None else ("loss" if e1 is not None and e1 <= 0 else "-"),
                _band(ch, 0.10, 0.0) if ch is not None else (WEAK if e1 is not None and e1 <= 0 else NA),
                "good >= +10%, watch 0-10%, weak = falling or a loss"))
    pe, med = x.get("pe_ttm"), x.get("pe_5y_median")
    if pe is None:
        out.append(("P/E", "loss" if e1 is not None and e1 <= 0 else "-", WEAK if e1 is not None and e1 <= 0 else NA,
                    "no P/E without profits"))
    else:
        rel = pe / med - 1 if med else None
        out.append(("P/E vs its own 5-year median", f"{pe:.1f} vs {med:.1f}" if med else f"{pe:.1f}",
                    _band(rel, -0.10, 0.20, higher_is_better=False) if rel is not None else NA,
                    "good = at least 10% below its usual P/E, watch = up to 20% above, weak = more than 20% above"))
        out.append(("P/E level", f"{pe:.1f}", _band(pe, 25, 35, higher_is_better=False),
                    "good <= 25, watch 25-35, weak > 35 (your Excel screener marks > 35 red)"))
    return out


def peers(fcon, tcon, tickers, day=None):
    rows = []
    for t in tickers:
        x = latest(fcon, tcon, t, day)
        e1, e0 = x.get("eps_now_basis"), x.get("eps_year_ago")
        rows.append({"ticker": t, "market_cap": x.get("market_cap"), "close": x.get("close"),
                     "pe_ttm": x.get("pe_ttm"), "pe_5y_median": x.get("pe_5y_median"), "ps_ttm": x.get("ps_ttm"),
                     "revenue_ttm": x.get("revenue_ttm"), "revenue_growth_q_yoy": x.get("revenue_growth_q_yoy"),
                     "revenue_growth_ttm_yoy": x.get("revenue_growth_ttm_yoy"),
                     "revenue_cagr_3y": x.get("revenue_cagr_3y"), "op_margin_ttm": x.get("op_margin_ttm"),
                     "net_margin_ttm": x.get("net_margin_ttm"), "roe_ttm": x.get("roe_ttm"),
                     "eps_growth_yoy": (e1 / e0 - 1) if e1 is not None and e0 and e0 > 0 else None,
                     "debt_to_equity": x.get("debt_to_equity"), "period_end": x.get("period_end"),
                     "filed": x.get("filed")})
    return pd.DataFrame(rows)
