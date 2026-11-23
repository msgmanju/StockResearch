"""
Market cap for the screener (sorting / display only - never used by a strategy or the backtest).

Source: Yahoo's summary values (yfinance Ticker.info), saved untouched under data/raw/yahoo/info/
(the same files the fundamentals audit saves). Why Yahoo and not SEC: SEC share counts don't give
one clean total for several share classes (GOOG/GOOGL, META, V, BRK-B) and are in ordinary shares
for ADRs (PDD: 1 ADS = 4 ordinary shares), so price x SEC shares is wrong for those.

    shares = Yahoo marketCap / Yahoo price at the time of the download   (today's share basis)
    market cap on a day = shares x that day's close (split-adjusted, so the same basis)
                          x splits after the download (if a split happened since)

For a past date this uses the latest share count, so it is approximate (buybacks/issuance move it a
few % a year) - fine for sorting by size.

Table market_cap in technicals.duckdb: one row per ticker from its newest saved file.
"""
import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor

from src.common import RAW_DIR, run

INFO_DIR = RAW_DIR / "yahoo" / "info"
NAME = re.compile(r"^(?P<t>.+)_(?P<d>\d{8})_(?P<h>\d{6})\.json$")

TABLE = """
CREATE OR REPLACE TABLE market_cap (
    ticker VARCHAR PRIMARY KEY, fetched_on DATE, market_cap DOUBLE, price DOUBLE, shares DOUBLE,
    currency VARCHAR, raw_file VARCHAR
)"""


def latest_files(folder=None):
    """{ticker: (path, download datetime)} - the newest saved info file of each ticker."""
    folder = folder or INFO_DIR
    out = {}
    if not folder.exists():
        return out
    for p in folder.iterdir():
        m = NAME.match(p.name)
        if not m:
            continue
        when = dt.datetime.strptime(m["d"] + m["h"], "%Y%m%d%H%M%S")
        if m["t"] not in out or when > out[m["t"]][1]:
            out[m["t"]] = (p, when)
    return out


def parse(info):
    """(market cap, price, shares, currency) from one Yahoo info dict; shares None if unusable."""
    mc = info.get("marketCap")
    px = info.get("currentPrice") or info.get("regularMarketPrice")
    cur = info.get("currency")
    ok = isinstance(mc, (int, float)) and isinstance(px, (int, float)) and mc > 0 and px > 0 and cur == "USD"
    return (mc if ok else None), (px if ok else None), (mc / px if ok else None), cur


def fetch(tickers, fetch_one=None, workers=4, today=None):
    """Download Yahoo info for tickers without a file from today. Returns (fetched, failed list)."""
    from src.checks.fundamentals_audit import yahoo_info
    today = today or dt.date.today()
    have = latest_files()
    todo = [t for t in tickers if t not in have or have[t][1].date() < today]
    failed = []

    def one(t):
        try:
            info = yahoo_info(t, fetch_one)
            if parse(info)[2] is None:
                failed.append(f"{t}: no market cap/price in USD")
        except Exception as e:
            failed.append(f"{t}: {type(e).__name__}: {str(e)[:80]}")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, todo))
    return len(todo), sorted(failed)


def load_table(con, folder=None):
    """Rebuild table market_cap from the newest saved file per ticker. Returns the number of rows."""
    rows = []
    for t, (p, when) in sorted(latest_files(folder).items()):
        try:
            info = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        mc, px, sh, cur = parse(info)
        if sh is not None:
            rows.append((t, when.date(), float(mc), float(px), float(sh), cur, p.name))
    con.execute(TABLE)
    if rows:
        con.executemany("INSERT INTO market_cap VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def check_prices(con, tickers=None):
    """Yahoo's price at download vs our stored close of that day (or the session before): differences over
    10% are listed (a wrong share count would make the market cap wrong). Returns [(ticker, ours, yahoo)]."""
    rows = run(con, """
        SELECT m.ticker, m.price, (SELECT p.close FROM prices_daily p WHERE p.ticker = m.ticker
                                   AND p.date <= m.fetched_on ORDER BY p.date DESC LIMIT 1)
        FROM market_cap m""")
    out = []
    for t, y, ours in rows:
        if (tickers is None or t in tickers) and ours and abs(y / ours - 1) > 0.10:
            out.append((t, ours, y))
    return out
