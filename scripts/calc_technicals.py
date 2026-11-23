"""
Calculate the daily 52-week / 6-month values and support/resistance, check them,
and export to Excel.

    python scripts/calc_technicals.py          # every ticker in tickers.txt
    python scripts/calc_technicals.py AAPL     # just these
    python scripts/calc_technicals.py --members           # every index member since the backtest start
    python scripts/calc_technicals.py --members --export  # ... and one Excel file per stock

Writes data/technicals.duckdb -> technicals_daily, swing_points, sr_daily
and exports/technicals/<TICKER>.xlsx,
and prints the latest values and v3 buy price for comparing with thinkorswim.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import EXPORTS_DIR, TECH_DB, connect, read_tickers  # noqa: E402
from src.calculate import technicals, support_resistance  # noqa: E402
from src.checks import technicals_audit  # noqa: E402

BUY_LOW_WEIGHT, BUY_AVG_WEIGHT = 0.70, 0.30      # your v3 buy price (strategy settings come later)


def next_session_values(con, ticker):
    """Values for the NEXT trading day: the latest 252 stored days, including the last one."""
    p = con.execute("""SELECT low, high, close FROM prices_daily WHERE ticker = ?
                       ORDER BY date DESC LIMIT ?""", [ticker, technicals.LOOKBACK_52W]).df()
    if len(p) < technicals.LOOKBACK_52W:
        return None
    return {"low_52w": p["low"].min(), "high_52w": p["high"].max(), "avg_close_52w": p["close"].mean(),
            "low_6m": p["low"].head(technicals.LOOKBACK_6M).min()}


def show(label, v):
    buy = BUY_LOW_WEIGHT * v["low_52w"] + BUY_AVG_WEIGHT * v["avg_close_52w"]
    print(f"  {label:<38} 52W low {v['low_52w']:>9.2f}  52W high {v['high_52w']:>9.2f}  "
          f"52W avg close {v['avg_close_52w']:>9.2f}  6M low {v['low_6m']:>9.2f}  "
          f"H/L {v['high_52w'] / v['low_52w']:.2f}  BUY PRICE {buy:.2f}")


def export(con, ticker, folder=EXPORTS_DIR / "technicals"):
    folder.mkdir(parents=True, exist_ok=True)
    df = con.execute(f"""
        SELECT t.date, t.close, t.low_52w, t.high_52w, t.avg_close_52w, t.low_6m,
               {BUY_LOW_WEIGHT} * t.low_52w + {BUY_AVG_WEIGHT} * t.avg_close_52w AS buy_price_v3,
               t.close / ({BUY_LOW_WEIGHT} * t.low_52w + {BUY_AVG_WEIGHT} * t.avg_close_52w) - 1 AS pct_from_buy,
               t.high_52w / t.low_52w AS hl_ratio,
               s.support, s.support_touches, s.ref_close / s.support - 1 AS pct_above_support,
               s.resistance, s.resistance_touches, s.resistance / s.ref_close - 1 AS pct_to_resistance,
               t.days_52w
        FROM technicals_daily t LEFT JOIN sr_daily s USING (ticker, date)
        WHERE t.ticker = ? ORDER BY t.date DESC""", [ticker]).df()
    with pd.ExcelWriter(folder / f"{ticker}.xlsx", engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="technicals", index=False)
        ws = xw.sheets["technicals"]
        ws.freeze_panes = "B2"
        for col in ws.columns:
            name = col[0].value
            ws.column_dimensions[col[0].column_letter].width = 14
            fmt = ("yyyy-mm-dd" if name == "date" else "0.0%" if str(name).startswith("pct_")
                   else "0" if name in ("days_52w", "support_touches", "resistance_touches") else "0.00")
            for c in col[1:]:
                c.number_format = fmt


def latest_row(con, ticker):
    r = con.execute(f"""
        SELECT t.date, t.close, t.low_52w, t.high_52w, t.avg_close_52w, t.low_6m,
               {BUY_LOW_WEIGHT} * t.low_52w + {BUY_AVG_WEIGHT} * t.avg_close_52w AS buy_price_v3,
               s.support, s.resistance, t.days_52w
        FROM technicals_daily t LEFT JOIN sr_daily s USING (ticker, date)
        WHERE t.ticker = ? ORDER BY t.date DESC LIMIT 1""", [ticker]).df()
    return r.iloc[0].to_dict() if len(r) else {}


def run_members(con, export_each=False):
    """Every index member: calculate, check, support/resistance; one line per stock and a summary file."""
    members_file = EXPORTS_DIR / "membership" / "all_members_since_backtest_start.txt"
    tickers = [t.strip().upper() for t in members_file.read_text().split() if t.strip()]
    status = dict(con.execute("SELECT ticker, status FROM stock_status").fetchall())
    rows, n_fail, n_skip, start = [], 0, 0, time.time()
    try:
        for i, t in enumerate(tickers, 1):
            n_px = con.execute("SELECT count(*) FROM prices_daily WHERE ticker = ?", [t]).fetchall()[0][0]
            if n_px == 0:
                n_skip += 1
                rows.append({"ticker": t, "checks": "NO PRICES", "price_audit": status.get(t, "NO DATA")})
                print(f"[{i}/{len(tickers)}] {t:<8} no prices - skipped")
                continue
            n_rows, full, first_full, last = technicals.calculate(con, t)
            results, _ = technicals_audit.check(con, t)
            n_sw, n_sr = support_resistance.calculate(con, t)
            bad = [f"{name}: {detail}" for name, st, detail in results if st != "PASS"]
            n_fail += bool(bad)
            if export_each:
                export(con, t)
            left = (time.time() - start) / i * (len(tickers) - i) / 60
            print(f"[{i}/{len(tickers)}] {t:<8} {'FAIL' if bad else 'PASS'}  {n_rows:,} days, {full:,} with a full "
                  f"52-week window, {n_sr:,} support/resistance days  (about {left:.0f} min left)")
            for b in bad:
                print(f"      {b}")
            rows.append({"ticker": t, "checks": "FAIL: " + " | ".join(bad) if bad else "PASS",
                         "price_audit": status.get(t, ""), "days": n_rows, "full_52w_days": full,
                         "first_full_day": first_full, **latest_row(con, t)})
    except KeyboardInterrupt:
        print("\nStopped - stocks done so far are saved; run again to redo all.")
    df = pd.DataFrame(rows)
    if "buy_price_v3" in df:
        df["pct_from_buy"] = df["close"] / df["buy_price_v3"] - 1
        df["hl_ratio"] = df["high_52w"] / df["low_52w"]
    out = EXPORTS_DIR / "technicals" / "technicals_summary.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="latest", index=False)
        ws = xw.sheets["latest"]
        ws.freeze_panes = "B2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 13
    print(f"\nTECHNICALS  {len(rows)} stocks: {len(rows) - n_fail - n_skip} PASS, {n_fail} FAIL, "
          f"{n_skip} without prices")
    print(f"saved: {out.relative_to(EXPORTS_DIR.parent)} (latest values per stock; dates are each stock's last day)")
    return n_fail == 0


def main(args=None, db_path=TECH_DB):
    args = sys.argv[1:] if args is None else list(args)
    if "--members" in args:
        con = connect(db_path)
        ok = run_members(con, export_each="--export" in args)
        con.close()
        print("All checks passed." if ok else "Some checks FAILED - see above.")
        return
    tickers = read_tickers(args)
    con = connect(db_path)
    all_ok = True
    for t in tickers:
        n_px = con.execute("SELECT count(*) FROM prices_daily WHERE ticker = ?", [t]).fetchall()[0][0]
        if n_px == 0:
            print(f"\n{t}: no prices stored - skipped (run update_prices.py first; may be delisted)")
            all_ok = False
            continue
        rows, full, first_full, last = technicals.calculate(con, t)
        print(f"\n{t}: {rows:,} days calculated, {full:,} with a complete 52-week window "
              f"(from {first_full} to {last})")
        results, _ = technicals_audit.check(con, t)
        for name, status, detail in results:
            print(f"  [{status}] {name:<18} {detail}")
            all_ok &= status == "PASS"
        latest = con.execute("""SELECT date, low_52w, high_52w, avg_close_52w, low_6m FROM technicals_daily
                                WHERE ticker = ? ORDER BY date DESC LIMIT 1""", [t]).df().iloc[0]
        print("  For comparing with thinkorswim:")
        show(f"for {latest['date']:%a %b %d} (days before it)", latest)
        nxt = next_session_values(con, t)
        if nxt:
            show("for the next trading day (incl. last)", nxt)
        n_sw, n_sr = support_resistance.calculate(con, t)
        print(f"  support/resistance: {n_sw:,} swing points, {n_sr:,} days calculated")
        sr = con.execute("""SELECT date, ref_close, support, support_touches, resistance, resistance_touches
                            FROM sr_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1""", [t]).fetchall()
        if sr:
            d, ref, sup, sn, res, rn = sr[0]
            print(f"  {'for ' + format(d, '%a %b %d') + ' (days before it)':<38} support {sup:>9.2f} "
                  f"({sn} touches)  resistance {res:>9.2f} ({rn} touches)  ref close {ref:.2f}")
        nxt = support_resistance.next_session(con, t)
        if nxt:
            ref, sup, sn, res, rn = nxt
            print(f"  {'for the next trading day (incl. last)':<38} support {sup:>9.2f} "
                  f"({sn} touches)  resistance {res:>9.2f} ({rn} touches)  ref close {ref:.2f}"
                  f"   <- same as running your stock screener now")
        export(con, t)
        print(f"  saved exports/technicals/{t}.xlsx")
    con.close()
    print("\nAll checks passed." if all_ok else "\nSome checks FAILED - see above.")


if __name__ == "__main__":
    main()
