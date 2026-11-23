"""
Build index membership history and check it (docs/MEMBERSHIP_PROCEDURE.md).

    python scripts/build_membership.py           # every index set up below (NASDAQ-100, S&P 500, S&P 100)
    python scripts/build_membership.py SP500     # just this one (NDX, SP500 or SP100)
    python scripts/build_membership.py SP100     # S&P 100 (needs the S&P 500 built first; checked against it)

Run it daily (the daily agent will): each run saves today's official list, so changes
from now on are recorded from the official source even if Wikipedia is late.

Writes:
    index_membership / index_changes / index_snapshots tables in data/technicals.duckdb
    logs/audit/membership_<INDEX>_<date_time>.txt                  readable report
    exports/membership/<INDEX>_membership.xlsx                      periods, changes, checks, daily counts
    exports/membership/<INDEX>_members_since_backtest_start.txt     tickers whose prices we need
    exports/membership/all_members_since_backtest_start.txt         all indexes together
"""
import sys
import uuid
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import EXPORTS_DIR, LOGS_DIR, TECH_DB, connect, run  # noqa: E402
from src import membership as mb  # noqa: E402
from src.collect import membership_sources as ms  # noqa: E402
from src.checks.price_audit import AUDIT_SCHEMA  # noqa: E402
from src.checks import etf_membership_check as etf_check  # noqa: E402
from src import sp100_build  # noqa: E402

# Each index plugs in here.
INDEXES = {
    "NDX": {
        "label": "NASDAQ-100",
        "official": ms.ndx_current_nasdaq, "official_name": "Nasdaq.com",
        "wiki_url": ms.NDX_WIKI, "wiki_raw_name": "ndx_wikipedia",     # list of pages, tried in order
        "min": int(mb.CFG["ndx_min"]), "max": int(mb.CFG["ndx_max"]),
        "etf": "QQQ",
    },
    "SP500": {
        "label": "S&P 500",
        "official": ms.sp500_current, "official_name": "iShares IVV (or SSGA SPY)",
        "wiki_url": ms.SP500_WIKI, "wiki_raw_name": "sp500_wikipedia",
        "min": int(mb.CFG["sp500_min"]), "max": int(mb.CFG["sp500_max"]),
        "etf": "SPY",
    },
    "SP100": {                    # built differently: no free change list exists (src/sp100_build.py)
        "label": "S&P 100",
        "official_name": "iShares OEF",
        "min": int(mb.CFG["sp100_min"]), "max": int(mb.CFG["sp100_max"]),
        "builder": sp100_build.build_sp100,
    },
}


def build_index(con, key, today, http=None, http_get=None, http_post=None):
    cfg = INDEXES[key]
    if "builder" in cfg:
        return cfg["builder"](con, cfg, today, http, http_get, http_post)
    kw = {"http": http} if http else {}
    results, issues = [], []

    def add(check, status, n, detail):
        results.append({"check": check, "status": status, "n_items": n, "detail": detail})

    # 1. today's official list
    try:
        official, _ = cfg["official"](**kw)
        if len(official) < cfg["min"] * 0.9:
            raise ValueError(f"only {len(official)} tickers returned")
    except Exception as e:
        official = None
        add("official_list", "FAIL", 0, f"{cfg['official_name']} list could not be used: {type(e).__name__}: {e}")

    # 2. Wikipedia: today's list and the change history (each from the first page that has it)
    empty = pd.DataFrame(columns=["date", "action", "ticker", "company", "reason"])
    wiki_now, changes, notes = ms.wiki_tables(cfg["wiki_url"], cfg["wiki_raw_name"], **kw)
    if changes is None:
        cached = con.execute("""SELECT date, action, ticker_at_time AS ticker, company, reason FROM index_changes
                                WHERE index_name = ? AND source = 'wikipedia'""", [key]).df()
        if len(cached):
            cached["date"] = pd.to_datetime(cached["date"]).dt.date
            changes = cached
            add("wikipedia", "REVIEW", len(cached), "Wikipedia change table not found today - using the change "
                f"history saved by the last successful run ({len(cached)} rows); " + "; ".join(notes))
        else:
            changes = empty
            add("wikipedia", "REVIEW", 0, "Wikipedia change table not found and no saved history - "
                "history from saved official snapshots only; " + "; ".join(notes))
    else:
        add("wikipedia", "PASS" if wiki_now is not None else "REVIEW", len(changes),
            "; ".join(notes) + ("" if wiki_now is not None else "; components table not found"))
    if official is not None and wiki_now is not None:      # 'BRKB' in a file -> 'BRK-B' as Wikipedia writes it
        official = official.assign(ticker=ms.fix_share_class(official["ticker"], wiki_now["ticker"]))
    if official is None and wiki_now is None:
        raise RuntimeError(f"{key}: neither the official list nor Wikipedia is available - nothing changed")
    base = official if official is not None else wiki_now

    if official is not None and wiki_now is not None:
        a, b = set(official["ticker"]), set(wiki_now["ticker"])
        for t in sorted(a - b):
            issues.append(("sources_today", today, t, "REVIEW", f"on {cfg['official_name']} list, not on Wikipedia's"))
        for t in sorted(b - a):
            issues.append(("sources_today", today, t, "REVIEW", f"on Wikipedia's list, not on {cfg['official_name']}'s"))
        diff = len(a ^ b)
        add("sources_today", "PASS" if not diff else "REVIEW", len(a | b),
            f"{cfg['official_name']} {len(a)} tickers vs Wikipedia {len(b)}; differences {diff}")

    # 3. save today's official list; compare with earlier snapshots
    if official is not None:
        mb.save_snapshot(con, key, today, official, cfg["official_name"])
    renames = mb.load_ticker_changes()
    cal = mb.sessions(mb.START, today + dt.timedelta(days=45))
    extra, snap_problems, snap_dates = mb.reconcile_snapshots(con, key, changes, renames, cal)
    issues += snap_problems
    n_rev = sum(1 for p in snap_problems if p[3] == "REVIEW")
    add("official_snapshots", "REVIEW" if n_rev else "PASS", len(snap_dates),
        f"{len(snap_dates)} saved official list(s)"
        + (f", {snap_dates[0]} to {snap_dates[-1]}" if snap_dates else "")
        + f"; changes seen only in official lists: {len(extra)}; items to review: {n_rev}")

    # 4. rebuild history
    all_changes = pd.concat([changes.assign(source="wikipedia"), extra], ignore_index=True)
    periods, ch, problems = mb.build_periods(key, base, all_changes, today, renames,
                                             source=f"{cfg['official_name']} today + Wikipedia + saved official lists")
    issues += problems
    splits = [x for x in mb.load_splits() if x["index"] == key]
    if splits:
        periods = mb.apply_splits(periods, key, splits, mb.sessions(mb.START, today + dt.timedelta(days=45)))
    walk_fail = sum(1 for p in problems if p[0] == "walk")
    add("history_consistent", "PASS" if walk_fail == 0 else "FAIL", int(ch["applied"].sum()),
        f"{int(ch['applied'].sum())} changes applied; {walk_fail} inconsistent; "
        f"{sum(1 for p in problems if p[0] == 'pending_change')} announced change(s) pending")

    earliest = changes["date"].min() if len(changes) else None
    covered = earliest is not None and earliest <= mb.BACKTEST_START
    add("history_coverage", "PASS" if covered else "REVIEW", len(changes),
        f"Wikipedia change history starts {earliest}; needed from {mb.BACKTEST_START}"
        + ("" if covered else " - membership before the first known change is assumed unchanged"))

    counts = mb.daily_counts(periods, today)
    out = (counts["members"] < cfg["min"]) | (counts["members"] > cfg["max"])
    # a spin-off is usually added a day before its replacement leaves: back in range within 3 sessions = normal
    back = pd.Series([not out.iloc[i + 1:i + 4].all() if i + 1 < len(out) else False for i in range(len(out))],
                     index=counts.index)
    transient, bad = counts[out & back], counts[out & ~back]
    for _, r in transient.iterrows():
        issues.append(("member_count", r["date"], "", "INFO", f"{r['members']} members for a few days "
                       f"(allowed {cfg['min']}-{cfg['max']}) - spin-off/replacement overlap"))
    for _, r in bad.head(50).iterrows():
        issues.append(("member_count", r["date"], "", "FAIL", f"{r['members']} members (allowed {cfg['min']}-{cfg['max']})"))
    add("member_count", "PASS" if bad.empty else "FAIL", len(counts),
        f"members per day {counts['members'].min()}-{counts['members'].max()}; "
        f"days outside {cfg['min']}-{cfg['max']}: {len(bad)} (+{len(transient)} short spin-off overlaps, allowed)")

    applied = ch[ch["applied"]]
    per_day = applied.groupby(["date", "action"]).size().unstack(fill_value=0) if len(applied) else pd.DataFrame()
    uneven = per_day[per_day.get("add", 0) != per_day.get("remove", 0)] if len(per_day) else per_day
    for d, r in uneven.iterrows():
        issues.append(("one_in_one_out", d, "", "INFO",
                       f"{int(r.get('add', 0))} added, {int(r.get('remove', 0))} removed (spin-off or delayed replacement?)"))
    add("one_in_one_out", "INFO" if len(uneven) else "PASS", len(per_day),
        f"dates where additions and removals differ: {len(uneven)} (normal for spin-offs)")

    has_prices = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'prices_daily'")[0][0]
    priced = {r[0] for r in run(con, "SELECT DISTINCT ticker FROM prices_daily")} if has_prices else set()
    need_rows = periods[periods["end_date"].isna() | (periods["end_date"] >= mb.BACKTEST_START)]
    need = sorted(set(need_rows["ticker"]))
    missing = sorted(set(need) - priced)
    add("prices_exist", "INFO", len(need),
        f"{len(need)} tickers were members since {mb.BACKTEST_START}; {len(missing)} have no prices stored yet")
    # 5. independent check against the ETF's quarterly SEC filings (+ quarter-end prices)
    try:
        etf_results, etf_issues, _ = etf_check.run_check(con, key, cfg["etf"], periods, renames, today,
                                                         http_get=http_get, http_post=http_post)
        results.extend(etf_results)
        issues += etf_issues
    except Exception as e:
        add(f"{cfg['etf'].lower()}_cross_check", "REVIEW", 0,
            f"could not run ({type(e).__name__}: {str(e)[:200]})")

    mb.store(con, key, periods, ch, f"{cfg['official_name']} today + Wikipedia + saved official lists")
    return {"periods": periods, "changes": ch, "counts": counts, "results": results,
            "issues": issues, "need": need, "missing": missing}


def status_of(results):
    s = {r["status"] for r in results}
    return "BLOCKED" if "FAIL" in s else "NEEDS REVIEW" if "REVIEW" in s else "VERIFIED"


def report(key, b, run_id, status):
    p, ch = b["periods"], b["changes"]
    lines = [f"MEMBERSHIP  {INDEXES[key]['label']}   STATUS: {status}",
             f"run {run_id} at {dt.datetime.now():%Y-%m-%d %H:%M}; {len(p)} membership periods, "
             f"{p['ticker'].nunique()} tickers, {int(ch['applied'].sum())} changes applied", ""]
    lines += [f"  [{r['status']:<6}] {r['check']:<20} {r['detail']}" for r in b["results"]]
    open_items = [i for i in b["issues"] if i[3] in ("FAIL", "REVIEW")]
    if open_items:
        lines += ["", "Items that need attention (first 30):"]
        lines += [f"  [{res}] {d}  {check:<20} {t:<10} {note}" for check, d, t, res, note in open_items[:30]]
    info = [i for i in b["issues"] if i[3] == "INFO"]
    if info:
        lines += ["", "Information (first 15):"]
        lines += [f"  {d}  {check:<20} {t:<10} {note}" for check, d, t, res, note in info[:15]]
    return "\n".join(lines)


def export(key, b):
    out = EXPORTS_DIR / "membership"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key}_members_since_backtest_start.txt").write_text("\n".join(b["need"]) + "\n")
    with pd.ExcelWriter(out / f"{key}_membership.xlsx", engine="openpyxl") as xw:
        b["periods"].to_excel(xw, sheet_name="periods", index=False)
        b["changes"].to_excel(xw, sheet_name="changes", index=False)
        pd.DataFrame(b["results"]).to_excel(xw, sheet_name="checks", index=False)
        pd.DataFrame(b["issues"], columns=["check", "date", "ticker", "result", "note"]).to_excel(
            xw, sheet_name="issues", index=False)
        b["counts"].to_excel(xw, sheet_name="daily_count", index=False)
        for ws in xw.sheets.values():
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 16
                if col[0].value in ("date", "start_date", "end_date"):
                    for c in col[1:]:
                        c.number_format = "yyyy-mm-dd"
                if col[0].value in ("detail", "note", "reason", "company"):
                    ws.column_dimensions[col[0].column_letter].width = 60


def main(args=None, today=None, db_path=TECH_DB, http=None, http_get=None, http_post=None):
    args = sys.argv[1:] if args is None else args
    asked = [a.upper() for a in args] or list(INDEXES)
    unknown = [k for k in asked if k not in INDEXES]
    keys = [k for k in INDEXES if k in asked]             # S&P 500 before S&P 100 (checked against it)
    if unknown:
        print(f"Unknown index {unknown}; available: {list(INDEXES)}")
        return {}
    today = today or dt.date.today()
    run_id = uuid.uuid4().hex[:8]
    con = connect(db_path)
    con.execute(AUDIT_SCHEMA).fetchall()
    con.execute(mb.SCHEMA).fetchall()
    statuses = {}
    for key in keys:
        b = build_index(con, key, today, http, http_get, http_post)
        status = statuses[key] = status_of(b["results"])
        for r in b["results"]:
            run(con, "INSERT INTO audit_runs VALUES (?, current_timestamp, ?, 'membership', ?, ?, ?, ?, ?)",
                [run_id, key, r["check"], r["status"], r["n_items"], 0, r["detail"]])
        for check, d, t, res, note in b["issues"]:
            run(con, "INSERT INTO audit_issues VALUES (?, ?, ?, ?, 'membership', NULL, NULL, NULL, ?)",
                [run_id, key, check, d, f"[{res}] {t} {note}".strip()])
        text = report(key, b, run_id, status)
        print(text)
        log = LOGS_DIR / "audit" / f"membership_{key}_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text + "\n")
        export(key, b)
        print(f"\nsaved: {log.relative_to(LOGS_DIR.parent)}, exports/membership/{key}_membership.xlsx, "
              f"exports/membership/{key}_members_since_backtest_start.txt ({len(b['need'])} tickers, "
              f"{len(b['missing'])} without prices yet)\n")
    allm = [r[0] for r in run(con, """SELECT DISTINCT ticker FROM index_membership
                                      WHERE end_date IS NULL OR end_date >= ? ORDER BY 1""", [mb.BACKTEST_START])]
    (EXPORTS_DIR / "membership" / "all_members_since_backtest_start.txt").write_text("\n".join(allm) + "\n")
    con.close()
    print(f"all indexes: {len(allm)} tickers -> exports/membership/all_members_since_backtest_start.txt")
    return statuses


if __name__ == "__main__":
    main()
