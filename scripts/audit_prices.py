"""
Audit stored daily prices (docs/AUDIT_PROCEDURE.md) and set each stock's status.

    python scripts/audit_prices.py               # every ticker in tickers.txt
    python scripts/audit_prices.py AAPL          # just these
    python scripts/audit_prices.py --members     # every index member with prices (price_coverage OK)
    python scripts/audit_prices.py --members --resume   # continue: skip stocks already audited today
    python scripts/audit_prices.py --offline     # skip the second-source comparison
    python scripts/audit_prices.py --sources tiingo,nasdaq   # choose the second source(s)

With --members the second source is Tiingo only (Nasdaq.com covers just the last year) unless
--sources is given, and the detailed per-stock reports are written only for stocks that are not
VERIFIED; every stock is still in the database and in exports/audit/price_audit_summary.xlsx.

Writes, for each ticker (all, or with --members only those needing attention):
    logs/audit/<TICKER>_prices_<date_time>.txt     readable report
    exports/audit/<TICKER>_price_audit.xlsx        status, checks, issues, side-by-side comparison
    audit_runs / audit_issues / stock_status tables in data/technicals.duckdb
Ends with a status summary. Safe to stop with Ctrl+C. If the audit makes an automatic correction,
run calc_technicals.py afterwards (the normal order anyway).
"""
import sys
import time
import uuid
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import EXPORTS_DIR, LOGS_DIR, TECH_DB, connect, read_tickers, run  # noqa: E402
from src.checks import price_audit  # noqa: E402


def _members(con):
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'price_coverage'")[0][0]
    if not has:
        raise SystemExit("No price_coverage table yet - run scripts/load_all_prices.py first.")
    return [r[0] for r in run(con, "SELECT ticker FROM price_coverage WHERE status = 'OK' ORDER BY ticker")]


def _write_reports(a, run_id, quiet):
    report = price_audit.text_report(a, run_id)
    log = LOGS_DIR / "audit" / f"{a.ticker}_prices_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(report + "\n")
    price_audit.excel_report(a, EXPORTS_DIR / "audit" / f"{a.ticker}_price_audit.xlsx")
    if not quiet:
        print(report)
        print(f"  saved: {log.relative_to(LOGS_DIR.parent)}  and  exports/audit/{a.ticker}_price_audit.xlsx\n")


def summary_excel(con, tickers, path):
    q = ",".join("?" * len(tickers))
    st = con.execute(f"""SELECT ticker, status, reason, price_source, first_date, last_date, run_id, audited_at
                         FROM stock_status WHERE ticker IN ({q}) ORDER BY
                         CASE status WHEN 'BLOCKED' THEN 0 WHEN 'NEEDS REVIEW' THEN 1
                                     WHEN 'VERIFIED - SINGLE SOURCE' THEN 2 ELSE 3 END, ticker""", tickers).df()
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        st.to_excel(w, index=False, sheet_name="status")
        st["status"].value_counts().rename_axis("status").reset_index(name="stocks").to_excel(
            w, index=False, sheet_name="counts")
        ws = w.sheets["status"]
        for col, width in zip("ABCDEFGH", (9, 26, 90, 10, 12, 12, 10, 20)):
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, (dt.date, dt.datetime)):
                    c.number_format = "yyyy-mm-dd"
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions


def main(args=None, db_path=TECH_DB, sources=None, quiet=False):
    args = sys.argv[1:] if args is None else list(args)
    online = "--offline" not in args
    members_mode, resume = "--members" in args, "--resume" in args
    if "--sources" in args:
        sources = [x.strip() for x in args[args.index("--sources") + 1].split(",") if x.strip()]
    plain = [a for i, a in enumerate(args) if not a.startswith("--") and not (i and args[i - 1] == "--sources")]
    run_id = uuid.uuid4().hex[:8]
    con = connect(db_path)
    con.execute(price_audit.AUDIT_SCHEMA).fetchall()

    if members_mode:
        tickers = _members(con)
        sources = sources or ["tiingo"]
        quiet = True
    else:
        tickers = read_tickers(plain)
    if resume:
        today = dt.datetime.combine(dt.date.today(), dt.time())
        done = {r[0] for r in run(con, "SELECT ticker FROM stock_status WHERE audited_at >= ?", [today])}
        skipped = [t for t in tickers if t in done]
        tickers_todo = [t for t in tickers if t not in done]
    else:
        skipped, tickers_todo = [], tickers
    print(f"Run {run_id}: {len(tickers)} stock(s); already audited today {len(skipped)}; to do {len(tickers_todo)}"
          + (f"; second source: {', '.join(sources)}" if online and sources else ""))

    summary, t0 = [], time.time()
    try:
        for i, t in enumerate(tickers_todo, 1):
            try:
                a = price_audit.audit_ticker(con, t, online=online, sources=sources)
            except Exception as e:                                   # one bad stock never stops the run
                print(f"[{i}/{len(tickers_todo)}] {t:<8} ERROR {type(e).__name__}: {e}")
                summary.append((t, "ERROR", f"{type(e).__name__}: {e}"))
                continue
            price_audit.save_to_db(con, a, run_id)
            if not members_mode or a.stock_status not in ("VERIFIED",):
                _write_reports(a, run_id, quiet)
            summary.append((t, a.stock_status, a.reason))
            if members_mode:
                left = (time.time() - t0) / i * (len(tickers_todo) - i) / 60
                print(f"[{i}/{len(tickers_todo)}] {t:<8} {a.stock_status:<26} (about {left:.0f} min left)")
    except KeyboardInterrupt:
        print("\nStopped. Everything audited so far is saved - run with --resume to continue.")

    print("=" * 100)
    print(f"STATUS SUMMARY  (run {run_id})")
    counts = {}
    for t, s, r in summary:
        counts[s] = counts.get(s, 0) + 1
        if not members_mode or s != "VERIFIED":
            print(f"  {t:<8} {s:<26} {r[:160]}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    if members_mode:
        out = EXPORTS_DIR / "audit" / "price_audit_summary.xlsx"
        summary_excel(con, tickers, out)
        allc = dict(run(con, f"""SELECT status, count(*) FROM stock_status WHERE ticker IN
                                 ({','.join('?' * len(tickers))}) GROUP BY 1""", tickers))
        print("  all members so far: " + ", ".join(f"{k}: {v}" for k, v in sorted(allc.items())))
        print("  saved: exports/audit/price_audit_summary.xlsx (details for non-VERIFIED stocks in logs/audit/)")
    con.close()
    return {t: s for t, s, _ in summary}


if __name__ == "__main__":
    main()
