"""
Live screen: today's prices right now (Yahoo, about real time during market hours) against the buy price
known this morning.

Nothing here is stored. The databases keep only completed days (scripts/daily_update.py); a day still
trading is a moving target, so it is used only on screen.

Point in time - the same rule as the backtest and the daily screener:
  buy price for today = 0.70 x lowest low + 0.30 x average close of the 252 sessions BEFORE today
                        (all stored, so it does not change during the day)
  today's open/high/low/last come from Yahoo's 1-minute bars of today
  % from buy = last / buy - 1;  low touched buy = today's low so far <= buy price
"""
import datetime as dt

import pandas as pd

from src import membership as mb
from src.common import run
from src import update_status as us
from src.calculate import support_resistance
from src.strategy import engine as en

LOOKBACK_52W, LOOKBACK_6M = 252, 126


def quotes(tickers, downloader=None):
    """{ticker: {open, high, low, last, time, day}} for the latest trading day Yahoo has 1-minute bars for."""
    if downloader is None:
        import yfinance as yf

        def downloader(t):
            return yf.download(t, period="1d", interval="1m", group_by="ticker", auto_adjust=False,
                               progress=False, threads=True, prepost=False)
    raw = downloader(list(tickers))
    out = {}
    if raw is None or raw.empty:
        return out
    multi = isinstance(raw.columns, pd.MultiIndex)
    for t in tickers:
        try:
            d = raw[t] if multi else raw
        except KeyError:
            continue
        d = d.dropna(subset=["Close"])
        if d.empty:
            continue
        idx = d.index.tz_convert(us.NY) if d.index.tz is not None else d.index
        out[t] = {"open": float(d["Open"].iloc[0]), "high": float(d["High"].max()), "low": float(d["Low"].min()),
                  "last": float(d["Close"].iloc[-1]), "time": idx[-1].to_pydatetime(), "day": idx[-1].date()}
    return out


def base_values(tcon, tickers):
    """52-week values for the NEXT session: the latest 252 stored sessions of each ticker (the last one included)."""
    q = ",".join("?" * len(tickers))
    return tcon.execute(f"""
        WITH r AS (SELECT ticker, date, low, high, close,
                          row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                   FROM prices_daily WHERE ticker IN ({q}))
        SELECT ticker,
               CASE WHEN count(*) FILTER (WHERE rn <= {LOOKBACK_52W}) = {LOOKBACK_52W}
                    THEN min(low) FILTER (WHERE rn <= {LOOKBACK_52W}) END AS low_52w,
               CASE WHEN count(*) FILTER (WHERE rn <= {LOOKBACK_52W}) = {LOOKBACK_52W}
                    THEN max(high) FILTER (WHERE rn <= {LOOKBACK_52W}) END AS high_52w,
               CASE WHEN count(*) FILTER (WHERE rn <= {LOOKBACK_52W}) = {LOOKBACK_52W}
                    THEN avg(close) FILTER (WHERE rn <= {LOOKBACK_52W}) END AS avg_close_52w,
               CASE WHEN count(*) FILTER (WHERE rn <= {LOOKBACK_6M}) = {LOOKBACK_6M}
                    THEN min(low) FILTER (WHERE rn <= {LOOKBACK_6M}) END AS low_6m,
               max(date) AS last_stored
        FROM r WHERE rn <= {LOOKBACK_52W} GROUP BY ticker""", list(tickers)).df()


def live_day(tcon):
    """The trading day being traded now (or just finished but not stored yet): None if there is none."""
    now = dt.datetime.now(us.NY)
    today = now.date()
    days = mb.sessions(today - dt.timedelta(days=5), today)
    if not days or days[-1] != today or now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return None
    stored = run(tcon, "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]
    return today if stored is None or stored < today else None


def next_levels(tcon, tickers):
    """{ticker: (ref, support, touches, resistance, touches)} for the next session (from the stored days)."""
    return {t: support_resistance.next_session(tcon, t) for t in tickers}


def screen(strategy="dip_buyer", tol_pct=5.0, universe=None, tcon=None, fcon=None, quotes_=None, day=None, levels=None):
    """Like engine.screen for today, with live prices. Returns (info, DataFrame)."""
    s = en.load_strategy(strategy)
    day = day or dt.datetime.now(us.NY).date()
    last_stored = run(tcon, "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]
    uni = (universe or s["universe"]).upper()
    members = en.members_on(tcon, uni, last_stored)
    reg = en.regime(tcon, last_stored, s)
    info = {"strategy": s["name"], "asked_date": day, "date": day, "universe": uni, "members": len(members),
            "tolerance_pct": tol_pct, "regime": reg, "live": True, "values_through": last_stored}
    if not members:
        return info, pd.DataFrame()
    base = base_values(tcon, members)
    q = quotes_ or {}
    rows = []
    for t in members:
        x = q.get(t)
        if x and x["day"] == day:
            rows.append({"ticker": t, "open": x["open"], "high": x["high"], "low": x["low"], "close": x["last"],
                         "quote_time": x["time"]})
    df = pd.DataFrame(rows, columns=["ticker", "open", "high", "low", "close", "quote_time"])
    info["no_price_that_day"] = sorted(set(members) - set(df["ticker"]))
    df = df.merge(base, on="ticker", how="left")
    stale = df["last_stored"].notna() & (df["last_stored"].astype("datetime64[ns]").dt.date < last_stored)
    sr_strategy = s.get("kind") == "support_resistance"
    if sr_strategy:                                   # strategy 2 needs today's support for every member
        sr = levels or {t: support_resistance.next_session(tcon, t) for t in df["ticker"]}
        for k, col in ((1, "support"), (2, "support_touches"), (3, "resistance"), (4, "resistance_touches")):
            df[col] = df["ticker"].map(lambda t: sr[t][k] if sr.get(t) else None)
    df = en.derive(df, s, reg, tol_pct, tcon, fcon, day)
    df.loc[df["ticker"].isin(df.loc[stale, "ticker"]), "note"] = "prices not stored up to the last session"
    if not sr_strategy:                               # support/resistance shown for the stocks in range only
        sr = {t: support_resistance.next_session(tcon, t) for t in df.loc[df["in_range"], "ticker"]}
        df["support"] = df["ticker"].map(lambda t: sr[t][1] if sr.get(t) else None)
        df["resistance"] = df["ticker"].map(lambda t: sr[t][3] if sr.get(t) else None)
    info["quote_time"] = df["quote_time"].max() if len(df) else None
    return info, df
