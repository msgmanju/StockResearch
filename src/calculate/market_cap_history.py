"""
Point-in-time market cap for every stock and day - used to keep only the N largest companies
(backtest "top N by market cap", strategy [bigcap_dip_buyer]). Uses only what was known on each day.

    market cap on day D = close on D x shares known before D (on today's share basis, like the prices)

Shares known before D, first that applies:
  1. "sec"       latest SEC diluted share count (WeightedAverageNumberOfDilutedSharesOutstanding) of a report
                 filed before D whose period ended at most MAX_AGE_DAYS before D
  2. "ni/eps"    TTM net income / TTM diluted EPS of the latest report filed before D (same age limit)
  3. "yahoo"     today's share count from Yahoo (market_cap table) - companies whose SEC data has no share
                 count (V, BRK-B, KKR ...); a constant, so approximate for older days
  4. "sec-old"   the last SEC share count whatever its age (BIDU stopped tagging it after 2010)
  5. "sec-later" the first SEC share count filed AFTER the day (foreign IFRS filers such as VOD have SEC data
                 only from 2018) - a little hindsight, but only about the share count
Share basis: SEC counts are converted to today's listed shares - stock splits after the filing are applied;
foreign ADSs use config/share_basis.toml (ordinary shares per ADS, share subdivisions).
Bad values: a count more than 5x away from the median of the company's own counts is dropped
(e.g. GFS FY2021 tagged 505,758 million shares).
Companies with several listed share classes (GOOG/GOOGL, FOX/FOXA): SEC's diluted count covers the whole
company, so each class shows the whole company's value; for ranking they count as ONE company (same SEC CIK).
"""
import datetime as dt
import tomllib

import numpy as np
import pandas as pd

from src.calculate import fundamentals as fu
from src.common import ROOT, run

MAX_AGE_DAYS = 400
OUTLIER = 5.0
BASIS_FILE = ROOT / "config" / "share_basis.toml"


def share_basis():
    if not BASIS_FILE.exists():
        return {}
    with open(BASIS_FILE, "rb") as f:
        rows = tomllib.load(f).get("ticker", [])
    return {r["ticker"]: {"per_listed": float(r["ordinary_per_listed"]),
                          "subdivisions": [(dt.date.fromisoformat(d), float(k)) for d, k in r.get("subdivisions", [])]}
            for r in rows}


def _splits(tcon, tickers):
    q = ",".join("?" * len(tickers))
    out = {}
    for t, d, v in run(tcon, f"SELECT ticker, date, value FROM corporate_actions WHERE action = 'split' AND ticker IN ({q})",
                       list(tickers)):
        out.setdefault(t, []).append((d, v))
    return {t: fu.real_splits(v) for t, v in out.items()}


def to_listed(ticker, filed, count, splits, basis):
    """A share count from a filing on `filed` -> today's listed-share basis."""
    b = basis.get(ticker)
    if b:
        f = 1.0
        for d, k in b["subdivisions"]:
            if filed < d:
                f *= k
        return count * f / b["per_listed"]
    return count * fu.split_factor_after(filed, splits.get(ticker, []))


def shares_history(fcon, tcon, tickers):
    """All share counts with the day they become usable: ticker, usable_from, period_end, shares, source."""
    tickers = sorted(set(tickers))
    q = ",".join("?" * len(tickers))
    basis, splits = share_basis(), _splits(tcon, tickers)
    f = fcon.execute(f"""SELECT ticker, accn, filed, "end" AS period_end, start, val FROM fund_facts
                         WHERE metric = 'shares_diluted' AND val > 0 AND ticker IN ({q})""", tickers).df()
    if len(f):
        f["dur"] = (pd.to_datetime(f["period_end"]) - pd.to_datetime(f["start"])).dt.days
        # per filing: the latest period, the shortest one (a quarter rather than year-to-date)
        f = f.sort_values(["ticker", "accn", "period_end", "dur"], ascending=[True, True, False, True])
        f = f.drop_duplicates(["ticker", "accn"])
        f["source"] = "sec"
    n = fcon.execute(f"""SELECT ticker, accn, filed, period_end, net_income_ttm / eps_diluted_ttm AS val
                         FROM fund_snapshots WHERE ticker IN ({q}) AND abs(eps_diluted_ttm) >= 0.05
                         AND net_income_ttm / eps_diluted_ttm > 0""", tickers).df()
    n["source"] = "ni/eps"
    h = pd.concat([f[["ticker", "filed", "period_end", "val", "source"]] if len(f) else None, n[
        ["ticker", "filed", "period_end", "val", "source"]]], ignore_index=True)
    if h.empty:
        return pd.DataFrame(columns=["ticker", "usable_from", "period_end", "shares", "source"])
    h["filed"] = pd.to_datetime(h["filed"]).dt.date
    h["shares"] = [to_listed(t, fd, v, splits, basis) for t, fd, v in zip(h["ticker"], h["filed"], h["val"])]
    med = h.groupby(["ticker", "source"])["shares"].transform("median")
    h = h[(h["shares"] <= med * OUTLIER) & (h["shares"] >= med / OUTLIER)]
    h["usable_from"] = pd.to_datetime(h["filed"]) + pd.Timedelta(days=1)
    h["period_end"] = pd.to_datetime(h["period_end"])
    return h[["ticker", "usable_from", "period_end", "shares", "source"]].sort_values("usable_from").reset_index(drop=True)


def yahoo_shares(tcon, tickers):
    """Today's share count from the market_cap table (Yahoo), on today's basis."""
    try:
        q = ",".join("?" * len(tickers))
        rows = run(tcon, f"SELECT ticker, shares, fetched_on FROM market_cap WHERE ticker IN ({q})", list(tickers))
    except Exception:
        return {}
    sp = _splits(tcon, [r[0] for r in rows]) if rows else {}
    return {t: s * fu.split_factor_after(d, sp.get(t, [])) for t, s, d in rows if s}


def company_keys(fcon, tickers):
    """{ticker: company key} - the SEC CIK (share classes of one company share it); ticker if unknown."""
    q = ",".join("?" * len(tickers))
    rows = run(fcon, f"SELECT ticker, cik FROM cik_map WHERE ticker IN ({q}) ORDER BY ticker, seq DESC", list(tickers))
    out = {}
    for t, c in rows:
        out.setdefault(t, str(c))           # the latest CIK of the ticker
    return {t: out.get(t, t) for t in tickers}


def add_market_cap(d, fcon, tcon, hist=None):
    """d: DataFrame with date, ticker, close. Adds shares, market_cap, mcap_source (point in time)."""
    tickers = sorted(d["ticker"].unique())
    hist = shares_history(fcon, tcon, tickers) if hist is None else hist
    x = d.assign(_ts=pd.to_datetime(d["date"]).astype("datetime64[ns]")).reset_index().sort_values("_ts")
    out = x[["index", "ticker", "_ts"]].copy()
    for src in ("sec", "ni/eps"):
        s = hist[hist["source"] == src][["ticker", "usable_from", "period_end", "shares"]].copy()
        s["usable_from"] = s["usable_from"].astype("datetime64[ns]")
        m = pd.merge_asof(out.sort_values("_ts"), s.sort_values("usable_from"), left_on="_ts",
                          right_on="usable_from", by="ticker", direction="backward")
        fresh = (m["_ts"] - m["period_end"]).dt.days <= MAX_AGE_DAYS
        out = out.merge(m.loc[:, ["index"]].assign(**{f"sh_{src}": np.where(fresh, m["shares"], np.nan),
                                                      f"any_{src}": m["shares"]}), on="index")
        if src == "sec":
            later = pd.merge_asof(out[["index", "ticker", "_ts"]].sort_values("_ts"), s.sort_values("usable_from"),
                                  left_on="_ts", right_on="usable_from", by="ticker", direction="forward")
            out = out.merge(later[["index"]].assign(later_sec=later["shares"]), on="index")
    ya = yahoo_shares(tcon, tickers)
    out["sh_yahoo"] = out["ticker"].map(ya)
    out = out.set_index("index").reindex(d.index)
    sh = out["sh_sec"].copy()
    src = pd.Series(np.where(sh.notna(), "sec", None), index=d.index, dtype=object)
    for col, name in (("sh_ni/eps", "ni/eps"), ("sh_yahoo", "yahoo"), ("any_sec", "sec-old"),
                      ("later_sec", "sec-later")):
        fill = sh.isna() & out[col].notna()
        sh[fill] = out.loc[fill, col]
        src[fill] = name
    d = d.copy()
    d["shares"] = sh.astype(float)
    d["market_cap"] = d["close"] * d["shares"]
    d["mcap_source"] = src
    return d


def add_mcap_rank(d, fcon, member_col="member"):
    """Rank of each row's COMPANY by market cap among the rows with member_col True that day (1 = largest).
    Share classes of one company get the same rank and count once."""
    keys = company_keys(fcon, sorted(d["ticker"].unique()))
    d = d.copy()
    d["company"] = d["ticker"].map(keys)
    m = d[d[member_col] & d["market_cap"].notna()]
    comp = m.groupby(["date", "company"])["market_cap"].max().reset_index()
    comp["mcap_rank"] = comp.groupby("date")["market_cap"].rank(ascending=False, method="first")
    d = d.drop(columns=["mcap_rank"], errors="ignore").merge(comp[["date", "company", "mcap_rank"]],
                                                            on=["date", "company"], how="left")
    d.loc[~d[member_col], "mcap_rank"] = np.nan
    return d
