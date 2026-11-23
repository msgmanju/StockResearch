"""
Step 8 - company fundamentals from SEC EDGAR.

    python scripts/load_fundamentals.py              # every index member (resumes where it stopped)
    python scripts/load_fundamentals.py AAPL DD-OLD  # just these
    python scripts/load_fundamentals.py --redo       # match and download everything again
    python scripts/load_fundamentals.py --rebuild    # recalculate every stock's fundamentals table

Part 1: find each stock's SEC company number (CIK) and download its "company facts" file
(every figure from its 10-K/10-Q filings, with filing dates).
Part 2: build the point-in-time tables in data/fundamentals.duckdb:
  fund_facts      every version of every tracked figure, with the date it was filed
  fund_snapshots  one row per filing: TTM revenue/op income/net income/EPS, quarterly revenue growth,
                  debt/equity ... as known right after that filing (see src/calculate/fundamentals.py)

How a stock is matched to a CIK - each candidate is checked, not trusted:
  1. config/cik_overrides.toml (your manual entries) - always wins
  2. SEC's current ticker list
  3. SEC's list of every company name ever registered (for delisted/renamed companies)
A candidate is judged by the reports it actually FILED from 1 year before joining the index to leaving
(not by the periods its reports mention - a new holding company's first 10-K repeats years it never filed).
When one CIK doesn't cover the whole stretch (holding-company reorganizations, mergers) the predecessor or
successor CIK is chained, each used only for the dates it filed.
"""
import sys
import time
import datetime as dt
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from src.common import ROOT, TECH_DB, FUND_DB, EXPORTS_DIR, connect, run  # noqa: E402
from src.collect import sec_facts as sf  # noqa: E402
from src.calculate import fundamentals as fu  # noqa: E402

OVERRIDES_FILE = ROOT / "config" / "cik_overrides.toml"
MAX_NAME_CANDIDATES = 5
GAP_DAYS = 200              # a stretch this long without any report = the CIK doesn't cover it
REPORT_LAG_DAYS = 100       # reports for the last quarter in the window are filed up to ~3 months later

SCHEMA = """
CREATE TABLE IF NOT EXISTS cik_map (          -- which SEC company number (CIK) to use for which dates
    ticker      VARCHAR NOT NULL,
    seq         INTEGER NOT NULL,             -- 1 = earliest
    cik         BIGINT  NOT NULL,
    entity_name VARCHAR,
    valid_from  DATE,                         -- NULL = from the start
    valid_to    DATE,                         -- NULL = to the end
    method      VARCHAR,                      -- 'override', 'sec ticker list', 'company name'
    filings     INTEGER,                      -- periodic reports filed in the window under this CIK
    facts_file  VARCHAR,
    PRIMARY KEY (ticker, seq)
);
CREATE TABLE IF NOT EXISTS cik_status (
    ticker      VARCHAR PRIMARY KEY,
    member_name VARCHAR,
    window_start DATE,
    window_end  DATE,
    filings_found INTEGER,
    filings_expected INTEGER,
    status      VARCHAR,       -- OK, ANNUAL ONLY, LOW COVERAGE, NOT FOUND, NO XBRL, NO SEC DATA
    note        VARCHAR,
    checked_at  TIMESTAMP
);
"""


def members(today):
    """{ticker: (company names, window start, window end)}: 1 year before joining (or the first price,
    if later - new listings and spin-offs have no earlier reports) to leaving."""
    tcon = connect(TECH_DB, read_only=True)
    rows = run(tcon, """SELECT m.ticker, string_agg(DISTINCT m.company, ' | '), min(m.start_date),
                               max(coalesce(m.end_date, ?)), any_value(p.first_price)
                        FROM index_membership m
                        LEFT JOIN (SELECT ticker, min(date) AS first_price FROM prices_daily GROUP BY ticker) p
                               USING (ticker)
                        GROUP BY m.ticker""", [today])
    tcon.close()
    out = {}
    for t, names, s, e, first_price in rows:
        w_start = s - dt.timedelta(days=366)
        if first_price is not None and first_price > w_start:
            w_start = first_price
        out[t] = (names or "", w_start, e)
    return out


def load_overrides():
    """{ticker: {"ciks": [cik, ...] in date order, "no_sec_data": bool, "note": str}}"""
    if not OVERRIDES_FILE.exists():
        return {}
    with open(OVERRIDES_FILE, "rb") as f:
        items = tomllib.load(f).get("cik", [])
    out = {}
    for i in items:
        o = out.setdefault(i["ticker"].upper(), {"ciks": [], "no_sec_data": False, "note": []})
        if i.get("no_sec_data"):
            o["no_sec_data"] = True
        elif i.get("cik"):
            o["ciks"].append(int(i["cik"]))
        if i.get("note"):
            o["note"].append(i["note"])
    for o in out.values():
        o["note"] = "; ".join(o["note"])
    return out


def _d(iso):
    return dt.date.fromisoformat(iso)


def match(ticker, names, w_start, w_end, ticker_list, name_list, overrides, http):
    """Choose the CIK(s) whose reports, by FILING date, cover the window. Returns (status row, map rows)."""
    lo, hi = w_start.isoformat(), (w_end + dt.timedelta(days=REPORT_LAG_DAYS)).isoformat()
    expected = max(1, round((w_end - w_start).days / 91.25))
    status = {"ticker": ticker, "member_name": names, "window_start": w_start, "window_end": w_end,
              "filings_found": 0, "filings_expected": expected, "status": "NOT FOUND", "note": ""}
    ov = overrides.get(ticker)
    if ov and ov["no_sec_data"]:
        status.update(status="NO SEC DATA", note="override: " + ov["note"])
        return status, []
    if ov and ov["ciks"]:
        cands = [(c, "override") for c in ov["ciks"]]
    else:
        cands = []
        if ticker in ticker_list:
            cands.append((ticker_list[ticker][0], "sec ticker list"))
        for n in names.split(" | "):
            for c in sf.name_candidates(name_list, n, MAX_NAME_CANDIDATES):
                if c not in {x for x, _ in cands}:
                    cands.append((c, "company name"))
    info, notes = {}, []
    for cik, method in cands:
        path, how = sf.download_facts(cik, http)
        if path is None:
            notes.append(f"CIK {cik} ({method}): {how}")
            continue
        facts = sf.load_facts(path)
        dates = sf.filed_dates(facts, lo, hi)
        info[cik] = {"cik": cik, "method": method, "entity_name": facts.get("entityName"), "dates": dates,
                     "facts_file": str(path.relative_to(ROOT))}
        notes.append(f"CIK {cik} {facts.get('entityName', '?')} ({method}): {len(dates)} reports filed in window")
    usable = {c: v for c, v in info.items() if v["dates"]}
    if not usable:
        status["status"] = "NO XBRL" if cands and not info else "NOT FOUND"
        status["note"] = "; ".join(notes) or "no candidate CIK (not in SEC's ticker or name lists)"
        return status, []

    if ov and ov["ciks"]:
        chain = [usable[c] for c in ov["ciks"] if c in usable]
    else:
        chain = [max(usable.values(), key=lambda v: (len(v["dates"]), v["method"] == "sec ticker list"))]
        others = [v for v in usable.values() if v is not chain[0]]
        first, last = _d(chain[0]["dates"][0][0]), _d(chain[0]["dates"][-1][0])
        if (first - w_start).days > GAP_DAYS and others:          # a predecessor company
            pre = max(others, key=lambda v: sum(1 for f, _ in v["dates"] if _d(f) < first))
            if any(_d(f) < first for f, _ in pre["dates"]):
                chain.insert(0, pre)
                others.remove(pre)
        if (w_end - last).days > GAP_DAYS and others:             # a successor company
            post = max(others, key=lambda v: sum(1 for f, _ in v["dates"] if _d(f) > last))
            if any(_d(f) > last for f, _ in post["dates"]):
                chain.append(post)
    chain.sort(key=lambda v: v["dates"][0][0])

    rows, found, all_dates = [], 0, []
    for i, v in enumerate(chain):
        valid_from = None if i == 0 else _d(v["dates"][0][0])
        valid_to = None if i == len(chain) - 1 else _d(chain[i + 1]["dates"][0][0]) - dt.timedelta(days=1)
        mine = [(f, form) for f, form in v["dates"] if (valid_from is None or _d(f) >= valid_from)
                and (valid_to is None or _d(f) <= valid_to)]
        rows.append({"ticker": ticker, "seq": i + 1, "cik": v["cik"], "entity_name": v["entity_name"],
                     "valid_from": valid_from, "valid_to": valid_to, "method": v["method"],
                     "filings": len(mine), "facts_file": v["facts_file"]})
        found += len(mine)
        all_dates += mine
    annual_only = all(form in sf.ANNUAL_FORMS for _, form in all_dates)
    days = [w_start] + sorted(_d(f) for f, _ in all_dates) + [w_end]
    longest_gap = max((b - a).days for a, b in zip(days, days[1:]))
    status["filings_found"] = found
    if annual_only:
        status["filings_expected"] = max(1, round(expected / 4))
        status["status"] = "ANNUAL ONLY" if longest_gap <= 400 + REPORT_LAG_DAYS else "LOW COVERAGE"
    else:
        status["status"] = "OK" if longest_gap <= GAP_DAYS and found >= 0.8 * expected else "LOW COVERAGE"
    status["note"] = (f"longest stretch without a report {longest_gap} days; " if status["status"] == "LOW COVERAGE"
                      else "") + "; ".join(notes)
    return status, rows


def save(con, status, rows):
    con.execute(SCHEMA).fetchall()
    run(con, "DELETE FROM cik_map WHERE ticker = ?", [status["ticker"]])
    run(con, "DELETE FROM cik_status WHERE ticker = ?", [status["ticker"]])
    for r in rows:
        run(con, "INSERT INTO cik_map VALUES (?,?,?,?,?,?,?,?,?)",
            [r["ticker"], r["seq"], r["cik"], r["entity_name"], r["valid_from"], r["valid_to"], r["method"],
             r["filings"], r["facts_file"]])
    run(con, "INSERT INTO cik_status VALUES (?,?,?,?,?,?,?,?,current_timestamp)",
        [status["ticker"], status["member_name"], status["window_start"], status["window_end"],
         status["filings_found"], status["filings_expected"], status["status"], status["note"][:2000]])


BUILD_SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_facts (
    ticker VARCHAR, metric VARCHAR, prio INTEGER, concept VARCHAR, unit VARCHAR, start DATE, "end" DATE,
    val DOUBLE, filed DATE, accn VARCHAR, form VARCHAR, cik BIGINT
);
CREATE TABLE IF NOT EXISTS fund_snapshots (
    ticker VARCHAR, filed DATE, cik BIGINT, form VARCHAR, accn VARCHAR, currency VARCHAR, annual_only BOOLEAN,
    period_end DATE, revenue_ttm DOUBLE, revenue_q DOUBLE, revenue_q_year_ago DOUBLE, revenue_ttm_year_ago DOUBLE,
    revenue_growth_q_yoy DOUBLE, revenue_growth_ttm_yoy DOUBLE, op_income_ttm DOUBLE, op_margin_ttm DOUBLE,
    net_income_ttm DOUBLE, eps_diluted_ttm DOUBLE, eps_source VARCHAR, ttm_has_derived_quarter BOOLEAN,
    balance_sheet_date DATE, equity DOUBLE, total_debt DOUBLE, debt_to_equity DOUBLE,
    revenue_q_derived BOOLEAN, revenue_tag VARCHAR, revenue_ttm_how VARCHAR, debt_note VARCHAR
);
CREATE TABLE IF NOT EXISTS fund_build (ticker VARCHAR PRIMARY KEY, signature VARCHAR, facts INTEGER,
                                       snapshots INTEGER, built_at TIMESTAMP);
"""
SNAP_COLS = ["ticker", "filed", "cik", "form", "accn", "currency", "annual_only", "period_end", "revenue_ttm",
             "revenue_q", "revenue_q_year_ago", "revenue_ttm_year_ago", "revenue_growth_q_yoy",
             "revenue_growth_ttm_yoy", "op_income_ttm", "op_margin_ttm", "net_income_ttm", "eps_diluted_ttm",
             "eps_source", "ttm_has_derived_quarter", "balance_sheet_date", "equity", "total_debt", "debt_to_equity",
             "revenue_q_derived", "revenue_tag", "revenue_ttm_how", "debt_note"]
FIRST_SNAPSHOT = dt.date(2013, 6, 1)        # the earliest window starts Nov 2014; older filings only feed TTM


_SPLITS = {}


def stock_splits(ticker):
    """Real share splits of the stock (from the price database), for restating EPS."""
    if not _SPLITS:
        tcon = connect(TECH_DB, read_only=True)
        for t, d, v in run(tcon, "SELECT ticker, date, value FROM corporate_actions WHERE action = 'split'"):
            _SPLITS.setdefault(t, []).append((d, v))
        tcon.close()
        _SPLITS.setdefault("__loaded__", [])
    return fu.real_splits(_SPLITS.get(ticker, []))


def build(con, ticker, force=False):
    """(Re)build fund_facts and fund_snapshots for one stock from its saved company-facts file(s)."""
    rows = run(con, "SELECT seq, cik, valid_from, valid_to, facts_file FROM cik_map WHERE ticker = ? ORDER BY seq",
               [ticker])
    if not rows:
        return None
    sig = f"v{fu.VERSION}|" + "|".join(f"{c}:{vf}:{vt}:{ff}" for _, c, vf, vt, ff in rows) + "|splits:" + ",".join(
        f"{d}:{r:g}" for d, r in stock_splits(ticker))
    old = run(con, "SELECT signature FROM fund_build WHERE ticker = ?", [ticker])
    if old and old[0][0] == sig and not force:
        return "unchanged"
    parts = [fu.extract(sf.load_facts(ROOT / ff), c, vf, vt) for _, c, vf, vt, ff in rows]
    splits = stock_splits(ticker)
    df = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame()
    snaps = pd.DataFrame()
    if len(df):
        snaps = fu.snapshots(df, min_filed=FIRST_SNAPSHOT, splits=splits)
    con.begin()
    run(con, "DELETE FROM fund_facts WHERE ticker = ?", [ticker])
    run(con, "DELETE FROM fund_snapshots WHERE ticker = ?", [ticker])
    if len(df):
        con.register("f_new", df.assign(ticker=ticker))
        run(con, """INSERT INTO fund_facts SELECT ticker, metric, prio, concept, unit, start, "end", val, filed,
                    accn, form, cik FROM f_new""")
        con.unregister("f_new")
    if len(snaps):
        snaps = snaps.assign(ticker=ticker)
        for c in SNAP_COLS:
            if c not in snaps:
                snaps[c] = None
        con.register("s_new", snaps[SNAP_COLS])
        run(con, "INSERT INTO fund_snapshots SELECT * FROM s_new")
        con.unregister("s_new")
    run(con, "INSERT OR REPLACE INTO fund_build VALUES (?, ?, ?, ?, current_timestamp)",
        [ticker, sig, len(df), len(snaps)])
    con.commit()
    return f"{len(df):,} figures, {len(snaps)} snapshots"


def build_all(con, tickers, force=False):
    cols = {r[0] for r in run(con, "SELECT column_name FROM information_schema.columns "
                                   "WHERE table_name = 'fund_snapshots'")}
    if cols and "debt_note" not in cols:                # older table layout: rebuild everything
        run(con, "DROP TABLE fund_snapshots")
        run(con, "DELETE FROM fund_build")
    con.execute(BUILD_SCHEMA).fetchall()
    mapped = [t for t in tickers if run(con, "SELECT 1 FROM cik_map WHERE ticker = ? LIMIT 1", [t])]
    print(f"\nBuilding point-in-time fundamentals for {len(mapped)} stocks ...")
    start, n_new = time.time(), 0
    try:
        for i, t in enumerate(mapped, 1):
            res = build(con, t, force)
            if res != "unchanged":
                n_new += 1
                left = (time.time() - start) / i * (len(mapped) - i) / 60
                print(f"[{i}/{len(mapped)}] {t:<8} {res}  (about {left:.0f} min left)")
    except KeyboardInterrupt:
        print("\nStopped. Finished stocks are saved - run again to continue.")
    n = run(con, "SELECT count(DISTINCT ticker), count(*) FROM fund_snapshots")[0]
    print(f"Built {n_new} stock(s) now; fundamentals table: {n[0]} stocks, {n[1]:,} snapshots")


def report(con, only=None):
    st = con.execute("SELECT * FROM cik_status ORDER BY status, ticker").df()
    mp = con.execute("SELECT * FROM cik_map ORDER BY ticker, seq").df()
    if only is not None:
        st = st[st["ticker"].isin(only)]
        mp = mp[mp["ticker"].isin(only)]
    print(f"\nSEC MATCHING  {len(st)} stocks: " + ", ".join(f"{k} {v}" for k, v in st["status"].value_counts().items()))
    print("  matched by: " + ", ".join(f"{k} {v}" for k, v in mp.drop_duplicates("ticker")["method"].value_counts().items()))
    for status in ("NOT FOUND", "NO XBRL", "LOW COVERAGE", "ANNUAL ONLY", "NO SEC DATA"):
        sub = st[st["status"] == status]
        if len(sub):
            print(f"\n{status} ({len(sub)}):")
            for _, r in sub.iterrows():
                print(f"  {r['ticker']:<8} {str(r['member_name'])[:34]:<34} {r['filings_found']}/{r['filings_expected']} "
                      f"reports  {str(r['note'])[:170]}")
    chained = mp[mp["ticker"].isin(mp[mp["seq"] > 1]["ticker"])]
    if len(chained):
        print(f"\nMore than one SEC company number over time ({chained['ticker'].nunique()}) - please glance over:")
        for t, g in chained.groupby("ticker"):
            print(f"  {t:<8} " + "  ->  ".join(
                f"CIK {r['cik']} {str(r['entity_name'])[:28]}"
                + (f" from {str(r['valid_from'])[:10]}" if pd.notna(r['valid_from']) else "") for _, r in g.iterrows()))
    ov = mp[(mp["method"] == "override") & ~mp["ticker"].isin(chained["ticker"])]
    if len(ov):
        print(f"\nFrom config/cik_overrides.toml - check the SEC name is the right company ({len(ov)}):")
        for _, r in ov.iterrows():
            print(f"  {r['ticker']:<8} CIK {r['cik']:<9} {r['entity_name']}  ({r['filings']} reports)")
    named = mp[(mp["method"] == "company name") & ~mp["ticker"].isin(chained["ticker"])]
    if len(named):
        print(f"\nMatched by company name - please glance over ({len(named)}):")
        names = dict(zip(st["ticker"], st["member_name"]))
        for _, r in named.iterrows():
            print(f"  {r['ticker']:<8} {str(names.get(r['ticker']))[:38]:<38} -> CIK {r['cik']} {r['entity_name']}")
    out = EXPORTS_DIR / "fundamentals" / "cik_map.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        st.to_excel(xw, sheet_name="status", index=False)
        mp.to_excel(xw, sheet_name="cik_map", index=False)
    print(f"\nsaved: {out.relative_to(ROOT)}")


def main(args=None, http=None, today=None):
    import requests
    http = http or requests.get
    args = sys.argv[1:] if args is None else list(args)
    redo = "--redo" in args
    picked = [a.upper() for a in args if not a.startswith("--")]
    today = today or dt.date.today()
    mem = members(today)
    tickers = picked or sorted(mem)
    unknown = [t for t in tickers if t not in mem]
    if unknown:
        print(f"Not in the membership table (skipped): {', '.join(unknown)}")
    tickers = [t for t in tickers if t in mem]

    con = connect(FUND_DB)
    old = {r[0] for r in run(con, "SELECT column_name FROM information_schema.columns WHERE table_name = 'cik_map'")}
    if "status" in old:                                  # first version of the table - rebuilt
        run(con, "DROP TABLE cik_map")
    con.execute(SCHEMA).fetchall()
    done = {t for (t,) in run(con, "SELECT ticker FROM cik_status WHERE status IN ('OK', 'ANNUAL ONLY', 'NO SEC DATA')")}
    todo = [t for t in tickers if redo or picked or t not in done]
    # stocks whose entry in config/cik_overrides.toml was added or changed since they were matched
    ovr = load_overrides()
    stored = {}
    for t, c in run(con, "SELECT ticker, cik FROM cik_map"):
        stored.setdefault(t, set()).add(int(c))
    changed = [t for t in tickers if t in ovr and t not in todo
               and ((ovr[t]["no_sec_data"] and t in stored)
                    or (not ovr[t]["no_sec_data"] and not stored.get(t, set()) <= set(ovr[t]["ciks"])))]
    if changed:
        print(f"Changed in config/cik_overrides.toml - matched again: {', '.join(changed)}")
        todo += changed
    print(f"{len(tickers)} stocks; already matched {len(tickers) - len(todo)}; to do {len(todo)}")
    if not todo:
        report(con, tickers if picked else None)
        build_all(con, tickers, force="--rebuild" in args)
        con.close()
        return
    print("Loading SEC ticker and company-name lists ...")
    ticker_list, name_list, overrides = sf.current_tickers(http), sf.all_names(http), load_overrides()
    start = time.time()
    try:
        for i, t in enumerate(todo, 1):
            names, w_start, e = mem[t]
            st, rows = match(t, names, w_start, e, ticker_list, name_list, overrides, http)
            save(con, st, rows)
            left = (time.time() - start) / i * (len(todo) - i) / 60
            ciks = " + ".join(str(r["cik"]) for r in rows) or "-"
            name = rows[-1]["entity_name"] if rows else ""
            print(f"[{i}/{len(todo)}] {t:<8} {st['status']:<12} CIK {ciks:<18} {str(name)[:30]:<30} "
                  f"{st['filings_found']}/{st['filings_expected']} reports  (about {left:.0f} min left)")
    except KeyboardInterrupt:
        print("\nStopped. Everything finished so far is saved - run the same command again to continue.")
    report(con, tickers if picked else None)
    build_all(con, tickers, force="--rebuild" in args)
    con.close()


if __name__ == "__main__":
    main()
