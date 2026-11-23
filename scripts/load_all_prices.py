"""
Step 6: download prices for every stock that was in the S&P 500 or NASDAQ-100 since the
backtest start (exports/membership/all_members_since_backtest_start.txt), and check that
the prices really cover the days each stock was in the index.

    python scripts/load_all_prices.py              # all members (resumes where it stopped)
    python scripts/load_all_prices.py AAPL TWTR    # just these
    python scripts/load_all_prices.py --yahoo-only # phase 1 only (fast)
    python scripts/load_all_prices.py --retry      # also re-try stocks marked PARTIAL / NO DATA

Phase 1 - Yahoo for every stock not yet loaded (about 1-2 seconds each).
Phase 2 - Tiingo for stocks Yahoo can't cover (mostly delisted ones). Tiingo's free plan allows
          50 requests an hour, so this phase waits between batches; it is safe to stop (Ctrl+C)
          and run again later - finished stocks are skipped.

Coverage rule (docs/AUDIT_PROCEDURE.md, step 6):
  member days = trading days the stock was in either index, from the backtest start to today.
  OK        - prices on at least 99% of member days
  PARTIAL   - some member days have prices, some don't (REVIEW: often a ticker that now belongs
              to a different company, or a merger where the index kept one ticker)
  NO DATA   - no source has prices for its member days (stored rows, if any, are removed so a
              different company's prices can never be used by mistake)
  Also noted: LATE START - prices begin less than a year before the stock joined, so its
  52-week values are not available for its first months in the index (normal for IPOs/spin-offs).
"""
import sys
import time
import uuid
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import EXPORTS_DIR, LOGS_DIR, ROOT, SETTINGS, START_DATE, TECH_DB, connect, run  # noqa: E402
from src.schema import create_technicals  # noqa: E402
from src.collect import yahoo_prices, tiingo_prices, second_sources  # noqa: E402
from src import membership as mb  # noqa: E402
from src import corrections  # noqa: E402
import scripts.update_prices as up  # noqa: E402

MEMBERS_FILE = EXPORTS_DIR / "membership" / "all_members_since_backtest_start.txt"
SYMBOLS_FILE = ROOT / "config" / "price_symbols.toml"


def load_symbols():
    """{ticker: [other symbols to try]} from config/price_symbols.toml"""
    import tomllib
    if not SYMBOLS_FILE.exists():
        return {}
    with open(SYMBOLS_FILE, "rb") as f:
        return {i["ticker"].upper(): [x.upper() for x in i.get("try", [])] for i in tomllib.load(f).get("symbol", [])}
OK_SHARE = 0.99
MAX_MISSING_DAYS = 2      # a short membership can still be OK with 1-2 missing days (e.g. a half-day session)
YAHOO_PAUSE = 0.5          # seconds between Yahoo requests
# Most stocks sent to Tiingo in one run (free plan: 500 different tickers a month; Power: no monthly cap)
TIINGO_MAX = int((SETTINGS.get("tiingo") or {}).get("max_tickers_per_run", 300))

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_coverage (
    ticker VARCHAR PRIMARY KEY, status VARCHAR, source VARCHAR,
    member_first DATE, member_last DATE, member_days INTEGER, days_with_price INTEGER,
    coverage DOUBLE, price_first DATE, price_last DATE, first_missing DATE, late_start BOOLEAN,
    note VARCHAR, checked_at TIMESTAMP
);
"""
EMPTY_ACTIONS = pd.DataFrame(columns=["date", "action", "value"])


def member_days(con, today):
    """{ticker: sorted list of sessions it was in either index, from the backtest start}"""
    cal = mb.sessions(mb.BACKTEST_START, today)
    rows = run(con, "SELECT ticker, start_date, end_date FROM index_membership")
    out = {}
    for t, s, e in rows:
        e = e or today
        out.setdefault(t, set()).update(d for d in cal if s <= d <= e)
    return {t: sorted(v) for t, v in out.items() if v}


def coverage(dates_with_price, days):
    """(share, n_with, first_missing) of member days that have a price."""
    have = set(dates_with_price)
    hit = [d for d in days if d in have]
    miss = [d for d in days if d not in have]
    return (len(hit) / len(days) if days else 0.0), len(hit), (miss[0] if miss else None)


def remove_filler_rows(con):
    """Delete stored rows that are fillers, not trades (see tiingo_prices.filler_rows) - any source."""
    df = con.execute("""SELECT ticker, date, open, high, low, close, volume FROM prices_daily
                        ORDER BY ticker, date""").df()
    if df.empty:
        return 0
    src = dict(run(con, "SELECT ticker, any_value(source) FROM prices_daily GROUP BY 1"))
    bad = []
    for t, g in df.groupby("ticker"):
        g = g.copy().reset_index(drop=True)
        g["date"] = pd.to_datetime(g["date"]).dt.date
        m = tiingo_prices.filler_rows(g)
        if src.get(t) != tiingo_prices.SOURCE and m.sum() < 20:
            # Yahoo: keep isolated bad bars (1-2 in a row) - the audit replaces them with the second
            # source's bar; remove only runs of 3+ (placeholder history before a US listing)
            run_id = (m != m.shift()).cumsum()
            run_len = m.groupby(run_id).transform("sum")
            m = m & (run_len >= 3)
        bad += [(t, d) for d in g.loc[m, "date"]]
    for t, d in bad:
        run(con, "DELETE FROM prices_daily WHERE ticker = ? AND date = ?", [t, d])
    return len(bad)


def assess(con, ticker, days, source_note=""):
    px = run(con, "SELECT date FROM prices_daily WHERE ticker = ? ORDER BY date", [ticker])
    dates = [r[0] for r in px]
    src = run(con, "SELECT any_value(source) FROM prices_daily WHERE ticker = ?", [ticker])[0][0]
    share, n_with, first_missing = coverage(dates, days)
    missing = len(days) - n_with
    status = "OK" if (share >= OK_SHARE or (n_with and missing <= MAX_MISSING_DAYS)) else ("PARTIAL" if n_with else "NO DATA")
    late = bool(dates) and dates[0] > max(START_DATE, days[0] - dt.timedelta(days=365))
    note = source_note
    if status == "OK" and 0 < missing <= MAX_MISSING_DAYS:
        have = set(dates)
        gone = [str(d) for d in days if d not in have]
        note = (note + "; " if note else "") + f"missing {', '.join(gone)} (checked again in the price audit)"
    if status == "PARTIAL" and dates and dates[-1] < days[-1]:
        # stopped trading (taken over / delisted) a few days before the index removed it
        before = [d for d in days if d <= dates[-1]]
        after = [d for d in days if d > dates[-1]]
        if before and coverage(dates, before)[0] >= OK_SHARE and len(after) <= 10:
            status = "OK"
            note = (note + "; " if note else "") + (f"stopped trading {dates[-1]}, {len(after)} trading day(s) "
                                                   f"before leaving the index - normal for a takeover")
    if status == "PARTIAL" and dates and first_missing is not None:
        note = (note + "; " if note else "") + (
            f"prices {dates[0]} to {dates[-1]} but member {days[0]} to {days[-1]}")
    return {"ticker": ticker, "status": status, "source": src, "member_first": days[0],
            "member_last": days[-1], "member_days": len(days), "days_with_price": n_with,
            "coverage": round(share, 4), "price_first": dates[0] if dates else None,
            "price_last": dates[-1] if dates else None, "first_missing": first_missing,
            "late_start": late, "note": note}


def save(con, row):
    run(con, "DELETE FROM price_coverage WHERE ticker = ?", [row["ticker"]])
    run(con, f"INSERT INTO price_coverage VALUES ({','.join('?' * 13)}, current_timestamp)",
        [row[k] for k in ["ticker", "status", "source", "member_first", "member_last", "member_days",
                          "days_with_price", "coverage", "price_first", "price_last", "first_missing",
                          "late_start", "note"]])


def _yahoo_with_retry(con, ticker, run_id, fetch):
    no_tiingo = lambda *a, **k: (pd.DataFrame(columns=yahoo_prices.PRICE_COLS), EMPTY_ACTIONS, None)  # noqa: E731
    for attempt in range(3):
        res = up.update_ticker(con, ticker, run_id, fetch, no_tiingo)
        last_err = run(con, """SELECT message FROM load_log WHERE run_id = ? AND ticker = ?
                               ORDER BY run_at DESC LIMIT 1""", [run_id, ticker])
        msg = last_err[0][0] if last_err else ""
        if res == "ERROR" and "rate" in (msg or "").lower():
            print(f"    Yahoo is limiting requests - waiting {60 * (attempt + 1)} s")
            time.sleep(60 * (attempt + 1))
            continue
        return res
    return "ERROR"


def try_tiingo(con, ticker, days, run_id, fetch_tiingo, symbol=None, source="tiingo"):
    """Download (Tiingo by default); keep it only if it covers more member days than what is stored.
    symbol: another symbol to download for this ticker (e.g. the OTC symbol after a bankruptcy)."""
    sym = symbol or ticker
    try:
        prices, actions, raw_file = fetch_tiingo(sym, START_DATE)
    except ConnectionError as e:                      # Tiingo usage limit (HTTP 429): not the stock's fault
        return f"LIMIT: {e}"
    except Exception as e:
        up._log(con, run_id, ticker, source, "coverage", START_DATE, [], "ERROR", f"{type(e).__name__}: {e}", None)
        return f"{source.title()} {sym}: {type(e).__name__}: {e}"
    if prices.empty:
        up._log(con, run_id, ticker, source, "coverage", START_DATE, prices, "EMPTY", "no rows", raw_file)
        return f"{source.title()} has no data for {sym}"
    new_share = coverage(prices["date"], days)[0]
    old = run(con, "SELECT date FROM prices_daily WHERE ticker = ?", [ticker])
    old_share = coverage([r[0] for r in old], days)[0]
    label = source.title() + ("" if sym == ticker else f" {sym}")
    if new_share <= old_share:
        return f"{label} covers {new_share:.0%} of member days (stored {old_share:.0%}) - kept stored"
    up._store(con, ticker, source, prices, actions, replace_all=True)
    up._log(con, run_id, ticker, source, "coverage", START_DATE, prices, "OK",
            "replaced (better coverage)" + ("" if sym == ticker else f" from symbol {sym}"), raw_file)
    return f"{label} used (covers {new_share:.0%} of member days; before {old_share:.0%})"


def apply_symbol_rules(con, tickers, run_id, fetch_tiingo):
    """config/price_symbols.toml: switch a stock to Tiingo, or drop prices before its first_date."""
    rules = up.symbol_rules()
    for t in tickers:
        r = rules.get(t) or {}
        if r.get("source") == tiingo_prices.SOURCE:
            cur = run(con, "SELECT any_value(source) FROM prices_daily WHERE ticker = ?", [t])[0][0]
            if cur != tiingo_prices.SOURCE:
                try:
                    prices, actions, raw_file = fetch_tiingo(t, START_DATE)
                except Exception as e:
                    print(f"  {t}: could not switch to Tiingo - {type(e).__name__}: {e}")
                    continue
                if prices.empty:
                    print(f"  {t}: Tiingo returned no data - kept {cur}")
                    continue
                up._store(con, t, tiingo_prices.SOURCE, prices, actions, replace_all=True)
                up._log(con, run_id, t, tiingo_prices.SOURCE, "full reload", START_DATE, prices, "OK",
                        "switched to Tiingo (config/price_symbols.toml)", raw_file)
                print(f"  {t}: switched to Tiingo ({len(prices):,} days) - {r.get('note', '')}")
        first = r.get("first_date")
        if first:
            n = run(con, "SELECT count(*) FROM prices_daily WHERE ticker = ? AND date < ?", [t, first])[0][0]
            if n:
                run(con, "DELETE FROM prices_daily WHERE ticker = ? AND date < ?", [t, first])
                print(f"  {t}: removed {n:,} row(s) before {first} - {r.get('note', '')}")


def _phase1(con, todo, run_id, fetch, pause, days_of, need_tiingo):
    for i, t in enumerate(todo, 1):
        stored = run(con, "SELECT count(*), any_value(source) FROM prices_daily WHERE ticker = ?", [t])[0]
        if stored[0] == 0 or stored[1] == yahoo_prices.SOURCE:
            print(f"[{i}/{len(todo)}] {t}")
            _yahoo_with_retry(con, t, run_id, fetch)
            time.sleep(pause)
        row = assess(con, t, days_of[t])
        if row["status"] != "OK":
            need_tiingo.append(t)
            row["note"] = "waiting for Tiingo"
        save(con, row)


def main(args=None, today=None, fetch=yahoo_prices.fetch, fetch_tiingo=tiingo_prices.fetch,
         db_path=TECH_DB, pause=YAHOO_PAUSE):
    args = sys.argv[1:] if args is None else list(args)
    yahoo_only, retry = "--yahoo-only" in args, "--retry" in args
    picked = [a.upper() for a in args if not a.startswith("--")]
    today = today or dt.date.today()
    run_id = uuid.uuid4().hex[:8]

    con = connect(db_path)
    create_technicals(con)
    con.execute(SCHEMA).fetchall()
    corrections.sync_manual(con)
    days_of = member_days(con, today)
    if picked:
        tickers = picked
    elif MEMBERS_FILE.exists():
        tickers = [t.strip().upper() for t in MEMBERS_FILE.read_text().split() if t.strip()]
    else:
        tickers = sorted(days_of)
    unknown = [t for t in tickers if t not in days_of]
    tickers = [t for t in tickers if t in days_of]
    if unknown:
        print(f"Not in the membership table (skipped): {', '.join(unknown)}")

    n_fill = remove_filler_rows(con)
    if n_fill:
        print(f"Removed {n_fill} filler row(s) (unchanged price, no or almost no volume - no real trade that day)")
    apply_symbol_rules(con, tickers, run_id, fetch_tiingo)
    # membership may have changed (renames, splits): re-check coverage of everything already stored
    run(con, f"DELETE FROM price_coverage WHERE ticker NOT IN ({','.join('?' * len(tickers))})", tickers)
    stored = {r[0] for r in run(con, "SELECT DISTINCT ticker FROM prices_daily")}
    prev = {t: (s, n or "") for t, s, n in run(con, "SELECT ticker, status, note FROM price_coverage")}
    for t in tickers:
        if t in stored and prev.get(t, ("", ""))[1] != "waiting for Tiingo":
            row = assess(con, t, days_of[t])
            if row["status"] != "OK" and prev.get(t):          # keep what was tried before
                row["note"] = prev[t][1]
            save(con, row)
    done = {t: (s, n or "") for t, s, n in run(con, "SELECT ticker, status, note FROM price_coverage")}

    def unfinished(t):
        if picked or t not in done:
            return True
        status, note = done[t]
        return status != "OK" and (retry or note == "waiting for Tiingo")
    todo = [t for t in tickers if unfinished(t)]
    print(f"Run {run_id}: {len(tickers)} stocks; already finished {len(tickers) - len(todo)}; to do {len(todo)}")

    # ---- phase 1: Yahoo
    need_tiingo = [t for t, (st, n) in done.items() if n == "waiting for Tiingo" and t in todo]
    todo_yahoo = [t for t in todo if t not in need_tiingo]
    try:
        _phase1(con, todo_yahoo, run_id, fetch, pause, days_of, need_tiingo)
    except KeyboardInterrupt:
        print("\nStopped. Everything finished so far is saved - run the same command again to continue.")
        con.close()
        return

    # ---- phase 2: Tiingo (and other symbols from config/price_symbols.toml) for what Yahoo couldn't cover
    symbols = load_symbols()
    if need_tiingo and not yahoo_only:
        if len(need_tiingo) > TIINGO_MAX:
            print(f"{len(need_tiingo)} stocks need Tiingo; doing {TIINGO_MAX} now (monthly limit), rest next run")
        batch = need_tiingo[:TIINGO_MAX]
        per_hour = second_sources.TIINGO_PER_HOUR
        hrs = max(0, len(batch) - per_hour) / per_hour
        print(f"\nTiingo phase: {len(batch)} stocks (about {hrs:.1f} h of waiting for the hourly limit; "
              f"safe to stop with Ctrl+C and run again)")
        limited = False
        try:
            for i, t in enumerate(batch, 1):
                print(f"[T {i}/{len(batch)}] {t}")
                note = try_tiingo(con, t, days_of[t], run_id, fetch_tiingo)
                if note.startswith("LIMIT"):
                    if limited:
                        print("Tiingo is still refusing (daily or monthly limit) - run again tomorrow to continue.")
                        break
                    limited = True
                    print("    Tiingo usage limit - waiting 60 min, then trying again (Ctrl+C is safe)")
                    time.sleep(3600)
                    note = try_tiingo(con, t, days_of[t], run_id, fetch_tiingo)
                    if note.startswith("LIMIT"):
                        print("Tiingo is still refusing (daily or monthly limit) - run again tomorrow to continue.")
                        break
                limited = False
                print(f"    {note}")
                row = assess(con, t, days_of[t], note)
                for sym in symbols.get(t, []):
                    if row["status"] == "OK":
                        break
                    for src, fn in (("tiingo", fetch_tiingo), ("yahoo", fetch)):
                        n2 = try_tiingo(con, t, days_of[t], run_id, fn, symbol=sym, source=src)
                        print(f"    {n2}")
                        if n2.startswith("LIMIT"):
                            continue
                        note = note + "; " + n2
                        row = assess(con, t, days_of[t], note)
                        if row["status"] == "OK":
                            break
                if row["status"] == "NO DATA" and row["price_first"] is not None:
                    run(con, "DELETE FROM prices_daily WHERE ticker = ?", [t])     # someone else's prices
                    run(con, "DELETE FROM corporate_actions WHERE ticker = ?", [t])
                    row.update(price_first=None, price_last=None, source=None,
                               note=row["note"] + "; stored prices were for other dates only - removed")
                save(con, row)
        except KeyboardInterrupt:
            print("\nStopped. Everything finished so far is saved - run the same command again to continue.")

    report(con, run_id)
    con.close()


def report(con, run_id):
    df = con.execute("SELECT * FROM price_coverage ORDER BY status, ticker").df()
    counts = df["status"].value_counts().to_dict()
    late = int(df["late_start"].fillna(False).sum())
    src = df["source"].fillna("-").value_counts().to_dict()
    lines = [f"PRICE COVERAGE  run {run_id} at {dt.datetime.now():%Y-%m-%d %H:%M}",
             f"stocks: {len(df)}; " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
             "sources: " + ", ".join(f"{k} {v}" for k, v in sorted(src.items())),
             f"late start (under 1 year of prices before joining - normal for IPOs/spin-offs): {late}", ""]
    for status in ("PARTIAL", "NO DATA"):
        sub = df[df["status"] == status]
        if len(sub):
            lines.append(f"{status} ({len(sub)}):")
            for _, r in sub.iterrows():
                lines.append(f"  {r['ticker']:7} member {str(r['member_first'])[:10]} to {str(r['member_last'])[:10]}, "
                             f"coverage {r['coverage']:.0%}  {r['note'] or ''}")
            lines.append("")
    text = "\n".join(lines)
    print("\n" + text)
    (LOGS_DIR / "audit").mkdir(parents=True, exist_ok=True)
    log = LOGS_DIR / "audit" / f"price_coverage_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    log.write_text(text)
    out = EXPORTS_DIR / "prices" / "price_coverage.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.drop(columns=["checked_at"]).to_excel(w, index=False, sheet_name="coverage")
        ws = w.sheets["coverage"]
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 14
            for c in col[1:]:
                if isinstance(c.value, (dt.date, dt.datetime)):
                    c.number_format = "yyyy-mm-dd"
        ws.column_dimensions["M"].width = 70
        ws.freeze_panes = "B2"
    print(f"saved: {log.relative_to(LOGS_DIR.parent)}, {out.relative_to(EXPORTS_DIR.parent)}")


if __name__ == "__main__":
    main()
