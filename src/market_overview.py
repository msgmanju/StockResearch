"""
Market at a glance (Screener page): QQQ vs its 200-day average and the major index ETFs' performance.
Display only - no strategy or backtest uses this module.

As of the screener's day (stored closes up to that day). With a live quote, today's price is compared with the
stored history (the 200-day average is the average of the last 200 stored closes).
"""
import numpy as np
import pandas as pd

INDEX_ETFS = {"QQQ": "NASDAQ-100", "VOO": "S&P 500", "DIA": "Dow Jones", "IWM": "Russell 2000"}
BAND_PCT = 2.5
PERIODS = {"1 day": 1, "1 week": 5, "1 month": 21, "3 months": 63, "1 year": 252}


def _series(tcon, ticker, day):
    px = tcon.execute("""SELECT date, close FROM prices_daily WHERE ticker = ? AND date <= ?
                         ORDER BY date DESC LIMIT 520""", [ticker, day]).df()
    if px.empty:
        return px
    px["date"] = pd.to_datetime(px["date"]).dt.date
    return px.sort_values("date").reset_index(drop=True)


def index_row(tcon, ticker, name, day, quote=None):
    """One ETF: price, period changes, 200/50-day averages and the trend."""
    px = _series(tcon, ticker, day)
    row = {"ticker": ticker, "index": name}
    if len(px) < 2:
        row["note"] = "no prices stored yet - run ./start.sh"
        return row
    closes = list(px["close"])
    live = quote is not None and quote.get("last") is not None
    if live:                                             # today's live price on top of the stored closes
        closes = closes + [float(quote["last"])]
    price = closes[-1]
    row.update({"price": price, "as_of": "live" if live else str(px["date"].iloc[-1])})
    for label, n in PERIODS.items():
        row[label] = (price / closes[-1 - n] - 1) * 100 if len(closes) > n else np.nan
    year = (pd.Timestamp(day).year if not live else pd.Timestamp.now().year)
    prev = px[pd.to_datetime(px["date"]).dt.year < year]
    row["YTD"] = (price / prev["close"].iloc[-1] - 1) * 100 if len(prev) else np.nan
    stored = list(px["close"])
    if len(stored) >= 200:
        sma200 = float(np.mean(stored[-200:]))
        row["sma200"] = sma200
        row["vs_200d"] = (price / sma200 - 1) * 100
        row["sma200_trend"] = ("rising" if len(stored) >= 220 and sma200 > np.mean(stored[-220:-20]) else
                               "falling" if len(stored) >= 220 else "")
        s = pd.Series(stored)
        side = (s > s.rolling(200).mean()).iloc[199:]
        streak = 0
        for v in reversed(list(side)):
            if v == side.iloc[-1]:
                streak += 1
            else:
                break
        row["closes_on_this_side"] = streak
    if len(stored) >= 50:
        row["sma50"] = float(np.mean(stored[-50:]))
        row["vs_50d"] = (price / row["sma50"] - 1) * 100
    return row


def overview(tcon, day, quotes=None, etfs=None):
    """DataFrame, one row per index ETF (QQQ first)."""
    etfs = etfs or INDEX_ETFS
    return pd.DataFrame([index_row(tcon, t, n, day, (quotes or {}).get(t)) for t, n in etfs.items()])


def qqq_status(row):
    """(level, text) for the QQQ banner: level is 'ok', 'warn' or 'bad'."""
    if row is None or pd.isna(row.get("vs_200d", np.nan)):
        return "warn", "QQQ: not enough stored history for a 200-day average."
    v, trend = row["vs_200d"], row.get("sma200_trend", "")
    n = int(row.get("closes_on_this_side", 0) or 0)
    side = "above" if v >= 0 else "below"
    band = ("above the +2.5% band" if v > BAND_PCT else "below the -2.5% band" if v < -BAND_PCT
            else "inside the +/-2.5% band")
    text = (f"QQQ {row['price']:.2f} is {abs(v):.1f}% {side} its 200-day average ({row['sma200']:.2f}, "
            f"{trend}) - {band}; {n} close{'s' if n != 1 else ''} in a row {side} it.")
    level = "ok" if v > BAND_PCT else "bad" if v < -BAND_PCT else "warn"
    return level, text
