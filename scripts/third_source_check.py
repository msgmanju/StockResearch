"""
Settle Yahoo-vs-Tiingo disagreements with a third source (Nasdaq.com, then Stooq).

    python scripts/third_source_check.py            # all open close / high-low disputes from the last audit
    python scripts/third_source_check.py BIDU WAB   # just these stocks
    python scripts/third_source_check.py --dry-run  # show the verdicts, change nothing

For each disputed day the three sources are compared on numbers that do not depend on
split/dividend adjustment:
    close     : that day's close / the previous day's close (the day's return)
    high, low : that day's high (or low) / that day's close
Majority rule:
    third source agrees with OURS   -> recorded as reviewed in config/audit_reviewed.toml
    third source agrees with TIINGO -> our value is corrected to Tiingo's (automatic correction,
                                       recorded in the price_corrections table; the raw files are untouched)
    agrees with neither / no data   -> left open for review
Every Nasdaq/Stooq reply is saved under data/raw/<source>/prices/<TICKER>/.
Afterwards run:  python scripts/audit_prices.py <the tickers listed at the end>
"""
import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import ROOT, TECH_DB, connect, run  # noqa: E402
from src import corrections  # noqa: E402
from src.collect import second_sources  # noqa: E402
from src.checks import price_audit as pa  # noqa: E402
import scripts.update_prices as up  # noqa: E402

TOLERANCE = 0.0025          # 0.25 percentage points on the return or the high/low-to-close ratio
WINDOW_DAYS = 10            # calendar days fetched on each side of the disputed day
PAUSE = 1.0                 # seconds between requests to the third source
REVIEWED_FILE = ROOT / "config" / "audit_reviewed.toml"
THIRD = (("nasdaq", second_sources.nasdaq), ("stooq", second_sources.stooq))


def open_disputes(con, picked):
    """Open close / high-low items from each stock's latest audit."""
    rows = run(con, """
        SELECT i.ticker, i.check_name, i.date, i.field, i.our_value, i.other_value
        FROM audit_issues i JOIN stock_status s ON s.ticker = i.ticker AND s.run_id = i.run_id
        WHERE i.check_name IN ('compare_tiingo', 'high_low')
          AND i.field IN ('close', 'high', 'low')
          AND (i.note LIKE '[FAIL]%' OR i.note LIKE '[REVIEW]%')
        ORDER BY 1, 3""")
    skip = {t for t, r in up.symbol_rules().items() if r.get("source") == "tiingo"}   # re-sourced anyway
    return [r for r in rows if r[0] not in skip and (not picked or r[0] in picked)]


def our_bars(con, ticker, day):
    df = con.execute("""SELECT date, high, low, close FROM prices_daily
                        WHERE ticker = ? AND date BETWEEN ? AND ? ORDER BY date""",
                     [ticker, day - dt.timedelta(days=WINDOW_DAYS), day]).df()
    df["date"] = df["date"].map(lambda d: d.date() if hasattr(d, "date") else d)
    return df


def third_bars(ticker, day, cache):
    """(source name, bars around the day) from the first third source that has the day."""
    errors = []
    for name, fetch in THIRD:
        key = (name, ticker, day)
        if key not in cache:
            try:
                cache[key] = fetch(ticker, day - dt.timedelta(days=WINDOW_DAYS), day + dt.timedelta(days=3))
            except Exception as e:
                cache[key] = f"{type(e).__name__}: {e}"
            time.sleep(PAUSE)
        df = cache[key]
        if isinstance(df, str):
            errors.append(f"{name}: {df[:80]}")
            continue
        if not df.empty and (df["date"] == day).any():
            return name, df.sort_values("date").reset_index(drop=True), ""
        errors.append(f"{name}: no row for {day}")
    return None, None, "; ".join(errors)


def measure(bars, day, field):
    """The adjustment-free number used to compare sources (None if not available)."""
    b = bars[bars["date"] <= day]
    if b.empty or b.iloc[-1]["date"] != day:
        return None
    cur = b.iloc[-1]
    if field == "close":
        if len(b) < 2 or not b.iloc[-2]["close"]:
            return None
        return float(cur["close"]) / float(b.iloc[-2]["close"]) - 1
    return float(cur[field]) / float(cur["close"]) if cur["close"] else None


def verdict(ours_m, tiingo_m, third_m):
    if third_m is None or ours_m is None or tiingo_m is None:
        return "no data"
    d_ours, d_tiingo = abs(third_m - ours_m), abs(third_m - tiingo_m)
    if d_ours <= TOLERANCE and d_ours < d_tiingo:
        return "ours"
    if d_tiingo <= TOLERANCE and d_tiingo < d_ours:
        return "tiingo"
    return "neither"


def add_reviewed(ticker, day, check, note):
    with open(REVIEWED_FILE, "a") as f:
        f.write(f'\n[[reviewed]]\nticker = "{ticker}"\ndate = {day.isoformat()}\ncheck = "{check}"\n'
                f'note = "{note}"\n')


def main(args=None, db_path=TECH_DB):
    args = sys.argv[1:] if args is None else list(args)
    dry = "--dry-run" in args
    picked = {a.upper() for a in args if not a.startswith("--")}
    con = connect(db_path)
    disputes = open_disputes(con, picked)
    if not disputes:
        print("No open close or high/low disputes in the latest audit.")
        return
    print(f"{len(disputes)} disputed value(s) in {len({d[0] for d in disputes})} stock(s)"
          + (" - DRY RUN, nothing is changed" if dry else ""))
    already = pa.load_reviewed()
    cache, touched, tally = {}, set(), {}
    for ticker, check, day, field, ours_v, tiingo_v in disputes:
        if isinstance(day, dt.datetime):
            day = day.date()
        ours = our_bars(con, ticker, day)
        ours_m = measure(ours, day, field)
        if field == "close":
            prev = ours[ours["date"] < day]
            tiingo_m = (tiingo_v / float(prev.iloc[-1]["close"]) - 1) if not prev.empty else None
        else:
            row = ours[ours["date"] == day]
            tiingo_m = tiingo_v / float(row.iloc[0]["close"]) if not row.empty else None
        name, third, why = third_bars(ticker, day, cache)
        third_m = measure(third, day, field) if third is not None else None
        v = verdict(ours_m, tiingo_m, third_m)
        tally[v] = tally.get(v, 0) + 1

        def pct(x):
            return "   n/a" if x is None else (f"{x:+.2%}" if field == "close" else f"{x:.4f}")
        label = "return" if field == "close" else f"{field}/close"
        print(f"  {ticker:6} {day} {field:5} {label:10} ours {pct(ours_m)}  tiingo {pct(tiingo_m)}  "
              f"{name or 'third'} {pct(third_m)}  -> ", end="")
        if v == "ours":
            print("OURS confirmed")
            if not dry and (ticker, day, check) not in already:
                add_reviewed(ticker, day, check,
                             f"third source {name} agrees with ours ({field} ours {ours_v:.4f}, tiingo {tiingo_v:.4f})")
                touched.add(ticker)
        elif v == "tiingo":
            print("TIINGO confirmed - ours corrected")
            if not dry:
                corrections.add_auto(con, ticker, day, field, float(tiingo_v), "tiingo",
                                     f"auto: third source {name} agrees with tiingo (ours was {ours_v:.4f})")
                if field == "close":                  # keep the bar consistent
                    row = ours[ours["date"] == day].iloc[0]
                    if tiingo_v > row["high"]:
                        corrections.add_auto(con, ticker, day, "high", float(tiingo_v), "tiingo",
                                             "auto: raised to the corrected close")
                    if tiingo_v < row["low"]:
                        corrections.add_auto(con, ticker, day, "low", float(tiingo_v), "tiingo",
                                             "auto: lowered to the corrected close")
                corrections.apply(con, ticker)
                touched.add(ticker)
        elif v == "neither":
            print("no majority - left for review")
        else:
            print(f"no third-source data ({why or 'missing neighbour day'}) - left for review")
    con.close()
    print("\n" + ", ".join(f"{k}: {n}" for k, n in sorted(tally.items())))
    if touched and not dry:
        print("Re-audit the changed stocks:\n  python scripts/audit_prices.py " + " ".join(sorted(touched)))


if __name__ == "__main__":
    main()
