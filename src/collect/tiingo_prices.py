"""
Tiingo as the STORED price source - used only when Yahoo has no data
(usually a delisted stock). Output matches yahoo_prices.fetch():
open/high/low/close split-adjusted (not dividend-adjusted), adj_close
split- and dividend-adjusted, volume split-adjusted.
Tiingo stocks are always downloaded in full (one request), because Tiingo
reports actual traded prices and the split adjustment needs the whole history.
"""
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from src.collect.second_sources import tiingo_raw
from src.common import START_DATE

SOURCE = "tiingo"
PRICE_COLS = ["date", "open", "high", "low", "close", "adj_close", "volume"]


def transform(rows):
    """Tiingo rows (list of dicts) -> (prices, actions) in our stored format."""
    if not rows:
        return (pd.DataFrame(columns=PRICE_COLS), pd.DataFrame(columns=["date", "action", "value"]))
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    sf = df.get("splitFactor", pd.Series(1.0, index=df.index)).fillna(1.0).astype(float)
    # product of the split factors on all LATER rows
    after = sf[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    prices = pd.DataFrame({
        "date": df["date"],
        "open": df["open"] / after, "high": df["high"] / after,
        "low": df["low"] / after, "close": df["close"] / after,
        "adj_close": df.get("adjClose", df["close"] / after),
        "volume": (df["volume"] * after).round().astype("int64"),
    })
    acts = [{"date": d, "action": "split", "value": float(v)} for d, v in zip(df["date"], sf) if v != 1.0]
    div = df.get("divCash", pd.Series(0.0, index=df.index)).fillna(0.0).astype(float)
    acts += [{"date": d, "action": "dividend", "value": float(v / a)}
             for d, v, a in zip(df["date"], div, after) if v > 0]
    prices = prices.dropna(subset=["close"])
    prices = prices[~filler_rows(prices)].reset_index(drop=True)
    return prices, pd.DataFrame(acts, columns=["date", "action", "value"])


def filler_rows(prices):
    """Rows Tiingo adds when nothing really traded (e.g. after a takeover closed or during a halt):
    open = high = low = close = the previous close, with zero volume or under 1% of the stock's
    usual volume. Not real prices."""
    if prices.empty:
        return pd.Series(False, index=prices.index)
    p = prices.sort_values("date")
    flat = (p["open"] == p["close"]) & (p["high"] == p["close"]) & (p["low"] == p["close"])
    vol = p["volume"].fillna(0)
    typical = vol.where(vol > 0).rolling(60, min_periods=5).median().shift(1).ffill()
    tiny = (vol == 0) | (vol < 0.01 * typical)          # e.g. 258 shares vs 20 million normally
    return (tiny & flat & (p["close"] == p["close"].shift(1))).reindex(prices.index)


def fetch(ticker: str, start: dt.date = START_DATE, end: dt.date | None = None):
    """Same return shape as yahoo_prices.fetch(): (prices, actions, raw_file)."""
    rows, raw_file = tiingo_raw(ticker, START_DATE, end or dt.date.today())
    prices, actions = transform(rows)
    return prices, actions, str(raw_file)


def replay_raw(raw_dir: Path, ticker: str):
    """Rebuild the stored prices from the fullest saved Tiingo download (for the audit)."""
    files = sorted((raw_dir / "tiingo" / "prices" / ticker).glob("*.json"))
    best = None
    for f in files:
        rows = json.loads(f.read_text())
        if isinstance(rows, list) and (best is None or len(rows) >= len(best[1])):
            best = (f, rows)
    if best is None:
        return None, []
    prices, _ = transform(best[1])
    return prices, [best[0]]
