"""
Step 11 - daily update: bring both databases up to the last completed trading day, with checks.

    python scripts/daily_update.py                 # everything that is out of date
    python scripts/daily_update.py --no-fundamentals --no-membership   # prices + technicals only
    ./start.sh                                     # this update, then the app

The last completed trading day is today after 6 pm New York time, otherwise the session before
(src/update_status.py). Each step only does what is missing, so running it twice is quick.

 1. index lists        build_membership.py (NASDAQ-100, S&P 500, S&P 100) - once a day
 2. prices             every current index member + the regime ETF (QQQ) + tickers.txt whose
                       newest price is older than the last session; new members without any prices
                       are loaded with load_all_prices.py (checks they cover their index days)
 3. price checks       on every new day: a row for the last session, low <= open/close <= high,
                       prices > 0, moves over 40% in a day flagged; a stock whose whole history was
                       reloaded (new split or dividend) gets the full price audit (offline checks)
 4. technicals         52-week values, 6-month low, support/resistance for every stock that changed,
                       then the technicals checks
 5. fundamentals       SEC's daily filing index since the last update -> company facts downloaded
                       again only for members that filed a 10-Q/10-K/20-F/40-F, those stocks rebuilt;
                       new members are matched to their SEC company first (load_fundamentals.py)
 5b. market cap        Yahoo market cap of every current member (once a day; saved raw under
                       data/raw/yahoo/info/) - only for the screener's size column and sort
 6. screen             exports/screens/screen_dip_buyer_<day>.xlsx for the last session
Ends with a summary, saved in logs/update/ and in the update_log table (shown on the app's start page).
"""
import datetime as dt
import gzip
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from src.common import ROOT, TECH_DB, FUND_DB, connect, run, read_tickers  # noqa: E402
from src import update_status as us  # noqa: E402
from src.calculate import technicals, support_resistance  # noqa: E402
from src.checks import technicals_audit  # noqa: E402
from src.collect import sec_facts as sf  # noqa: E402
from src.strategy import engine as en  # noqa: E402

LOG_DIR = ROOT / "logs" / "update"
DAILY_INDEX_DIR = sf.SEC_DIR / "daily_index"
FILING_FORMS = {"10-Q", "10-K", "20-F", "40-F", "10-QT", "10-KT"}
BIG_MOVE = 0.40
FILING_LOOKBACK_DAYS = 10          # filings re-checked each run (SEC's facts file can lag a filing by a day)

UPDATE_LOG = """
CREATE TABLE IF NOT EXISTS update_log (
    run_id VARCHAR, run_at TIMESTAMP, target_session DATE, status VARCHAR, summary VARCHAR
)"""


class Report:
    def __init__(self):
        self.lines, self.problems, self.notes = [], [], []

    def say(self, text=""):
        print(text, flush=True)
        self.lines.append(text)

    def problem(self, text):
        self.say(f"  [REVIEW] {text}")
        self.problems.append(text)

    def note(self, text):
        self.say(f"  [INFO]   {text}")
        self.notes.append(text)


# ------------------------------------------------------------------ 1. index lists
def update_membership(rep, today):
    con = connect(TECH_DB, read_only=True)
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'index_snapshots'")[0][0]
    done = run(con, "SELECT count(DISTINCT index_name) FROM index_snapshots WHERE snap_date = ?", [today])[0][0] \
        if has else 0
    con.close()
    if done >= 3:
        rep.say("1. index lists: already saved today - skipped")
        return
    rep.say("1. index lists: rebuilding (python scripts/build_membership.py)")
    from scripts import build_membership
    try:
        statuses = build_membership.main([], today=today) or {}
    except Exception as e:
        rep.problem(f"index lists not rebuilt: {type(e).__name__}: {e} - yesterday's lists are used")
        return
    for k, s in statuses.items():
        (rep.note if s == "VERIFIED" else rep.problem)(f"index list {k}: {s}")


# ------------------------------------------------------------------ 2-3. prices and their checks
def price_targets(con):
    tickers = set(us.current_members(con))
    for name in ("dip_buyer",):
        try:
            tickers.add(en.load_strategy(name).get("regime_ticker", "QQQ"))
        except Exception:
            tickers.add("QQQ")
    tickers.add("QQQ")
    try:
        tickers |= set(read_tickers())
    except Exception:
        pass
    return sorted(tickers)


def check_new_rows(con, ticker, since, target, rep):
    """Checks on the days added since `since` (exclusive)."""
    rows = run(con, """SELECT date, open, high, low, close, volume,
                              lag(close) OVER (ORDER BY date) AS prev
                       FROM prices_daily WHERE ticker = ? ORDER BY date""", [ticker])
    new = [r for r in rows if since is None or r[0] > since]
    bad = []
    for d, o, h, lo, c, v, prev in new:
        if None in (o, h, lo, c) or min(o, h, lo, c) <= 0:
            bad.append(f"{d} missing or non-positive price")
        elif lo > min(o, c) * 1.0001 or h < max(o, c) * 0.9999:
            bad.append(f"{d} low/high do not contain open/close (O {o} H {h} L {lo} C {c})")
        if prev and c and abs(c / prev - 1) > BIG_MOVE:
            bad.append(f"{d} moved {c / prev - 1:+.0%} in one day (check for a split or bad print)")
    for b in bad:
        rep.problem(f"{ticker}: {b}")
    return not bad


def update_prices_step(rep, target):
    from scripts import update_prices, load_all_prices
    con = connect(TECH_DB)
    update_prices.create_technicals(con)
    update_prices.corrections.sync_manual(con)
    tickers = price_targets(con)
    last = dict(run(con, "SELECT ticker, max(date) FROM prices_daily GROUP BY 1"))
    behind = [t for t in tickers if t in last and last[t] < target]
    new = [t for t in tickers if t not in last]
    rep.say(f"2. prices: {len(tickers)} stocks/ETFs tracked; up to date {len(tickers) - len(behind) - len(new)}; "
            f"to update {len(behind)}; new (no prices yet) {len(new)}")
    run_id = uuid.uuid4().hex[:8]
    changed, failed = [], []
    t0 = time.time()
    for i, t in enumerate(behind, 1):
        res = update_prices.update_ticker(con, t, run_id)
        (changed if res == "OK" else failed).append(t)
        if i % 50 == 0:
            print(f"   ... {i}/{len(behind)} ({(time.time() - t0) / i * (len(behind) - i) / 60:.0f} min left)",
                  flush=True)
    reloaded = [r[0] for r in run(con, """SELECT DISTINCT ticker FROM load_log
                                         WHERE run_id = ? AND mode = 'full reload' AND status = 'OK'""", [run_id])]
    con.close()
    con = connect(TECH_DB)
    members = set(us.current_members(con))
    con.close()
    new_members, new_other = [t for t in new if t in members], [t for t in new if t not in members]
    if new_members:
        rep.say(f"   new members: loading full history for {', '.join(new_members)} (load_all_prices.py)")
        try:
            load_all_prices.main(new_members)
            changed += new_members
            reloaded += new_members
        except Exception as e:
            rep.problem(f"new members not loaded: {type(e).__name__}: {e}")
    con = connect(TECH_DB)
    for t in new_other:                                 # e.g. a ticker you added to tickers.txt
        if update_prices.update_ticker(con, t, run_id) == "OK":
            changed.append(t)
            reloaded.append(t)
        else:
            failed.append(t)
    for t in failed:
        rep.problem(f"{t}: price download failed (kept the stored prices to {last.get(t)})")
    # 3. checks
    rep.say(f"3. price checks on the new days of {len(changed)} stocks")
    n_ok = 0
    for t in changed:
        n_ok += check_new_rows(con, t, last.get(t) if t not in reloaded else None, target, rep)
    target_, newest, still = us.price_status(con, target)
    if still:
        rep.problem(f"{len(still)} member(s) have no price for {target} yet: {', '.join(still[:20])}"
                    + (" ..." if len(still) > 20 else "") + " (Yahoo may not have it yet - run again later)")
    con.close()
    if reloaded:
        rep.say(f"   full price audit (offline) for stocks whose history was reloaded: {', '.join(reloaded)}")
        from scripts import audit_prices
        try:
            res = audit_prices.main(reloaded + ["--offline"], quiet=True) or {}
            for t, st in (res.items() if isinstance(res, dict) else []):
                if st != "VERIFIED":
                    rep.problem(f"{t}: price audit {st} - see logs/audit/ and exports/audit/")
        except Exception as e:
            rep.problem(f"price audit not run: {type(e).__name__}: {e}")
    rep.say(f"   checks passed for {n_ok} of {len(changed)} stocks")
    return changed


# ------------------------------------------------------------------ 4. technicals
def technicals_step(rep, changed):
    rep.say(f"4. technicals + support/resistance for {len(changed)} stocks")
    con = connect(TECH_DB)
    bad = 0
    for t in changed:
        try:
            technicals.calculate(con, t)
            support_resistance.calculate(con, t)
            results, _ = technicals_audit.check(con, t)
            fails = [f"{n}: {d}" for n, s, d in results if s == "FAIL"]
            if fails:
                bad += 1
                rep.problem(f"{t}: technicals check failed - {'; '.join(fails)[:300]}")
        except Exception as e:
            bad += 1
            rep.problem(f"{t}: technicals not calculated: {type(e).__name__}: {e}")
    con.close()
    rep.say(f"   done; problems {bad}")


# ------------------------------------------------------------------ 5. fundamentals
def daily_index(day, http):
    """[(form, cik, date filed, accession)] from SEC's daily form index (kept once the day is over)."""
    f = DAILY_INDEX_DIR / f"form.{day:%Y%m%d}.idx"
    if f.exists():
        text = f.read_text(errors="replace")
    else:
        q = (day.month - 1) // 3 + 1
        r = sf._get(f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/form.{day:%Y%m%d}.idx", http)
        if r.status_code in (403, 404):
            return []                                   # weekend / holiday / not published yet
        r.raise_for_status()
        text = r.text
        if day < dt.date.today():
            DAILY_INDEX_DIR.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
    out = []
    for line in text.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5 or not parts[-1].startswith("edgar/"):
            continue
        form = parts[0].strip().upper()
        if form.split("/")[0] not in FILING_FORMS:
            continue
        m = re.search(r"(\d{10}-\d{2}-\d{6})", parts[-1])
        if m and parts[-3].isdigit():
            out.append((form, int(parts[-3]), parts[-2], m.group(1)))
    return out


def _accessions(path):
    with gzip.open(path, "rb") as g:
        facts = json.load(g)
    acc = set()
    for tax in (facts.get("facts") or {}).values():
        for tag in tax.values():
            for rows in tag.get("units", {}).values():
                acc.update(r.get("accn") for r in rows)
    return acc


def fundamentals_step(rep, today, http=requests.get):
    from scripts import load_fundamentals as lf
    tcon = connect(TECH_DB, read_only=True)
    members = us.current_members(tcon)
    tcon.close()
    fcon = connect(FUND_DB)
    have = {r[0] for r in run(fcon, "SELECT ticker FROM cik_status")}
    fcon.close()
    new = [t for t in members if t not in have]
    if new:
        rep.say(f"5. fundamentals: matching new members to their SEC company: {', '.join(new)}")
        try:
            lf.main(new, http=http)
        except Exception as e:
            rep.problem(f"new members not matched: {type(e).__name__}: {e}")
    fcon = connect(FUND_DB)
    ciks = {}
    for t, c in run(fcon, "SELECT ticker, cik FROM cik_map WHERE valid_to IS NULL OR valid_to >= ?", [today]):
        if t in members:
            ciks.setdefault(int(c), set()).add(t)
    filings = []
    for k in range(FILING_LOOKBACK_DAYS, -1, -1):
        day = today - dt.timedelta(days=k)
        if day.weekday() >= 5:
            continue
        try:
            filings += [f for f in daily_index(day, http) if f[1] in ciks]
        except Exception as e:
            rep.note(f"SEC filing index for {day} not read: {type(e).__name__}: {e}")
    rep.say(f"5. fundamentals: {len(filings)} report(s) filed by current members in the last "
            f"{FILING_LOOKBACK_DAYS} days")
    to_build, waiting = set(), []
    for cik in sorted({f[1] for f in filings}):
        accs = {f[3] for f in filings if f[1] == cik}
        path = sf.facts_file(cik)
        known = _accessions(path) if path else set()
        if accs <= known:
            continue                                    # already in the saved facts file
        try:
            path, how = sf.download_facts(cik, http, max_age_days=0)
        except Exception as e:
            rep.problem(f"CIK {cik} ({', '.join(sorted(ciks[cik]))}): company facts not downloaded: {e}")
            continue
        if path is None:
            continue
        rel = str(path.relative_to(ROOT))
        run(fcon, "UPDATE cik_map SET facts_file = ? WHERE cik = ?", [rel, cik])
        to_build |= ciks[cik]
        missing = accs - _accessions(path)
        if missing:
            waiting.append(f"{', '.join(sorted(ciks[cik]))} ({', '.join(sorted(missing))})")
    if waiting:
        rep.note("SEC has not added these new reports to the company facts yet - checked again next run: "
                 + "; ".join(waiting))
    if to_build:
        rep.say(f"   rebuilding fundamentals for {', '.join(sorted(to_build))}")
        lf.build_all(fcon, sorted(to_build))
    else:
        rep.say("   no new reports to add")
    fcon.close()


# ------------------------------------------------------------------ 5b. market cap
def market_cap_step(rep):
    """Yahoo market cap for the screener's size column/sort (once a day; display only)."""
    from src.collect import market_cap as mcap
    tcon = connect(TECH_DB, read_only=True)
    members = us.current_members(tcon)
    tcon.close()
    n, failed = mcap.fetch(members)
    tcon = connect(TECH_DB)
    rows = mcap.load_table(tcon)
    have = {r[0] for r in run(tcon, "SELECT ticker FROM market_cap")}
    off = mcap.check_prices(tcon, set(members))
    tcon.close()
    missing = sorted(set(members) - have)
    rep.say(f"5b. market cap: {n} downloaded from Yahoo; {rows} in the table; members without one: {len(missing)}")
    if failed:
        rep.note(f"Yahoo gave no usable market cap for {len(failed)}: " + "; ".join(failed[:10]))
    if len(missing) > 0.1 * max(len(members), 1):
        rep.problem(f"{len(missing)} members have no market cap (Yahoo down or rate-limited?) - "
                    "run python scripts/load_market_cap.py --fetch later")
    for t, ours, y in off[:10]:
        rep.note(f"{t}: Yahoo price {y:.2f} vs our close {ours:.2f} when downloaded")


# ------------------------------------------------------------------ 6. screen
def screen_step(rep, target):
    from scripts import screen
    rep.say(f"6. screen for {target}")
    try:
        screen.main([target.isoformat()])
    except Exception as e:
        rep.problem(f"screen not written: {type(e).__name__}: {e}")


def main(args=None, now=None):
    args = sys.argv[1:] if args is None else list(args)
    rep = Report()
    run_id = uuid.uuid4().hex[:8]
    t0 = time.time()
    target = us.expected_session(now)
    today = dt.date.today()
    con = connect(TECH_DB, read_only=True)
    _, newest, behind = us.price_status(con, target)
    con.close()
    rep.say(f"DAILY UPDATE  run {run_id}  {dt.datetime.now():%Y-%m-%d %H:%M}")
    rep.say(f"last completed trading day: {target}; newest stored price: {newest}; "
            f"members behind: {len(behind)}")
    rep.say()
    if "--no-membership" not in args:
        update_membership(rep, today)
    changed = update_prices_step(rep, target)
    if changed:
        technicals_step(rep, changed)
    else:
        rep.say("4. technicals: nothing changed - skipped")
    if "--no-fundamentals" not in args:
        try:
            fundamentals_step(rep, today)
        except Exception as e:
            rep.problem(f"fundamentals not updated: {type(e).__name__}: {e}")
    if "--no-market-cap" not in args:
        try:
            market_cap_step(rep)
        except Exception as e:
            rep.problem(f"market cap not updated: {type(e).__name__}: {e}")
    if "--no-screen" not in args:
        screen_step(rep, target)
    status = "NEEDS REVIEW" if rep.problems else "OK"
    rep.say()
    rep.say(f"UPDATE {status}: data up to {target}; {len(rep.problems)} item(s) to review; "
            f"{(time.time() - t0) / 60:.1f} min")
    for p in rep.problems[:30]:
        rep.say(f"  - {p}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"update_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    log.write_text("\n".join(rep.lines))
    print(f"saved: {log.relative_to(ROOT)}")
    con = connect(TECH_DB)
    con.execute(UPDATE_LOG).fetchall()
    summary = (f"{len(rep.problems)} item(s) to review" + (": " + "; ".join(rep.problems[:5]) if rep.problems else ""))
    run(con, "INSERT INTO update_log VALUES (?, current_timestamp, ?, ?, ?)", [run_id, target, status, summary[:2000]])
    con.close()
    return status


if __name__ == "__main__":
    main()
