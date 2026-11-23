"""
Download daily prices into data/technicals.duckdb.

    python scripts/update_prices.py            # tickers in tickers.txt
    python scripts/update_prices.py AAPL MSFT  # just these

Yahoo is the main source:
  First run for a ticker : full history from start_date (settings.toml).
  Later runs             : only the days since the last stored date, plus an
                           overlap to pick up corrections.
  New split or dividend  : the whole history is downloaded again, because the
                           source re-adjusts all older prices.
If Yahoo has no data (usually a delisted stock), Tiingo is used instead and
that ticker stays on Tiingo (always a full download - one request).
Corrections from config/price_corrections.toml (and the audit) are re-applied
after every download.
"""
import sys
import uuid
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import ROOT, START_DATE, OVERLAP_DAYS, TECH_DB, connect, run, read_tickers  # noqa: E402
from src.schema import create_technicals  # noqa: E402
from src.collect import yahoo_prices, tiingo_prices  # noqa: E402
from src import corrections  # noqa: E402


def _log(con, run_id, ticker, source, mode, start, prices, status, message, raw_file):
    first = prices["date"].min() if len(prices) else None
    last = prices["date"].max() if len(prices) else None
    run(con, """INSERT INTO load_log VALUES (?, current_timestamp, ?, 'prices', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [run_id, ticker, source, mode, start, first, last, len(prices), status, message[:500], raw_file])


def _new_actions(con, ticker, actions):
    if actions.empty:
        return actions
    known = {(d, a) for d, a in run(con, "SELECT date, action FROM corporate_actions WHERE ticker = ?", [ticker])}
    mask = [(d, a) not in known for d, a in zip(actions["date"], actions["action"])]
    return actions[mask]


SYMBOLS_FILE = ROOT / "config" / "price_symbols.toml"


def symbol_rules():
    """Per-stock price rules from config/price_symbols.toml:
    try = other symbols, source = "tiingo" (always use Tiingo), first_date = drop prices before it."""
    import tomllib
    if not SYMBOLS_FILE.exists():
        return {}
    with open(SYMBOLS_FILE, "rb") as f:
        return {i["ticker"].upper(): i for i in tomllib.load(f).get("symbol", [])}


def _store(con, ticker, source, prices, actions, replace_all):
    con.begin()
    try:
        if replace_all:
            run(con, "DELETE FROM prices_daily WHERE ticker = ?", [ticker])
            run(con, "DELETE FROM corporate_actions WHERE ticker = ?", [ticker])
        con.register("p_new", prices.assign(ticker=ticker))
        run(con, """INSERT OR REPLACE INTO prices_daily
                    SELECT ticker, date, open, high, low, close, adj_close, volume, ?, current_timestamp
                    FROM p_new""", [source])
        con.unregister("p_new")
        if not actions.empty:
            con.register("a_new", actions.assign(ticker=ticker))
            run(con, """INSERT OR REPLACE INTO corporate_actions
                        SELECT ticker, date, action, value, ?, current_timestamp FROM a_new""", [source])
            con.unregister("a_new")
        first = symbol_rules().get(ticker, {}).get("first_date")
        if first:          # e.g. thin over-the-counter prints before a stock's US listing
            run(con, "DELETE FROM prices_daily WHERE ticker = ? AND date < ?", [ticker, first])
        corrections.apply(con, ticker)
        con.commit()
    except Exception:
        con.rollback()
        raise


def update_ticker(con, ticker, run_id, fetch=yahoo_prices.fetch, fetch_tiingo=tiingo_prices.fetch):
    last, stored_source = run(con, "SELECT max(date), any_value(source) FROM prices_daily WHERE ticker = ?",
                              [ticker])[0]
    source = stored_source or yahoo_prices.SOURCE
    if symbol_rules().get(ticker, {}).get("source") == tiingo_prices.SOURCE:
        source = tiingo_prices.SOURCE              # config/price_symbols.toml: Yahoo's history is wrong
    mode = "backfill" if last is None else "incremental"
    start = START_DATE if last is None else last - dt.timedelta(days=OVERLAP_DAYS)

    try:
        if source == tiingo_prices.SOURCE:
            mode, start = ("backfill" if last is None else "full reload"), START_DATE
            prices, actions, raw_file = fetch_tiingo(ticker, start)
        else:
            prices, actions, raw_file = fetch(ticker, start)
            if mode == "incremental":
                fresh = _new_actions(con, ticker, actions)
                if not fresh.empty:
                    kinds = ", ".join(sorted(set(fresh["action"])))
                    mode, start = "full reload", START_DATE
                    print(f"  {ticker}: new {kinds} found - downloading full history again")
                    prices, actions, raw_file = fetch(ticker, start)
            if prices.empty and last is None:
                print(f"  {ticker}: no Yahoo data - trying Tiingo (delisted stocks are kept there)")
                source = tiingo_prices.SOURCE
                prices, actions, raw_file = fetch_tiingo(ticker, START_DATE)
    except Exception as e:
        _log(con, run_id, ticker, source, mode, start, [], "ERROR", f"{type(e).__name__}: {e}", None)
        print(f"  {ticker}: ERROR {type(e).__name__}: {e}")
        return "ERROR"

    if prices.empty:
        _log(con, run_id, ticker, source, mode, start, prices, "EMPTY", "no rows returned", raw_file)
        print(f"  {ticker}: no data returned from {start}")
        return "EMPTY"

    _store(con, ticker, source, prices, actions, replace_all=(mode == "full reload"))
    _log(con, run_id, ticker, source, mode, start, prices, "OK", "", raw_file)
    total = run(con, "SELECT count(*), min(date), max(date) FROM prices_daily WHERE ticker = ?", [ticker])[0]
    print(f"  {ticker}: {mode} from {source}, {len(prices)} rows downloaded; stored {total[0]:,} days "
          f"({total[1]} to {total[2]})")
    return "OK"


def main(args=None, fetch=yahoo_prices.fetch, db_path=TECH_DB, fetch_tiingo=tiingo_prices.fetch):
    tickers = read_tickers(sys.argv[1:] if args is None else args)
    run_id = uuid.uuid4().hex[:8]
    print(f"Run {run_id}: {len(tickers)} ticker(s) -> {db_path}")
    con = connect(db_path)
    create_technicals(con)
    n_manual = corrections.sync_manual(con)
    if n_manual:
        print(f"  {n_manual} manual correction(s) loaded from config/price_corrections.toml")
    results = {t: update_ticker(con, t, run_id, fetch, fetch_tiingo) for t in tickers}
    con.close()
    bad = [t for t, s in results.items() if s != "OK"]
    print(f"Done: {len(tickers) - len(bad)} OK" + (f", problems: {', '.join(bad)}" if bad else ""))
    return results


if __name__ == "__main__":
    main()
