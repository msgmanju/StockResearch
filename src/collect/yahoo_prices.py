"""Daily prices, splits and dividends from Yahoo Finance (via yfinance)."""
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from src.common import RAW_DIR, SETTINGS

SOURCE = "yahoo"
_TZ = ZoneInfo(SETTINGS["market"]["timezone"])
_DAY_DONE = int(SETTINGS["market"]["day_complete_hour"])

PRICE_COLS = ["date", "open", "high", "low", "close", "adj_close", "volume"]


def _download(ticker: str, start: dt.date) -> pd.DataFrame:
    import yfinance as yf
    return yf.Ticker(ticker).history(start=start.isoformat(), interval="1d",
                                     auto_adjust=False, actions=True)


def fetch(ticker: str, start: dt.date, downloader=_download, now=None):
    """
    Returns (prices, actions, raw_file).
      prices : date, open, high, low, close, adj_close, volume
      actions: date, action, value
      raw_file: path of the untouched download saved under data/raw/yahoo/prices/
    Today's bar is dropped until the market day is complete (New York time).
    """
    raw = downloader(ticker, start)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=PRICE_COLS), pd.DataFrame(columns=["date", "action", "value"]), None

    raw = raw.reset_index()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    folder = RAW_DIR / "yahoo" / "prices" / ticker
    folder.mkdir(parents=True, exist_ok=True)
    raw_file = folder / f"{ticker}_{stamp}.csv"
    raw.to_csv(raw_file, index=False)

    df = raw.rename(columns={"Date": "date", "Open": "open", "High": "high", "Low": "low",
                             "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
                             "Dividends": "dividends", "Stock Splits": "splits"})
    dates = pd.to_datetime(df["date"])
    if dates.dt.tz is not None:
        dates = dates.dt.tz_convert(_TZ).dt.tz_localize(None)
    df["date"] = dates.dt.date
    if "adj_close" not in df:
        df["adj_close"] = float("nan")

    now = now or dt.datetime.now(_TZ)
    if now.hour < _DAY_DONE:
        df = df[df["date"] < now.date()]

    acts = []
    for col, name in (("splits", "split"), ("dividends", "dividend")):
        if col in df:
            sub = df[df[col].fillna(0) != 0]
            acts += [{"date": d, "action": name, "value": float(v)} for d, v in zip(sub["date"], sub[col])]
    actions = pd.DataFrame(acts, columns=["date", "action", "value"])
    from src.collect.tiingo_prices import filler_rows       # same rule for both sources
    df = df[~filler_rows(df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)).values]

    prices = df.dropna(subset=["close"])[PRICE_COLS].copy()
    prices["volume"] = prices["volume"].fillna(0).astype("int64")
    return prices, actions, str(raw_file.relative_to(RAW_DIR.parent.parent))
