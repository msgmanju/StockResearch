"""
Build the S&P 100 history and run its audit checks (called by scripts/build_membership.py SP100).
Method and sources: src/sp100_history.py, src/collect/sp100_sources.py, docs/MEMBERSHIP_PROCEDURE.md section 8.

Lists of members on known days, best source first:
  OEF's SEC N-PORT holdings (quarter-ends from Sep 2019; CUSIPs -> tickers)
  OEF's SEC reports 2015-2019 (N-Q, N-CSR, N-CSRS: quarter-ends; company names -> tickers)
  Wikipedia's S&P 100 page at a quarter-end: the independent check (used as a list only at the build start,
  if no fund report covers it - Wikipedia was often edited weeks or months late)
  today's iShares OEF list and the lists saved by earlier runs
"""
import pandas as pd
import requests

from src.common import run
from src import membership as mb
from src import sp100_history as h
from src.collect import sp100_sources as src_
from src.collect import membership_sources as ms
from src.collect import cusip_map, sec_nport
from src.checks.price_audit import load_reviewed

KEY = "SP100"
RESIDUAL = 0.0005          # an OEF position under 0.05% of the fund is not an index stock (leftover/odd lot)
TIMING_SESSIONS = 10       # Wikipedia is often edited days or weeks after a change


def _session_on_or_before(cal, d):
    return max((s for s in cal if s <= d), default=None)


def _quarter_ends(start, end):
    out, d = [], pd.Timestamp(start) + pd.offsets.QuarterEnd(0)
    while d.date() <= end:
        out.append(d.date())
        d += pd.offsets.QuarterEnd(1)
    return out


class Resolver:
    """Tickers and company names from any source -> today's ticker of an S&P 500 member on that day."""

    def __init__(self, sp500, cal, renames):
        self.sp, self.cal, self.renames = sp500, cal, renames
        self.splits = mb.load_splits()                      # FOXA before 2019-03-19 = 21st Century Fox (TFCFA)
        self.taliases = h.load_ticker_aliases()             # UTX -> RTX, PCLN -> BKNG ...
        self.at_time = [(r.ticker_at_time, r.ticker, r.start_date, r.end_date) for r in sp500.itertuples()
                        if r.ticker_at_time]                # DD -> DD-OLD before Sep 2017
        self.names = {}                                     # name key -> {tickers}
        for r in sp500.itertuples():
            self.add_name(r.company, r.ticker)
        for n, t in h.load_name_aliases():
            self.add_name(n, t)
        self._m = {}

    def members(self, day):
        day = max(day, self.cal[0])
        if day not in self._m:
            m = self.sp[(self.sp["start_date"] <= day) & (self.sp["end_date"].isna() | (self.sp["end_date"] >= day))]
            self._m[day] = dict(zip(m["ticker"], m["company"]))
        return self._m[day]

    def add_name(self, name, ticker):
        if name and ticker:
            self.names.setdefault(h.name_key(name), set()).add(ticker)

    def ticker(self, t, day):
        """(today's ticker or None, note)"""
        mem = self.members(day)
        cands = [mb.to_current_ticker(t, day, self.renames)]
        cands += [new for old, new, until in self.taliases if old == t and day < until]
        cands += [cur for old, cur, s, e in self.at_time if old == t and s <= max(day, self.cal[0]) and (e is None or pd.isna(e) or day <= e)]
        cands += [x["old_ticker"] for x in self.splits if x["ticker"] == t and day < x["before"]]
        for c in cands:
            if c in mem:
                return c, ("" if c == t else f"{t} -> {c}")
        return None, ""

    def left_before(self, t, day):
        """The S&P 500 exit date if this ticker had already left the S&P 500 by `day` (a stale list)."""
        cands = {mb.to_current_ticker(t, day, self.renames), t}
        ends = self.sp[self.sp["ticker"].isin(cands) & self.sp["end_date"].notna()]["end_date"]
        ends = [e for e in ends if e < day]
        return max(ends) if ends else None

    def name(self, n, day):
        mem = self.members(day)
        cands = {t for t in self.names.get(h.name_key(n), ()) if t in mem}
        if not cands:
            hits = [t for t, c in mem.items() if c and mb.similar_names(h.clean_name(n), c)]
            exact = [t for t in hits if mb._norm_name(mem[t]) == mb._norm_name(h.clean_name(n))]
            cands = set(exact or hits)
        if len(cands) > 1:
            cls = h.class_letter(n)
            by_cls = {t for t in cands if cls and h.class_letter(mem[t]) == cls}
            cands = by_cls if len(by_cls) == 1 else ({h.pick_share_class(cands, cls)} - {None} or cands)
        return next(iter(cands)) if len(cands) == 1 else None


def build_sp100(con, cfg, today, http=None, http_get=None, http_post=None):
    get = http or requests.get
    sec_get = http_get or requests.get
    kw_post = {"http": http_post} if http_post else {}
    results, issues = [], []

    def add(check, status, n, detail):
        results.append({"check": check, "status": status, "n_items": n, "detail": detail})

    cal = mb.sessions(mb.START, today)
    renames = mb.load_ticker_changes()
    sp500 = con.execute("""SELECT ticker, ticker_at_time, company, start_date, end_date, start_known
                           FROM index_membership WHERE index_name = 'SP500'""").df()
    if sp500.empty:
        raise RuntimeError("SP100: build the S&P 500 first (python scripts/build_membership.py SP500) - "
                           "the S&P 100 is checked against it")
    for c in ("start_date", "end_date"):
        sp500[c] = pd.to_datetime(sp500[c]).dt.date
    sp500["end_date"] = sp500["end_date"].where(sp500["end_date"].notna(), None)
    sp500["ticker_at_time"] = sp500["ticker_at_time"].where(sp500["ticker_at_time"].notna(), None)
    R = Resolver(sp500, cal, renames)

    # ---------------------------------------------------------------- 1. today
    try:
        official, _ = src_.oef_current(http=get)
        official = official.assign(ticker=ms.fix_share_class(official["ticker"], list(sp500["ticker"])))
        if len(official) < cfg["min"] * 0.9:
            raise ValueError(f"only {len(official)} tickers returned")
        add("official_list", "PASS", len(official), f"iShares OEF: {len(official)} stocks")
    except Exception as e:
        official = None
        add("official_list", "FAIL", 0, f"iShares OEF list could not be used: {type(e).__name__}: {e}")
    try:
        _, wiki_today = src_.wiki_list_on(today, http=get)
    except Exception as e:
        wiki_today = None
        add("wikipedia", "REVIEW", 0, f"Wikipedia S&P 100 page not read: {type(e).__name__}: {e}")
    if official is not None and wiki_today:
        official = official.assign(ticker=ms.fix_share_class(official["ticker"], list(wiki_today)))
        a, b = set(official["ticker"]), set(wiki_today)
        issues += h.compare_lists(today, a, b, "Wikipedia's list today", check="sources_today")
        add("sources_today", "PASS" if a == b else "REVIEW", len(a | b),
            f"iShares OEF {len(a)} vs Wikipedia {len(b)}; differences {len(a ^ b)}")
    if official is not None:
        mb.save_snapshot(con, KEY, today, official, "iShares OEF")

    # ---------------------------------------------------------------- 2. OEF N-PORT (CUSIPs)
    snaps, nport_days = [], []
    try:
        hold = src_.nport_holdings(http=sec_get)
    except Exception as e:
        hold = pd.DataFrame()
        add("oef_filings", "REVIEW", 0, f"OEF SEC N-PORT filings not read: {type(e).__name__}: {str(e)[:200]}")
    if len(hold):
        hold["cusip"] = [sec_nport.security_id(c, i) for c, i in zip(hold["cusip"], hold["isin"])]
        tick = cusip_map.lookup(con, hold["cusip"].unique(), **kw_post)
        hold["ticker"] = hold["cusip"].map(tick)
        for period in sorted(hold["period"].unique()):
            day = _session_on_or_before(cal, period)
            if day is None:
                continue
            q = hold[hold["period"] == period]
            q = q[q["value_usd"].fillna(0) >= RESIDUAL * q["value_usd"].sum()]
            got = {}
            for t, n in zip(q["ticker"], q["name"]):
                tt = R.ticker(t, day)[0] if isinstance(t, str) else None
                if tt is None:
                    tt = R.name(n, day)
                    if tt:
                        issues.append(("sp100_mapping", day, tt, "INFO", f"OEF N-PORT: '{n}' (CUSIP not mapped) matched by name"))
                if tt:
                    got[tt] = n
                    R.add_name(n, tt)
                else:
                    issues.append(("sp100_mapping", day, t if isinstance(t, str) else "", "REVIEW",
                                   f"OEF N-PORT: '{n}' is not an S&P 500 member that day in our history - left out"))
            snaps.append((day, got, f"OEF N-PORT {period}"))
            nport_days.append(day)
        add("oef_filings", "PASS", len(nport_days),
            f"{len(nport_days)} OEF quarter-end holdings (SEC N-PORT) {nport_days[0]} to {nport_days[-1]}")
    first_nport = nport_days[0] if nport_days else cal[-1]

    # ---------------------------------------------------------------- 3. Wikipedia at quarter-ends before N-PORT
    wiki = {}                                           # day -> (revision date, {ticker: company}) resolved
    def wiki_on(day, issues_out):
        try:
            ts, lst = src_.wiki_list_on(day, http=get)
        except Exception as e:
            issues_out.append(("wikipedia_history", day, "", "REVIEW", f"page version not read: {type(e).__name__}: {e}"))
            return None
        if not lst:
            issues_out.append(("wikipedia_history", day, "", "REVIEW", "no Wikipedia S&P 100 list for this date"))
            return None
        got = {}
        for t, comp in lst.items():
            tt, note = R.ticker(t, day)
            tt = tt or R.name(comp, day)
            if tt:
                got[tt] = comp
                R.add_name(comp, tt)
                continue
            gone = R.left_before(t, day)
            if gone:
                issues_out.append(("sp100_mapping", day, t, "INFO", f"Wikipedia ({ts}) still lists '{comp}', which left "
                                   f"the S&P 500 on {gone} - page not yet updated, left out"))
            else:
                issues_out.append(("sp100_mapping", day, t, "REVIEW", f"Wikipedia ({ts}): '{comp}' ({t}) is not an S&P 500 "
                                   "member that day in our history - left out (add a [[ticker_alias]] in "
                                   "config/sp100_changes.toml if it is a renamed company)"))
        return ts, got

    q_days = [_session_on_or_before(cal, q) or cal[0] for q in [mb.START] + _quarter_ends(mb.START, first_nport)
              if q < first_nport]
    for day in dict.fromkeys(q_days):
        w = wiki_on(day, issues)
        if w:
            wiki[day] = w

    # ---------------------------------------------------------------- 4. OEF reports 2015-2019 (company names)
    reports = {}                                        # day -> (members, source)
    n_rep = n_rep_bad = 0
    try:
        docs = src_.report_documents(http=sec_get)
    except Exception as e:
        docs = []
        add("oef_reports", "REVIEW", 0, f"OEF SEC reports not read: {type(e).__name__}: {str(e)[:150]}")
    for form, fdate, acc, path in docs:
        period, raw_rows = src_.parse_report(path)
        agg = {}                                    # 'BlackRock Inc.c' (holding) + 'BlackRock Inc.' (affiliates table)
        for n, v in raw_rows:
            agg[h.clean_name(n)] = max(v, agg.get(h.clean_name(n), 0))
        rows = list(agg.items())
        total = sum(v for _, v in rows)
        names = [n for n, v in rows if v >= RESIDUAL * total]     # leftovers (e.g. a spin-off's shares) are not index stocks
        if not period or len(names) < 90:
            issues.append(("oef_reports", pd.Timestamp(fdate).date(), "", "INFO",
                           f"{form} {acc}: OEF schedule not found or incomplete ({len(names)} names) - not used"))
            continue
        day = _session_on_or_before(cal, period) or cal[0]
        if (cal[0] - period).days > 95:                 # only the last report before the build start is needed
            continue
        for n, v in rows:
            if v < RESIDUAL * total:
                issues.append(("oef_reports", day, "", "INFO", f"OEF {form} {period}: leftover position '{h.clean_name(n)}' "
                               f"(${v:,.0f}, under {RESIDUAL:.2%} of the fund) - not an index stock, ignored"))
        got, bad = {}, []
        for n in names:
            t = R.name(n, day)
            (got.__setitem__(t, n) if t else bad.append(n))
        n_rep += 1
        src = f"OEF {form} {period}"
        if bad:
            n_rep_bad += 1
            for n in bad:
                issues.append(("oef_reports", day, "", "REVIEW", f"{src}: holding '{h.clean_name(n)}' not matched to an "
                               "S&P 500 member - report used as a check only (add a [[name_alias]] in config/sp100_changes.toml)"))
        if period < cal[0]:
            if day not in reports or reports[day][2] < period:   # the latest report before the build start
                reports[day] = (got, src, period, bool(bad))
        else:
            reports[day] = (got, src, period, bool(bad))
    if docs:
        add("oef_reports", "REVIEW" if n_rep_bad else "PASS", n_rep,
            f"{n_rep} OEF reports 2015-2019 read (N-Q, N-CSR, N-CSRS); fully matched {n_rep - n_rep_bad}, "
            f"with unmatched names {n_rep_bad} (those are used only as checks)")

    # the fund's own reports are the lists; Wikipedia (often edited late) only fills the build start if no report
    # covers it, and otherwise serves as the independent check
    pre = {d: (got, src) for d, (got, src, period, bad) in reports.items() if not bad and d < first_nport}
    if cal[0] not in pre and cal[0] in wiki:
        ts, got = wiki[cal[0]]
        pre[cal[0]] = (got, f"Wikipedia {ts}")
        issues.append(("history_coverage", cal[0], "", "REVIEW", f"no OEF report near the build start - "
                       f"membership on {cal[0]} taken from Wikipedia's page of {ts}"))
    snaps += [(d, m, s) for d, (m, s) in pre.items()]
    saved = con.execute("""SELECT snap_date, ticker, company FROM index_snapshots
                           WHERE index_name = ? AND snap_date > ? ORDER BY 1""",
                        [KEY, nport_days[-1] if nport_days else first_nport]).df()
    for d, g in saved.groupby("snap_date"):
        d = pd.Timestamp(d).date()
        day = _session_on_or_before(cal, d)
        got = {}
        for t, comp in zip(g["ticker"], g["company"]):
            tt = R.ticker(t, day)[0] or R.name(comp, day)
            if tt:
                got[tt] = comp
            else:
                issues.append(("sp100_mapping", day, t, "REVIEW", f"OEF list {d}: '{comp}' not an S&P 500 member in our history"))
        snaps.append((day, got, f"OEF list {d}"))
    snaps = sorted({d: (d, m, s) for d, m, s in sorted(snaps, key=lambda x: x[0])}.values(), key=lambda x: x[0])
    if not snaps:
        raise RuntimeError("SP100: no dated lists available - nothing changed")
    if snaps[0][0] > cal[0]:
        add("history_coverage", "REVIEW", 0, f"first dated list is {snaps[0][0]}; earlier membership assumed unchanged")
    last_filed = nport_days[-1] if nport_days else None
    gaps, info_gaps = [], []
    for a, b in zip(snaps, snaps[1:]):
        if (b[0] - a[0]).days > 100:
            (info_gaps if last_filed and a[0] >= last_filed else gaps).append((a[0], b[0]))
    for a, b in info_gaps:
        issues.append(("snapshot_gaps", b, "", "INFO",
                       f"no list between OEF's latest filing ({a}) and {b}; changes in between are dated by rule "
                       f"(each run saves today's list, so this closes as runs build up)"))
    kinds = pd.Series([s.split()[0] if not s.startswith("OEF") else " ".join(s.split()[:2]) for _, _, s in snaps]).value_counts()
    add("dated_lists", "REVIEW" if gaps else "PASS", len(snaps),
        f"{len(snaps)} dated lists ({', '.join(f'{k} {v}' for k, v in kinds.items())}); gaps over 100 days: {len(gaps)}")

    # ---------------------------------------------------------------- 5. changes -> periods
    ch, date_issues, waits = h.changes_from_snapshots(snaps, sp500, cal, h.load_overrides())
    issues += date_issues
    current = pd.DataFrame({"ticker": list(snaps[-1][1]), "company": list(snaps[-1][1].values())})
    periods, ch, problems = mb.build_periods(KEY, current, ch, today, (), source="iShares OEF + OEF SEC filings + Wikipedia")
    names_now = dict(zip(sp500["ticker"], sp500["company"]))
    periods["company"] = [names_now.get(t, c) for t, c in zip(periods["ticker"], periods["company"])]
    issues += problems
    walk_fail = sum(1 for p in problems if p[0] == "walk")
    add("history_consistent", "PASS" if not walk_fail else "FAIL", int(ch["applied"].sum()),
        f"{int(ch['applied'].sum())} changes applied; {walk_fail} inconsistent")
    prec = ch["precision"].value_counts().to_dict() if len(ch) else {}
    add("change_dates", "REVIEW" if prec.get("snapshot", 0) else "PASS", len(ch),
        "how each change date was found: " + ", ".join(f"{k} {v}" for k, v in sorted(prec.items()))
        + " (exact = S&P 500 join/leave day; rebalance = quarterly rebalance, the usual S&P 100 change day)")

    def ours_on(day):
        return set(periods[(periods["start_date"] <= day) & (periods["end_date"].isna() | (periods["end_date"] >= day))]["ticker"])

    # ---------------------------------------------------------------- 6. audit checks
    n_bad = 0
    for day, m, s in snaps:
        diff = h.compare_lists(day, ours_on(day), set(m), s, result="FAIL", check="lists_reproduced")
        n_bad += len(diff)
        issues += diff
    add("lists_reproduced", "FAIL" if n_bad else "PASS", len(snaps),
        f"history matches all {len(snaps)} dated lists on their dates; differences {n_bad}")

    sub = h.check_subset_of_sp500(periods, sp500, cal)
    issues += sub
    add("subset_of_sp500", "FAIL" if sub else "PASS", len(periods),
        f"S&P 100 members not in the S&P 500 on the same day: {len(sub)} tickers")

    counts = mb.daily_counts(periods, today)
    out = (counts["members"] < cfg["min"]) | (counts["members"] > cfg["max"])
    back = pd.Series([not out.iloc[i + 1:i + 4].all() if i + 1 < len(out) else False for i in range(len(out))],
                     index=counts.index)
    waiting = counts["date"].map(lambda d: any(lo <= d <= hi for lo, hi in waits))
    transient, bad = counts[out & back], counts[out & ~back & ~waiting]
    n_wait = int((out & ~back & waiting).sum())
    for _, r in transient.iterrows():
        issues.append(("member_count", r["date"], "", "INFO", f"{r['members']} members for a few days "
                       f"(allowed {cfg['min']}-{cfg['max']}) - spin-off/replacement overlap"))
    if len(bad):
        runs = (bad["date"].map({d: i for i, d in enumerate(cal)}).diff() != 1).cumsum()
        for _, g in bad.groupby(runs):
            issues.append(("member_count", g["date"].iloc[0], "", "FAIL",
                           f"{g['members'].min()}-{g['members'].max()} members from {g['date'].iloc[0]} to "
                           f"{g['date'].iloc[-1]} ({len(g)} days; allowed {cfg['min']}-{cfg['max']})"))
    add("member_count", "FAIL" if len(bad) else "REVIEW" if n_wait else "PASS", len(counts),
        f"members per day {counts['members'].min()}-{counts['members'].max()}; "
        f"days outside {cfg['min']}-{cfg['max']}: {len(bad)} (+{len(transient)} short overlaps, allowed; "
        f"+{n_wait} while a mid-quarter replacement's date is unknown - see sp100_dates)")

    applied = ch[ch["applied"]]
    per_day = applied.groupby(["date", "action"]).size().unstack(fill_value=0) if len(applied) else pd.DataFrame()
    uneven = per_day[per_day.get("add", 0) != per_day.get("remove", 0)] if len(per_day) else per_day
    for d, r in uneven.iterrows():
        issues.append(("one_in_one_out", d, "", "INFO", f"{int(r.get('add', 0))} added, {int(r.get('remove', 0))} removed"))
    add("one_in_one_out", "INFO" if len(uneven) else "PASS", len(per_day),
        f"dates where additions and removals differ: {len(uneven)}")

    pos = {d: i for i, d in enumerate(cal)}

    def near_change(t, day):
        i = pos.get(day, 0)
        lo, hi = cal[max(0, i - TIMING_SESSIONS)], cal[min(len(cal) - 1, i + TIMING_SESSIONS)]
        return any(lo <= d <= hi for d in applied.loc[applied["ticker"] == t, "date"])

    # independent: Wikipedia's page on every fund-dated list, and inside every gap between lists.
    # A difference is Wikipedia's delay (INFO), not ours, when:
    #   - the stock changed in our history within 10 sessions of the day, or after the page version was saved, or
    #   - Wikipedia's next version agrees with us while our history for that stock did not change (it caught up late).
    fund_days = sorted(set(nport_days) | {d for d, (m, s) in pre.items() if s.startswith("OEF") and d > cal[0]})
    gap_days = sorted({d for d in wiki for a, b in gaps if a < d < b})
    check_days = sorted(set(fund_days) | set(gap_days))
    seen, raw = [], []                                  # days read; (day, ticker, side, revision)
    for day in check_days:
        w = wiki.get(day) or wiki_on(day, [])
        if not w:
            continue
        ts, theirs = w
        seen.append((day, set(theirs), ts))
        ours = ours_on(day)
        raw += [(day, t, "ours", ts) for t in sorted(ours - set(theirs))]
        raw += [(day, t, "wiki", ts) for t in sorted(set(theirs) - ours)]
    n_wdiff = n_wlate = 0
    open_days = set()
    for day, t, side, ts in raw:
        rev = pd.Timestamp(ts).date()
        changed_since_rev = any(rev < d <= day for d in applied.loc[applied["ticker"] == t, "date"])
        # the first later Wikipedia version that agrees with us about t, while our history for t did not change
        member = t in ours_on(day)
        caught_up = None
        for d2, th2, ts2 in (x for x in seen if x[0] > day):
            if any(day < d <= d2 for d in applied.loc[applied["ticker"] == t, "date"]):
                break                                   # our history changed for t: not a plain Wikipedia delay
            if (t in th2) == member:
                caught_up = ts2
                break
        where = "in our S&P 100, not on Wikipedia" if side == "ours" else "on Wikipedia, not in our S&P 100"
        if near_change(t, day) or changed_since_rev or caught_up:
            why = (f"its version of {caught_up} agrees with us, our history unchanged" if caught_up
                   else "page version older than the change")
            issues.append(("wikipedia_check", day, t, "INFO", f"{where} on {day} (Wikipedia {ts}) - Wikipedia late: {why}"))
            n_wlate += 1
        else:
            issues.append(("wikipedia_check", day, t, "REVIEW", f"{where} on {day} (Wikipedia {ts})"))
            n_wdiff += 1
            open_days.add(day)
    add("wikipedia_check", "REVIEW" if n_wdiff else "PASS" if seen else "INFO", len(seen),
        f"{len(seen)} quarter-ends compared with Wikipedia's page of that date; differences {n_wdiff} "
        f"(+{n_wlate} where Wikipedia was late - a later version agrees with us, or the change was recent; allowed)")
    for a, b in gaps:
        inside = [d for d, _, _ in seen if a < d < b]
        if inside and not any(d in open_days for d in inside):
            issues.append(("snapshot_gaps", b, "", "INFO", f"no fund list between {a} and {b} (OEF filed no holdings "
                           f"report for that quarter); Wikipedia on {', '.join(map(str, inside))} agrees with our history"))
        else:
            issues.append(("snapshot_gaps", b, "", "REVIEW", f"no list between {a} and {b} - change dates less certain"))
    if gaps and not any(i[0] == "snapshot_gaps" and i[3] == "REVIEW" for i in issues):
        for r in results:
            if r["check"] == "dated_lists":
                r["status"], r["detail"] = "PASS", r["detail"] + " (each gap confirmed by Wikipedia)"

    # independent: fund reports that could not be used as lists (unmatched names) - compare what matched
    n_q = n_qdiff = 0
    for day, (got, src, period, bad_names) in reports.items():
        if not bad_names or period < cal[0]:
            continue
        ours = ours_on(day)
        d = [x for x in h.compare_lists(day, ours, set(got), src, check="oef_report_check") if "not in our" in x[4]]
        n_q += 1
        n_qdiff += len(d)
        issues += d
    if n_q:
        add("oef_report_check", "REVIEW" if n_qdiff else "PASS", n_q,
            f"{n_q} partly matched OEF reports: matched holdings missing from our history {n_qdiff}")

    has_prices = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'prices_daily'")[0][0]
    priced = {r[0] for r in run(con, "SELECT DISTINCT ticker FROM prices_daily")} if has_prices else set()
    need = sorted(set(periods[periods["end_date"].isna() | (periods["end_date"] >= mb.BACKTEST_START)]["ticker"]))
    missing = sorted(set(need) - priced)
    for t in missing:
        issues.append(("prices_exist", today, t, "INFO", "member since the backtest start without stored prices"))
    add("prices_exist", "INFO", len(need),
        f"{len(need)} tickers were members since {mb.BACKTEST_START}; {len(missing)} have no prices stored yet"
        + (f" ({', '.join(missing)})" if missing else ""))

    reviewed = load_reviewed()
    touched = set()
    for k, (check, d, t, res, note) in enumerate(issues):
        if res in ("FAIL", "REVIEW") and (t, d, check) in reviewed:
            issues[k] = (check, d, t, "INFO", f"REVIEWED: {reviewed[(t, d, check)] or 'confirmed'} - {note}")
            touched.add(check)
    for r in results:
        if r["status"] in ("FAIL", "REVIEW") and r["check"] in touched and not any(
                i[0] == r["check"] and i[3] in ("FAIL", "REVIEW") for i in issues):
            r["status"], r["detail"] = "PASS", r["detail"] + " (all items reviewed)"
    # mapping / page-version problems are reported as their own check
    open_map = [i for i in issues if i[0] in ("sp100_mapping", "wikipedia_history") and i[3] in ("FAIL", "REVIEW")]
    add("source_mapping", "REVIEW" if open_map else "PASS", len(open_map),
        f"list entries that could not be matched to an S&P 500 member, or page versions not read: {len(open_map)}")

    mb.store(con, KEY, periods, ch, "iShares OEF + OEF SEC filings + Wikipedia")
    return {"periods": periods, "changes": ch, "counts": counts, "results": results,
            "issues": issues, "need": need, "missing": missing}
