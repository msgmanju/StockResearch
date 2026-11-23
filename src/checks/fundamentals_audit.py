"""
Checks for the point-in-time fundamentals (step 8). See docs/FUNDAMENTALS.md.

  raw_vs_db     the stored figures are exactly what the saved SEC files give when read again
  coverage      on every month-end of the stock's window (1 year before joining the index to leaving)
                a recent report is known: latest period end within 200 days (yearly filers: 470)
  fields        share of those month-ends with revenue / EPS / op margin / debt-to-equity
  derived_q4    on every report whose latest quarter was derived (annual minus 9 months), that quarter must
                look like a quarter: revenue > 0 and at most 3x the average of the other three
  jumps         TTM revenue more than doubling or halving between two consecutive reports
                (usually a merger or spin-off - listed for review)
  yahoo         latest values vs Yahoo Finance (current members), only when Yahoo's latest quarter is the
                same as ours: TTM revenue within 3% (banks/insurers: information only - Yahoo defines their
                revenue differently), TTM EPS within 5% (or 5 cents), quarterly revenue growth within 3 points. Operating margin and debt/equity are information only
                (providers adjust operating income and define debt differently).
  eps_sign      information only (quarterly EPS summed across a big share-count change)

Items you have looked at and accepted (a real merger, a known data quirk) go in
config/fundamentals_reviewed.toml; they then show as REVIEWED (information) instead of NEEDS REVIEW.
"""
import datetime as dt
import json
import tomllib

import pandas as pd

from src.collect import sec_facts as sf
from src.calculate import fundamentals as fu
from src.common import RAW_DIR, ROOT, run

FRESH_DAYS, FRESH_DAYS_ANNUAL = 200, 470
OK_SHARE = 0.90
YAHOO_TOL = {"revenue_ttm": 0.03, "eps_diluted_ttm": 0.05, "revenue_growth_q_yoy": 0.03}
YAHOO_INFO = {"op_margin_ttm": 0.03}        # providers adjust operating income differently


REVIEWED_FILE = ROOT / "config" / "fundamentals_reviewed.toml"


def reviewed_items():
    """[[reviewed]] entries: ticker, check, optional dates (report filed dates; none = all), note."""
    if not REVIEWED_FILE.exists():
        return []
    with open(REVIEWED_FILE, "rb") as f:
        return tomllib.load(f).get("reviewed", [])


def apply_reviewed(a, items):
    """Mark matching REVIEW issues as REVIEWED. A check whose REVIEW issues are all reviewed becomes INFO."""
    mine = [r for r in items if str(r.get("ticker", "")).upper() == a.ticker]
    if not mine:
        return
    for i in a.issues:
        if i["result"] != "REVIEW":
            continue
        for r in mine:
            dates = [str(d) for d in r.get("dates", [])]
            if r.get("check") == i["check"] and (not dates or str(i["date"]) in dates):
                i["result"] = "REVIEWED"
                i["note"] = f"reviewed: {r.get('note', '')} | {i['note']}"
                break
    for res in a.results:
        if res["status"] != "REVIEW":
            continue
        own = [i for i in a.issues if i["check"] == res["check"]]
        if own and len(own) < 50 and all(i["result"] != "REVIEW" for i in own) and any(i["result"] == "REVIEWED" for i in own):
            res["status"] = "INFO"
            res["detail"] += " - all reviewed (config/fundamentals_reviewed.toml)"


class FundAudit:
    def __init__(self, ticker):
        self.ticker = ticker
        self.results, self.issues = [], []
        self.status, self.reason = None, ""
        self.summary = {}

    def add(self, check, status, detail):
        self.results.append({"check": check, "status": status, "detail": detail})

    def issue(self, check, date, field, ours, other, result, note):
        if sum(1 for i in self.issues if i["check"] == check) < 50:
            self.issues.append({"check": check, "date": date, "field": field, "ours": ours, "other": other,
                                "result": result, "note": note})


def _status(fails, reviews=0, infos=0):
    return "FAIL" if fails else "REVIEW" if reviews else "INFO" if infos else "PASS"


def check_raw_vs_db(a, con):
    rows = run(con, "SELECT cik, valid_from, valid_to, facts_file FROM cik_map WHERE ticker = ? ORDER BY seq",
               [a.ticker])
    parts = [fu.extract(sf.load_facts(ROOT / ff), c, vf, vt) for c, vf, vt, ff in rows]
    raw = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()
    db = con.execute("""SELECT metric, concept, start, "end", val, filed FROM fund_facts WHERE ticker = ?""",
                     [a.ticker]).df()
    if raw.empty and db.empty:
        a.add("raw_vs_db", "FAIL", "no figures in the SEC file(s) for the tracked tags")
        return
    key = ["metric", "concept", "start", "end", "filed", "val"]
    for d in (raw, db):
        for c in ("start", "end", "filed"):
            d[c] = pd.to_datetime(d[c]).dt.date
        d["val"] = d["val"].round(6)
    r = raw[key].astype(str).value_counts()
    b = db[key].astype(str).value_counts()
    diff = r.subtract(b, fill_value=0)
    bad = diff[diff != 0]
    for idx, n in bad.head(20).items():
        a.issue("raw_vs_db", idx[4], idx[0], None, float(idx[5]), "FAIL",
                ("in the SEC file but not stored" if n > 0 else "stored but not in the SEC file")
                + f" ({idx[1]} {idx[2]}..{idx[3]})")
    a.add("raw_vs_db", _status(len(bad)), f"{len(raw):,} figures re-read from {len(rows)} SEC file(s); "
                                          f"{len(bad)} differences")


def month_ends(start, end):
    d = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
    out = []
    while d <= end:
        out.append(d)
        d = (pd.Timestamp(d) + pd.offsets.MonthEnd(1)).date()
    return out


def as_of(snaps, day):
    """The snapshot in force on `day`: the last one FILED BEFORE that day (a report filed on a day is
    used from the next day on)."""
    s = snaps[snaps["filed"] < day]
    return None if s.empty else s.iloc[-1]


def check_coverage(a, snaps, w_start, w_end):
    days = month_ends(w_start + dt.timedelta(days=120), w_end)
    if not days:
        a.add("coverage", "INFO", "window too short to check")
        return
    fresh, have = 0, {"revenue_ttm": 0, "eps_diluted_ttm": 0, "op_margin_ttm": 0, "debt_to_equity": 0}
    stale = []
    for d in days:
        s = as_of(snaps, d)
        if s is None or pd.isna(s["period_end"]):
            stale.append(d)
            continue
        limit = FRESH_DAYS_ANNUAL if s["annual_only"] else FRESH_DAYS
        if (d - s["period_end"]).days > limit:
            stale.append(d)
            continue
        fresh += 1
        for f in have:
            have[f] += pd.notna(s[f])
    share = fresh / len(days)
    a.summary["coverage"] = share
    for f, n in have.items():
        a.summary[f"has_{f}"] = n / fresh if fresh else 0.0
    if stale:
        runs, cur = [], [stale[0], stale[0]]
        for d in stale[1:]:
            if (d - cur[1]).days <= 32:
                cur[1] = d
            else:
                runs.append(cur)
                cur = [d, d]
        runs.append(cur)
        for lo, hi in runs[:10]:
            a.issue("coverage", lo, "report", None, None, "REVIEW",
                    f"no recent report known from {lo} to {hi}")
    a.add("coverage", "PASS" if share >= OK_SHARE else "REVIEW",
          f"{fresh}/{len(days)} month-ends ({share:.0%}) with a recent report; fields present: "
          + ", ".join(f"{f.replace('_ttm', '')} {n / fresh if fresh else 0:.0%}" for f, n in have.items()))


def check_derived_q4(a, snaps):
    """A latest quarter derived as annual minus 9 months must look like a quarter at the time it was used."""
    s = snaps[snaps["revenue_q_derived"].fillna(False).astype(bool)].dropna(subset=["revenue_q", "revenue_ttm"])
    bad = 0
    for r in s.itertuples():
        avg = (r.revenue_ttm - r.revenue_q) / 3
        if r.revenue_q <= 0 or (avg > 0 and r.revenue_q > 3 * avg):
            bad += 1
            a.issue("derived_q4", r.filed, "revenue_q", r.revenue_q, avg, "REVIEW",
                    "quarter derived as annual minus 9 months doesn't look like a quarter "
                    "(periods may not line up)")
    a.add("derived_q4", _status(0, bad), f"{len(s)} reports with a derived latest quarter; {bad} implausible")


def check_eps_sign(a, snaps):
    s = snaps.dropna(subset=["eps_diluted_ttm", "net_income_ttm"])
    bad = s[(s["eps_diluted_ttm"] * s["net_income_ttm"] < 0) & (s["net_income_ttm"].abs() > 1e6)]
    for r in bad.head(10).itertuples():
        a.issue("eps_sign", r.filed, "eps_diluted_ttm", r.eps_diluted_ttm, r.net_income_ttm, "INFO",
                "TTM EPS and TTM net income have opposite signs - usually quarterly EPS summed across a big "
                "change in share count (merger, bankruptcy, spin-off)")
    a.add("eps_sign", "INFO" if len(bad) else "PASS",
          f"{len(s)} reports checked; {len(bad)} with opposite signs (information)")


def check_jumps(a, snaps):
    s = snaps.dropna(subset=["revenue_ttm"])
    s = s[s["revenue_ttm"] > 0]
    r = s["revenue_ttm"] / s["revenue_ttm"].shift(1)
    bad = s[(r > 2.0) | (r < 0.5)]
    for row in bad.itertuples():
        a.issue("jumps", row.filed, "revenue_ttm", row.revenue_ttm, None, "REVIEW",
                f"TTM revenue changed x{(r[row.Index]):.2f} from the previous report (merger/spin-off?)")
    a.add("jumps", _status(0, len(bad)), f"{len(s)} reports; {len(bad)} TTM revenue jumps (x2 or x0.5)")


# ------------------------------------------------------------------ Yahoo cross-check
def yahoo_info(ticker, fetch=None):
    """Yahoo's summary values (saved raw under data/raw/yahoo/info/)."""
    if fetch is None:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
    else:
        info = fetch(ticker)
    folder = RAW_DIR / "yahoo" / "info"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ticker}_{dt.datetime.now():%Y%m%d_%H%M%S}.json").write_text(json.dumps(info, default=str))
    return info


def check_yahoo(a, snaps, info, today):
    s = snaps.iloc[-1] if len(snaps) else None
    if s is None or (today - s["filed"]).days > 140:
        a.add("yahoo", "INFO", "no recent report to compare")
        return
    if s["currency"] != "USD" or info.get("financialCurrency", "USD") not in ("USD", None):
        a.add("yahoo", "INFO", f"reports in {s['currency']} - compared for information only")
        return
    mrq = info.get("mostRecentQuarter")
    if mrq:
        mrq = (dt.datetime.utcfromtimestamp(mrq) if isinstance(mrq, (int, float)) else pd.Timestamp(mrq)).date()
        if abs((mrq - s["period_end"]).days) > 10:
            a.add("yahoo", "INFO", f"Yahoo's latest quarter ends {mrq}, ours {s['period_end']} - "
                                   f"{'Yahoo already has a newer quarter (results out, report not filed yet)' if mrq > s['period_end'] else 'Yahoo is behind'}; not compared")
            return
    theirs = {"revenue_ttm": info.get("totalRevenue"), "eps_diluted_ttm": info.get("trailingEps"),
              "op_margin_ttm": info.get("operatingMargins"), "revenue_growth_q_yoy": info.get("revenueGrowth")}
    financial = s["op_income_ttm"] is None or pd.isna(s["op_income_ttm"])     # banks/insurers
    n, bad, notes = 0, 0, []
    for f, tol in {**YAHOO_TOL, **YAHOO_INFO}.items():
        ours, other = s[f], theirs[f]
        if ours is None or pd.isna(ours) or other is None:
            continue
        n += f in YAHOO_TOL
        if f in ("revenue_ttm", "eps_diluted_ttm"):
            diff = abs(ours / other - 1) if other else float("inf")
            ok = diff <= tol or abs(ours - other) <= (0.05 if f == "eps_diluted_ttm" else 0)
        else:
            diff = abs(ours - other)
            ok = diff <= tol
        if not ok:
            info_only = f in YAHOO_INFO or (financial and f == "revenue_ttm")
            bad += not info_only
            a.issue("yahoo", s["period_end"], f, float(ours), float(other), "INFO" if info_only else "REVIEW",
                    f"differs from Yahoo ({'%.1f%%' % (diff * 100) if f in ('revenue_ttm', 'eps_diluted_ttm') else '%.1f points' % (diff * 100)})"
                    + (" - information only" + (" (banks/insurers: revenue defined differently)"
                                                 if financial and f == "revenue_ttm" else "") if info_only else ""))
        notes.append(f.replace("_ttm", ""))
    de_y = info.get("debtToEquity")
    if de_y is not None and pd.notna(s["debt_to_equity"]):
        a.issue("yahoo", s["period_end"], "debt_to_equity", float(s["debt_to_equity"]), de_y / 100, "INFO",
                "debt/equity vs Yahoo (information only - Yahoo includes leases)")
    a.add("yahoo", _status(0, bad) if n else "INFO",
          f"{n} values compared with Yahoo ({', '.join(notes)}); {bad} outside tolerance")


# ------------------------------------------------------------------ one stock
def audit(con, ticker, today, yahoo_fetch=None, online=True, reviewed=None):
    a = FundAudit(ticker)
    st = run(con, "SELECT status, window_start, window_end, note FROM cik_status WHERE ticker = ?", [ticker])
    if not st:
        a.status, a.reason = "NO DATA", "not matched to an SEC company yet (run load_fundamentals.py)"
        return a
    cstatus, w_start, w_end, note = st[0]
    if cstatus in ("NO SEC DATA", "NOT FOUND", "NO XBRL"):
        a.status, a.reason = "NO DATA", f"{cstatus}: {note[:150]}"
        return a
    snaps = con.execute("SELECT * FROM fund_snapshots WHERE ticker = ? ORDER BY filed", [ticker]).df()
    for c in ("filed", "period_end", "balance_sheet_date"):
        snaps[c] = pd.to_datetime(snaps[c]).dt.date
    facts = con.execute("""SELECT metric, prio, concept, unit, start, "end", val, filed, accn, form, cik
                           FROM fund_facts WHERE ticker = ?""", [ticker]).df()
    for c in ("start", "end", "filed"):
        facts[c] = pd.to_datetime(facts[c]).dt.date
    facts["start"] = facts["start"].where(facts["start"].notna(), None)
    check_raw_vs_db(a, con)
    check_coverage(a, snaps, w_start, w_end)
    check_derived_q4(a, snaps)
    check_eps_sign(a, snaps)
    check_jumps(a, snaps)
    current = w_end >= today - dt.timedelta(days=7)
    if online and current:
        try:
            check_yahoo(a, snaps, yahoo_info(ticker, yahoo_fetch), today)
        except Exception as e:
            a.add("yahoo", "INFO", f"Yahoo not available: {type(e).__name__}: {str(e)[:100]}")
    apply_reviewed(a, reviewed if reviewed is not None else reviewed_items())
    order = {"PASS": 0, "INFO": 1, "REVIEW": 2, "FAIL": 3}
    worst = max((order[r["status"]] for r in a.results), default=0)
    cov = a.summary.get("coverage", 0)
    if worst == 3:
        a.status = "BLOCKED"
        a.reason = "failed: " + ", ".join(r["check"] for r in a.results if r["status"] == "FAIL")
    elif worst == 2:
        a.status = "NEEDS REVIEW"
        a.reason = "to review: " + ", ".join(r["check"] for r in a.results if r["status"] == "REVIEW")
    elif cstatus == "ANNUAL ONLY":
        a.status, a.reason = "VERIFIED - ANNUAL ONLY", "foreign filer: yearly reports only"
    elif not any(r["check"] == "yahoo" and r["status"] == "PASS" for r in a.results):
        a.status, a.reason = "VERIFIED - NO CROSS-CHECK", "passed; no current Yahoo values to compare (former member)"
    else:
        a.status, a.reason = "VERIFIED", "all checks passed, matches Yahoo"
    a.summary["coverage"] = cov
    return a
