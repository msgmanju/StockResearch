"""
S&P 100 history from dated lists (docs/MEMBERSHIP_PROCEDURE.md, section 'S&P 100').

S&P publishes no free change list for the S&P 100, so history is rebuilt from lists of members on
known days ("snapshots"): Wikipedia's page at quarter-ends (2015-2019), OEF's SEC filings at
quarter-ends (late 2019 on), our saved daily official lists, and today's iShares list.
Between two snapshots A and B, each ticker that appears or disappears gets a change date:
  1. exact   - the day it joined or left the S&P 500 inside (A, B]   (an S&P 100 stock must be in the S&P 500)
  2. exact   - a date you set in config/sp100_changes.toml
  3. paired  - a replacement for a stock that left the S&P 500 mid-quarter (only when there is
               exactly one candidate, so the pairing is certain from the counts)
  4. rebalance - otherwise the quarterly rebalance inside (A, B]: effective the Monday after the
               third Friday of Mar/Jun/Sep/Dec (when S&P makes most S&P 100 changes)
  5. snapshot - no rebalance inside (A, B]: date B itself (flagged REVIEW)
"""
import datetime as dt
import re
import tomllib

import pandas as pd

from src.common import ROOT

OVERRIDES_FILE = ROOT / "config" / "sp100_changes.toml"


def third_friday(year, month):
    d = dt.date(year, month, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def rebalance_dates(cal):
    """Effective dates (first session after the third Friday of Mar, Jun, Sep, Dec) within the calendar."""
    out = []
    for y in range(cal[0].year, cal[-1].year + 1):
        for m in (3, 6, 9, 12):
            f = third_friday(y, m)
            nxt = next((s for s in cal if s > f), None)
            if nxt and cal[0] <= nxt:
                out.append(nxt)
    return out


def _config():
    if not OVERRIDES_FILE.exists():
        return {}
    with open(OVERRIDES_FILE, "rb") as f:
        return tomllib.load(f)


def load_ticker_aliases():
    """[(old ticker, today's ticker, used until)] from config/sp100_changes.toml [[ticker_alias]]:
    old tickers on Wikipedia's S&P 100 page that our S&P 500 history stores under today's ticker."""
    return [(i["old"].upper(), i["new"].upper(), dt.date.fromisoformat(str(i["until"])))
            for i in _config().get("ticker_alias", [])]


def load_name_aliases():
    """[(company name as written in a fund report, ticker)] from config/sp100_changes.toml [[name_alias]]"""
    return [(i["name"], i["ticker"].upper()) for i in _config().get("name_alias", [])]


def load_overrides():
    """{(ticker, action): [(date, note)]} from config/sp100_changes.toml [[change]]"""
    items = _config().get("change", [])
    out = {}
    for i in items:
        out.setdefault((i["ticker"].upper(), i["action"].lower()), []).append(
            (dt.date.fromisoformat(str(i["date"])), i.get("note", "")))
    return out


def sp500_events(sp500_periods):
    """{ticker: (list of S&P 500 start dates, list of first-session-after-end dates)}"""
    ev = {}
    for r in sp500_periods.itertuples(index=False):
        s, e = ev.setdefault(r.ticker, ([], []))
        if r.start_known:
            s.append(r.start_date)
        if r.end_date is not None and not pd.isna(r.end_date):
            e.append(r.end_date)
    return ev


def sp500_intervals(sp500_periods, last_day):
    """{ticker: [(start, end)]} (end = last_day for current members)"""
    out = {}
    for r in sp500_periods.itertuples(index=False):
        end = r.end_date if r.end_date is not None and not pd.isna(r.end_date) else last_day
        out.setdefault(r.ticker, []).append((r.start_date, end))
    return out


def in_sp500(intervals, ticker, day):
    return any(s <= day <= e for s, e in intervals.get(ticker, ()))


def changes_from_snapshots(snaps, sp500_periods, cal, overrides=None):
    """
    snaps : list of (date, {ticker: company}, source) in date order; tickers are today's tickers
    Returns (changes DataFrame[date, action, ticker, company, reason, source, precision], issues, waits).
    'date' follows build_periods' rule: add on D = member from D; remove on D = member until the session before D.
    waits: [(first day, last day)] when a stock left mid-quarter and its replacement's day is not known
    (the member count is one short then; reported as REVIEW, not as a count failure).
    """
    overrides = overrides or {}
    ev = sp500_events(sp500_periods)
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}
    prev = {cal[i + 1]: d for i, d in enumerate(cal[:-1])}
    rebal = rebalance_dates(cal)
    rows, issues, waits = [], [], []
    for (a, ma, sa), (b, mb_, sb) in zip(snaps, snaps[1:]):
        added, removed = sorted(set(mb_) - set(ma)), sorted(set(ma) - set(mb_))
        if not added and not removed:
            continue
        reb = [d for d in rebal if a < d <= b]
        reb_d = reb[-1] if reb else None
        window = f"between {a} ({sa}) and {b} ({sb})"
        dated = []                                            # [action, ticker, company, date, precision, reason]

        def override(t, action):
            ov = [(d, n) for d, n in overrides.get((t, action), []) if a < d <= b + dt.timedelta(days=7)]
            return ov[0] if ov else None

        for t in removed:
            ends = ev.get(t, ([], []))[1]
            ov = override(t, "remove")
            if ov:
                dated.append(["remove", t, ma[t], ov[0], "override", f"{window}; date from sp100_changes.toml {ov[1]}".strip()])
            elif any(a <= e < b for e in ends):
                e = min(e for e in ends if a <= e < b)
                dated.append(["remove", t, ma[t], nxt.get(e, e), "exact", f"{window}; left the S&P 500 after {e}"])
            elif reb_d:
                dated.append(["remove", t, ma[t], reb_d, "rebalance", f"{window}; quarterly rebalance {reb_d}"])
            else:
                dated.append(["remove", t, ma[t], b, "snapshot", f"{window}; no rebalance in between - date of the later list"])
        off_days = {x[3]: x[1] for x in dated if x[4] == "exact" and x[3] != reb_d}
        for t in added:
            starts = ev.get(t, ([], []))[0]
            joined = [s for s in starts if a < s <= b]
            ov = override(t, "add")
            if ov:
                dated.append(["add", t, mb_[t], ov[0], "override", f"{window}; date from sp100_changes.toml {ov[1]}".strip()])
            elif joined and (reb_d is None or max(joined) >= reb_d):          # joined the S&P 500 after the rebalance
                dated.append(["add", t, mb_[t], max(joined), "exact", f"{window}; joined the S&P 500 on {max(joined)}"])
            elif any(s in off_days for s in joined):                         # merger / spin-off: DowDuPont 2017, DuPont 2019
                s = next(s for s in joined if s in off_days)
                dated.append(["add", t, mb_[t], s, "exact",
                              f"{window}; joined the S&P 500 on {s}, the day {off_days[s]} left (same corporate event)"])
            elif joined:
                s = max(joined)
                dated.append(["add", t, mb_[t], reb_d, "rebalance",
                              f"{window}; quarterly rebalance {reb_d} (joined the S&P 500 on {s})"])
                issues.append(("sp100_dates", reb_d, t, "INFO",
                               f"joined the S&P 500 on {s}, dated in the S&P 100 at the {reb_d} rebalance; if it "
                               f"joined both on {s} (e.g. a spin-off), set that in config/sp100_changes.toml"))
            elif reb_d:
                dated.append(["add", t, mb_[t], reb_d, "rebalance", f"{window}; quarterly rebalance {reb_d}"])
            else:
                dated.append(["add", t, mb_[t], b, "snapshot", f"{window}; no rebalance in between - date of the later list"])
        # a spin-off that replaced its parent (Honeywell -> Honeywell Aerospace 2026): the parent leaves the same day
        for x in [x for x in dated if x[0] == "remove" and x[4] == "rebalance"]:
            kin = [y for y in dated if y[0] == "add" and y[4] == "exact" and _same_family(x[2], y[2])]
            if len(kin) == 1:
                x[3], x[4] = kin[0][3], "paired"
                x[5] = f"{window}; left when {kin[0][1]} (same company family) joined, {kin[0][3]}"
        # a stock that left mid-quarter: its replacement joins the same day, if it is certain which one
        paired_days = {x[3] for x in dated if x[0] == "add" and x[4] in ("exact", "override")}
        off = [x for x in dated if x[0] == "remove" and x[4] == "exact" and x[3] != reb_d and x[3] not in paired_days]
        open_adds = [x for x in dated if x[0] == "add" and x[4] == "rebalance"]
        reb_removes = [x for x in dated if x[0] == "remove" and x[4] == "rebalance"]
        spare = len(open_adds) - len(reb_removes)
        if len(off) == 1 and spare == 1 and len(open_adds) == 1:
            x = open_adds[0]
            x[3], x[4] = off[0][3], "paired"
            x[5] = f"{window}; replaced {off[0][1]}, which left the S&P 500 (pairing certain: only candidate)"
        elif off and spare > 0:
            for o in sorted(off, key=lambda x: x[3])[:spare]:
                if reb_d and o[3] < reb_d:
                    waits.append((o[3], prev.get(reb_d, reb_d)))
                    issues.append(("sp100_dates", o[3], o[1], "REVIEW",
                                   f"left the S&P 500 (so the S&P 100) on {o[3]}; its replacement is one of "
                                   f"{', '.join(x[1] for x in open_adds)} but which one and when is not certain - dated at "
                                   f"the {reb_d} rebalance until set in config/sp100_changes.toml"))
        for x in dated:
            rows.append({"date": x[3], "action": x[0], "ticker": x[1], "company": x[2], "reason": x[5],
                         "source": f"{sa} -> {sb}", "precision": x[4]})
            if x[4] == "snapshot":
                issues.append(("sp100_dates", x[3], x[1], "REVIEW",
                               f"{x[0]} {window}: no S&P 500 event or rebalance date found - set it in config/sp100_changes.toml"))
    ch = pd.DataFrame(rows, columns=["date", "action", "ticker", "company", "reason", "source", "precision"])
    return ch, issues, waits


def _same_family(a, b):
    """'Honeywell International' / 'Honeywell Aerospace': same first word (4+ letters)."""
    wa, wb = (re.findall(r"[a-z]{4,}", str(x).lower())[:1] for x in (a, b))
    return bool(wa) and wa == wb


def check_subset_of_sp500(periods, sp500_periods, cal):
    """Every S&P 100 member must be an S&P 500 member on the same day. Returns issues (FAIL)."""
    iv = sp500_intervals(sp500_periods, cal[-1])
    issues = []
    for r in periods.itertuples(index=False):
        end = r.end_date if r.end_date is not None and not pd.isna(r.end_date) else cal[-1]
        bad = [d for d in cal if r.start_date <= d <= end and not in_sp500(iv, r.ticker, d)]
        if bad:
            issues.append(("subset_of_sp500", bad[0], r.ticker, "FAIL",
                           f"S&P 100 member but not in our S&P 500 history on {len(bad)} day(s), "
                           f"{bad[0]} to {bad[-1]}"))
    return issues


def compare_lists(day, ours, theirs, source, result="REVIEW", timing=(), check="sp100_vs_source"):
    """Issues for tickers on one list only; tickers in `timing` (near a change) are INFO."""
    out = []
    for t in sorted(set(ours) - set(theirs)):
        out.append((check, day, t, "INFO" if t in timing else result, f"in our S&P 100 on {day}, not in {source}"))
    for t in sorted(set(theirs) - set(ours)):
        out.append((check, day, t, "INFO" if t in timing else result, f"in {source} on {day}, not in our S&P 100"))
    return out


# ------------------------------------------------------------------ company names -> tickers
_FOOT = re.compile(r"(?<=[.)A-Z])[a-e](,[a-e])*$")      # footnote letters: 'Amazon.com Inc.a', 'Class Aa', '(The)a,b'
_NAME_FIX = [(r"\bnvs\b", " "), (r"/[a-z]{2}\b", " "), (r"\btwenty[- ]first\b", "21st"), (r"\bthe\b", " ")]


def clean_name(n):
    n = " ".join(str(n).replace("\xa0", " ").replace("’", "'").split())
    return _FOOT.sub("", n).strip()


def class_letter(n):
    m = re.search(r"\b(?:class|series|cl)\s*([A-Z])\b", clean_name(n), re.I)
    return m.group(1).upper() if m else None


def name_key(n):
    from src.membership import _norm_name
    s = clean_name(n).lower()
    for a, b in _NAME_FIX:
        s = re.sub(a, b, s)
    s = re.sub(r"\b(?:class|series|cl)\s*[a-z]\b", " ", s)
    return _norm_name(s).replace(" ", "")


def pick_share_class(cands, cls):
    """Two share classes of one company: class A -> the longer ticker / the one ending in A (GOOGL, FOXA, TFCFA)."""
    if len(cands) != 2 or not cls:
        return None
    a, b = sorted(cands, key=lambda t: (len(t), t.endswith("A")))
    return b if cls == "A" else a
