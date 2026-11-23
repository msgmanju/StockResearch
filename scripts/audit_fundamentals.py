"""
Audit the point-in-time fundamentals (step 8) - see src/checks/fundamentals_audit.py for the checks.

    python scripts/audit_fundamentals.py              # every index member since the backtest start
    python scripts/audit_fundamentals.py AAPL JPM     # just these
    python scripts/audit_fundamentals.py --offline    # without the Yahoo cross-check

Writes fund_audit / fund_audit_issues in data/fundamentals.duckdb,
exports/fundamentals/fundamentals_audit.xlsx and logs/audit/fundamentals_audit_<time>.txt.
Safe to stop with Ctrl+C.
"""
import sys
import time
import uuid
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import EXPORTS_DIR, FUND_DB, LOGS_DIR, connect, run  # noqa: E402
from src.checks import fundamentals_audit as fa  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_audit (
    ticker VARCHAR PRIMARY KEY, status VARCHAR, reason VARCHAR, coverage DOUBLE, has_revenue DOUBLE,
    has_eps DOUBLE, has_op_margin DOUBLE, has_debt_to_equity DOUBLE, run_id VARCHAR, audited_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS fund_audit_issues (
    run_id VARCHAR, ticker VARCHAR, check_name VARCHAR, date DATE, field VARCHAR, ours DOUBLE, other DOUBLE,
    result VARCHAR, note VARCHAR
);
"""
ORDER = ["BLOCKED", "NEEDS REVIEW", "NO DATA", "VERIFIED - ANNUAL ONLY", "VERIFIED - NO CROSS-CHECK", "VERIFIED"]


def members():
    f = EXPORTS_DIR / "membership" / "all_members_since_backtest_start.txt"
    return [t.strip().upper() for t in f.read_text().split() if t.strip()]


def save(con, a, run_id):
    s = a.summary
    run(con, "INSERT OR REPLACE INTO fund_audit VALUES (?,?,?,?,?,?,?,?,?,current_timestamp)",
        [a.ticker, a.status, a.reason, s.get("coverage"), s.get("has_revenue_ttm"), s.get("has_eps_diluted_ttm"),
         s.get("has_op_margin_ttm"), s.get("has_debt_to_equity"), run_id])
    run(con, "DELETE FROM fund_audit_issues WHERE ticker = ?", [a.ticker])
    for i in a.issues:
        run(con, "INSERT INTO fund_audit_issues VALUES (?,?,?,?,?,?,?,?,?)",
            [run_id, a.ticker, i["check"], i["date"], i["field"],
             None if i["ours"] is None or pd.isna(i["ours"]) else float(i["ours"]),
             None if i["other"] is None or pd.isna(i["other"]) else float(i["other"]), i["result"], i["note"]])


def main(args=None, today=None, yahoo_fetch=None, db_path=FUND_DB):
    args = sys.argv[1:] if args is None else list(args)
    online = "--offline" not in args
    picked = [a.upper() for a in args if not a.startswith("--")]
    tickers = picked or members()
    today = today or dt.date.today()
    run_id = uuid.uuid4().hex[:8]
    con = connect(db_path)
    con.execute(SCHEMA).fetchall()
    print(f"Run {run_id}: {len(tickers)} stock(s)" + ("" if online else " (offline - no Yahoo cross-check)"))
    reviewed = fa.reviewed_items()
    lines, start = [], time.time()
    try:
        for i, t in enumerate(tickers, 1):
            a = fa.audit(con, t, today, yahoo_fetch, online, reviewed)
            save(con, a, run_id)
            left = (time.time() - start) / i * (len(tickers) - i) / 60
            cov = a.summary.get("coverage")
            print(f"[{i}/{len(tickers)}] {t:<8} {a.status:<26} "
                  f"{'' if cov is None else f'coverage {cov:.0%}  '}(about {left:.0f} min left)")
            lines.append(f"{t:<8} {a.status:<26} {a.reason}")
            for r in a.results:
                if r["status"] in ("REVIEW", "FAIL"):
                    lines.append(f"    [{r['status']}] {r['check']:<11} {r['detail']}")
            for x in a.issues:
                if x["result"] in ("REVIEW", "FAIL"):
                    lines.append(f"        {x['date']}  {x['check']:<11} {x['field']:<20} ours {x['ours']}  "
                                 f"other {x['other']}  {x['note']}")
    except KeyboardInterrupt:
        print("\nStopped - stocks done so far are saved.")
    q = ",".join("?" * len(tickers))
    df = con.execute(f"SELECT * FROM fund_audit WHERE ticker IN ({q})", tickers).df()
    df["o"] = df["status"].map({s: i for i, s in enumerate(ORDER)})
    df = df.sort_values(["o", "ticker"]).drop(columns="o")
    issues = con.execute(f"""SELECT * FROM fund_audit_issues WHERE ticker IN ({q})
                             ORDER BY ticker, check_name, date""", tickers).df()
    print("=" * 100)
    print(f"FUNDAMENTALS AUDIT  (run {run_id})")
    for _, r in df[~df["status"].str.startswith("VERIFIED")].iterrows():
        print(f"  {r['ticker']:<8} {r['status']:<26} {str(r['reason'])[:120]}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in df["status"].value_counts().items()))
    ok = df[df["status"].str.startswith("VERIFIED")]
    if len(ok):
        print(f"  verified stocks - average share of month-ends with: revenue {ok['has_revenue'].mean():.0%}, "
              f"EPS {ok['has_eps'].mean():.0%}, op margin {ok['has_op_margin'].mean():.0%}, "
              f"debt/equity {ok['has_debt_to_equity'].mean():.0%}")
    out = EXPORTS_DIR / "fundamentals" / "fundamentals_audit.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="status", index=False)
        issues.to_excel(xw, sheet_name="issues", index=False)
    log = LOGS_DIR / "audit" / f"fundamentals_audit_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(lines) + "\n")
    print(f"  saved: {out.relative_to(EXPORTS_DIR.parent)} and {log.relative_to(LOGS_DIR.parent)}")
    con.close()


if __name__ == "__main__":
    main()
