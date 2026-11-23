"""
Independent daily prices used only to audit Yahoo: Tiingo (full history),
Nasdaq.com (last ~1 year) and Stooq (currently blocks automated downloads).
Each fetch returns a DataFrame (date, open, high, low, close, volume) and saves
the untouched response under data/raw/<source>/prices/<TICKER>/.
"""
import datetime as dt
import io
import json
import time

import pandas as pd
import requests

from src.common import RAW_DIR, SECRETS, SETTINGS

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
COLS = ["date", "open", "high", "low", "close", "volume"]


def _save_raw(source, ticker, text, ext):
    folder = RAW_DIR / source / "prices" / ticker
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{ticker}_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.{ext}"
    f.write_text(text)
    return f


def _num(x):
    if x is None:
        return float("nan")
    s = str(x).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return float("nan")


# ---------------------------------------------------------------- Nasdaq.com
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{sym}/historical"


def nasdaq(ticker: str, start: dt.date, end: dt.date, http=requests.get) -> pd.DataFrame:
    sym = ticker.replace("-", ".")            # BRK-B -> BRK.B
    headers = dict(HEADERS, Origin="https://www.nasdaq.com", Referer="https://www.nasdaq.com/")
    rows = []
    year_start = max(start, dt.date(end.year - 1, 1, 1))     # Nasdaq serves ~1 year only
    while year_start <= end:                  # one request per calendar year
        year_end = min(dt.date(year_start.year, 12, 31), end)
        r = http(NASDAQ_URL.format(sym=sym), headers=headers, timeout=30,
                 params={"assetclass": "stocks", "fromdate": year_start.isoformat(),
                         "todate": year_end.isoformat(), "limit": 9999})
        r.raise_for_status()
        _save_raw("nasdaq", ticker, r.text, "json")
        data = (r.json() or {}).get("data") or {}
        rows += ((data.get("tradesTable") or {}).get("rows")) or []
        year_start = year_end + dt.timedelta(days=1)
    if not rows:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], format="%m/%d/%Y").dt.date,
        "open": df["open"].map(_num), "high": df["high"].map(_num),
        "low": df["low"].map(_num), "close": df["close"].map(_num),
        "volume": df["volume"].map(_num),
    })
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- Stooq
STOOQ_URL = "https://stooq.com/q/d/l/"


def stooq(ticker: str, start: dt.date, end: dt.date, http=requests.get) -> pd.DataFrame:
    sym = ticker.lower().replace(".", "-") + ".us"
    r = http(STOOQ_URL, headers=HEADERS, timeout=30,
             params={"s": sym, "d1": f"{start:%Y%m%d}", "d2": f"{end:%Y%m%d}", "i": "d"})
    r.raise_for_status()
    _save_raw("stooq", ticker, r.text, "csv")
    if not r.text.lstrip().lower().startswith("date"):
        raise ValueError(f"Stooq did not return CSV data: {r.text[:80]!r}")
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip().lower() for c in df.columns]
    if "volume" not in df:
        df["volume"] = float("nan")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[COLS].sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- Tiingo
# Free plan: 50 requests/hour, 1,000/day, 500 different tickers/month.
# close/open/high/low are the ACTUAL traded prices (not split-adjusted);
# divCash and splitFactor give the dividends and splits on each date.
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{sym}/prices"
# Your plan's limit, from config/settings.toml [tiingo] requests_per_hour (free plan: 50, Power: 10,000).
TIINGO_PER_HOUR = int((SETTINGS.get("tiingo") or {}).get("requests_per_hour", 50))
# Request times are kept in a file, so a script that is stopped and started again still knows how
# many requests Tiingo has counted in the last hour.
TIINGO_CALLS_FILE = RAW_DIR / "tiingo" / "request_times.json"


def _load_calls():
    try:
        return [float(t) for t in json.loads(TIINGO_CALLS_FILE.read_text())]
    except Exception:
        return []


def _save_calls(calls):
    try:
        TIINGO_CALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TIINGO_CALLS_FILE.write_text(json.dumps(calls))
    except Exception:
        pass


def _tiingo_wait():
    now = time.time()
    calls = [t for t in _load_calls() if now - t < 3600]
    if len(calls) >= TIINGO_PER_HOUR:
        wait = 3600 - (now - min(calls)) + 1
        resume = dt.datetime.now() + dt.timedelta(seconds=wait)
        print(f"    Tiingo hourly limit reached - waiting {wait / 60:.0f} min (continues at {resume:%H:%M}; "
              f"Ctrl+C is safe)")
        time.sleep(wait)
        now = time.time()
        calls = [t for t in calls if now - t < 3600]
    calls.append(time.time())
    _save_calls(calls)


def tiingo_raw(ticker: str, start: dt.date, end: dt.date, http=requests.get):
    """Tiingo's daily rows as returned (list of dicts), plus the saved raw file path."""
    token = (SECRETS.get("tiingo") or {}).get("api_key", "")
    if not token:
        raise ValueError("no Tiingo API key in config/secrets.toml")
    _tiingo_wait()
    r = http(TIINGO_URL.format(sym=ticker.lower()), timeout=60,
             headers={"Content-Type": "application/json", "Authorization": f"Token {token}"},
             params={"startDate": start.isoformat(), "endDate": end.isoformat(), "format": "json"})
    if r.status_code in (401, 403):
        raise PermissionError(f"Tiingo refused the API key (HTTP {r.status_code})")
    if r.status_code == 429:
        raise ConnectionError("Tiingo usage limit reached (HTTP 429) - try again later")
    r.raise_for_status()
    raw_file = _save_raw("tiingo", ticker, r.text, "json")
    data = r.json()
    if isinstance(data, dict):                       # error message instead of a list
        raise ValueError(f"Tiingo: {data.get('detail') or data}")
    return data, raw_file


def tiingo(ticker: str, start: dt.date, end: dt.date, http=requests.get) -> pd.DataFrame:
    """Tiingo prices for comparison: actual traded prices plus dividends and splits."""
    data, _ = tiingo_raw(ticker, start, end, http=http)
    if not data:
        return pd.DataFrame(columns=COLS + ["div_cash", "split_factor"])
    df = pd.DataFrame(data)
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], utc=True).dt.date,
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "volume": df["volume"].astype(float),
        "div_cash": df.get("divCash", 0.0), "split_factor": df.get("splitFactor", 1.0),
    })
    return out.sort_values("date").reset_index(drop=True)


SOURCES = {"tiingo": tiingo, "nasdaq": nasdaq, "stooq": stooq}
