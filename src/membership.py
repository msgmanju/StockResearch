"""
Index membership (docs/MEMBERSHIP_PROCEDURE.md).

Method: start from today's official list and walk the change history backwards.
  - a ticker ADDED on date D was a member from D (first session on/after D)
  - a ticker REMOVED on date D was a member until the last session before D
Result: one row per membership period in the index_membership table.

Future-proofing:
  - every run saves today's official list (index_snapshots). Comparing consecutive
    snapshots shows every change from now on, even if Wikipedia is late or wrong.
  - announced changes dated in the future are applied once the official list shows them
    (or their date has passed); until then they are kept as pending.
  - a ticker that disappears while a new one appears for a similarly named company is
    flagged as a possible rename (add it to config/ticker_changes.toml).
"""
import difflib
import re
import datetime as dt
import tomllib

import pandas as pd

from src.common import ROOT, SETTINGS, run

CFG = {"start_date": "2015-11-01", "backtest_start": "2016-11-01",
       "ndx_min": 100, "ndx_max": 107, "sp500_min": 500, "sp500_max": 506,
       "sp100_min": 100, "sp100_max": 102}
CFG.update(SETTINGS.get("membership", {}))
START = dt.date.fromisoformat(str(CFG["start_date"]))
BACKTEST_START = dt.date.fromisoformat(str(CFG["backtest_start"]))
TICKER_CHANGES_FILE = ROOT / "config" / "ticker_changes.toml"
SPLITS_FILE = ROOT / "config" / "membership_splits.toml"
REMOVE_GRACE_DAYS = 14   # Wikipedia sometimes lists a removal under the old ticker a few days after a rename

SCHEMA = """
CREATE TABLE IF NOT EXISTS index_membership (
    index_name      VARCHAR NOT NULL,     -- 'NDX', 'SP500' or 'SP100'
    ticker          VARCHAR NOT NULL,     -- today's ticker (prices are stored under it)
    ticker_at_time  VARCHAR,              -- ticker while in the index, if different
    company         VARCHAR,
    start_date      DATE    NOT NULL,     -- first session in the index (or the build start date)
    end_date        DATE,                 -- last session in the index; NULL = still a member
    start_known     BOOLEAN,              -- FALSE = was already a member on the build start date
    source          VARCHAR,
    note            VARCHAR,
    built_at        TIMESTAMP
);
CREATE TABLE IF NOT EXISTS index_snapshots (
    index_name  VARCHAR NOT NULL,
    snap_date   DATE    NOT NULL,          -- date the official list was captured
    ticker      VARCHAR NOT NULL,
    company     VARCHAR,
    source      VARCHAR,
    captured_at TIMESTAMP,
    PRIMARY KEY (index_name, snap_date, ticker)
);
CREATE TABLE IF NOT EXISTS index_changes (
    index_name VARCHAR, date DATE, action VARCHAR, ticker VARCHAR, ticker_at_time VARCHAR,
    company VARCHAR, reason VARCHAR, source VARCHAR, applied BOOLEAN, built_at TIMESTAMP
);
"""


def sessions(start, end):
    import exchange_calendars as xc
    cal = xc.get_calendar("XNYS")
    return [d.date() for d in cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def load_ticker_changes():
    """[(old, new, date)] from config/ticker_changes.toml"""
    if not TICKER_CHANGES_FILE.exists():
        return []
    with open(TICKER_CHANGES_FILE, "rb") as f:
        items = tomllib.load(f).get("change", [])
    return [(i["old"].upper(), i["new"].upper(), dt.date.fromisoformat(str(i["date"]))) for i in items]


def to_current_ticker(ticker, on_date, renames, grace_days=0):
    """Follow renames forward: the ticker a company has today.
    grace_days: also apply a rename to a record dated up to this many days after it."""
    t, changed = ticker, True
    while changed:
        changed = False
        for old, new, d in renames:
            if t == old and on_date < d + dt.timedelta(days=grace_days):
                t, on_date, changed = new, max(on_date, d), True
    return t


def load_splits():
    """config/membership_splits.toml: one ticker used by two companies while in the index."""
    if not SPLITS_FILE.exists():
        return []
    with open(SPLITS_FILE, "rb") as f:
        items = tomllib.load(f).get("split", [])
    return [{"index": i["index"].upper(), "ticker": i["ticker"].upper(), "before": dt.date.fromisoformat(str(i["before"])),
             "old_ticker": i["old_ticker"].upper(), "old_company": i.get("old_company", "")} for i in items]


def apply_splits(periods, index_name, splits, days):
    """Give the part of a period before 'before' to the earlier company (stored under old_ticker)."""
    rows = []
    first_on_or_after = lambda d: next((s for s in days if s >= d), d)   # noqa: E731
    last_before = lambda d: next((s for s in reversed(days) if s < d), None)  # noqa: E731
    for r in periods.to_dict("records"):
        sp = next((x for x in splits if x["index"] == index_name and x["ticker"] == r["ticker"]
                   and r["start_date"] < x["before"]), None)
        if sp is None:
            rows.append(r)
            continue
        old = dict(r, ticker=sp["old_ticker"], ticker_at_time=r["ticker"], company=sp["old_company"] or r["company"])
        if r["end_date"] is None or pd.isna(r["end_date"]) or r["end_date"] >= sp["before"]:
            old["end_date"] = last_before(sp["before"])
            rows.append(old)
            rows.append(dict(r, start_date=first_on_or_after(sp["before"]), start_known=True))
        else:
            rows.append(old)
    out = pd.DataFrame(rows, columns=periods.columns)
    return out.sort_values(["ticker", "start_date"]).reset_index(drop=True)


def _merge_adjacent(p, days):
    """Join two periods of one ticker that meet (a rename shown as remove + add on the same day)."""
    if p.empty:
        return p
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    out = []
    for r in p.sort_values(["ticker", "start_date"]).to_dict("records"):
        prev = out[-1] if out else None
        if prev and prev["ticker"] == r["ticker"] and prev["end_date"] is not None and not pd.isna(prev["end_date"]) \
                and nxt.get(prev["end_date"], prev["end_date"]) >= r["start_date"]:
            prev["end_date"] = r["end_date"]
            prev["ticker_at_time"] = prev["ticker_at_time"] or r["ticker_at_time"]
            continue
        out.append(r)
    return pd.DataFrame(out, columns=p.columns)


def build_periods(index_name, current, changes, today, renames=(), source=""):
    """
    current : DataFrame(ticker, company) - members today
    changes : DataFrame(date, action, ticker, company, reason)
    Returns (periods DataFrame, change rows with 'applied' flag, list of problems).
    """
    days = sessions(START, today + dt.timedelta(days=45))      # room for announced changes
    first_on_or_after = lambda d: next((s for s in days if s >= d), None)   # noqa: E731
    last_before = lambda d: next((s for s in reversed(days) if s < d), None)  # noqa: E731

    ch = changes.copy()
    ch["ticker_at_time"] = ch["ticker"]
    ch["ticker"] = [to_current_ticker(t, d, renames, REMOVE_GRACE_DAYS if a == "remove" else 0)
                    for t, d, a in zip(ch["ticker"], ch["date"], ch["action"])]
    base = set(current["ticker"])
    future = ch["date"] > today
    shown_today = ((ch["action"] == "add") & ch["ticker"].isin(base)) | \
                  ((ch["action"] == "remove") & ~ch["ticker"].isin(base))
    ch["applied"] = (ch["date"] > START) & (~future | shown_today)
    problems = []
    for _, r in ch[future & ~shown_today].iterrows():
        problems.append(("pending_change", r["date"], r["ticker"], "INFO",
                         f"announced {r['action']} - applied automatically once the official list shows it"))

    names = dict(zip(current["ticker"], current["company"]))
    open_end = {t: None for t in current["ticker"]}           # ticker -> end date of its open period
    at_time = {}
    periods = []
    for d in sorted(ch.loc[ch["applied"], "date"].unique(), reverse=True):
        rows = ch[(ch["date"] == d) & ch["applied"]]
        for _, r in rows[rows["action"] == "add"].iterrows():
            t = r["ticker"]
            names.setdefault(t, r["company"])
            if t in open_end:
                periods.append({"ticker": t, "ticker_at_time": at_time.pop(t, None) or (
                    r["ticker_at_time"] if r["ticker_at_time"] != t else None),
                    "company": names.get(t, ""), "start_date": first_on_or_after(d),
                    "end_date": open_end.pop(t), "start_known": True})
            else:
                problems.append(("walk", d, t, "FAIL",
                                 "added on this date but not a member afterwards and no later removal recorded"))
        for _, r in rows[rows["action"] == "remove"].iterrows():
            t = r["ticker"]
            names.setdefault(t, r["company"])
            if t in open_end:
                problems.append(("walk", d, t, "FAIL",
                                 "removed on this date but still a member afterwards without being re-added"))
            elif last_before(d) is None:
                continue                      # left on the first session of the build: never a member here
            else:
                open_end[t] = last_before(d)
                if r["ticker_at_time"] != t:
                    at_time[t] = r["ticker_at_time"]
    for t, end in open_end.items():
        periods.append({"ticker": t, "ticker_at_time": at_time.get(t), "company": names.get(t, ""),
                        "start_date": days[0], "end_date": end, "start_known": False})
    p = _merge_adjacent(pd.DataFrame(periods, columns=["ticker", "ticker_at_time", "company", "start_date",
                                                       "end_date", "start_known"]), days)
    p.insert(0, "index_name", index_name)
    p["source"] = source
    return p.sort_values(["ticker", "start_date"]).reset_index(drop=True), ch, problems


def daily_counts(periods, today):
    days = sessions(START, today)
    counts = []
    for s in days:
        m = periods[(periods["start_date"] <= s) & (periods["end_date"].isna() | (periods["end_date"] >= s))]
        counts.append((s, len(m)))
    return pd.DataFrame(counts, columns=["date", "members"])


UNIVERSES = {"BOTH": "NASDAQ-100 + S&P 500", "NDX": "NASDAQ-100 only", "SP500": "S&P 500 only",
             "SP100": "S&P 100 only", "NDX+SP100": "NASDAQ-100 + S&P 100"}


def index_names(universe):
    """'BOTH' -> ['NDX', 'SP500']; 'NDX+SP100' -> ['NDX', 'SP100']; 'SP100' -> ['SP100']"""
    u = universe.upper()
    return ["NDX", "SP500"] if u == "BOTH" else [x.strip() for x in u.split("+") if x.strip()]


def members_on(con, index_name, on_date):
    """Tickers in the index on a given session (see index_names; the S&P 100 is inside the S&P 500)."""
    names = index_names(index_name)
    q = ",".join("?" * len(names))
    return [r[0] for r in run(con, f"""SELECT DISTINCT ticker FROM index_membership
                                     WHERE index_name IN ({q}) AND start_date <= ?
                                       AND (end_date IS NULL OR end_date >= ?) ORDER BY 1""",
                              names + [on_date, on_date])]


def store(con, index_name, periods, changes, source):
    con.execute(SCHEMA).fetchall()
    con.begin()
    try:
        run(con, "DELETE FROM index_membership WHERE index_name = ?", [index_name])
        run(con, "DELETE FROM index_changes WHERE index_name = ?", [index_name])
        p = periods.assign(note="", built_at=pd.Timestamp.now())
        con.register("p_new", p)
        run(con, """INSERT INTO index_membership SELECT index_name, ticker, ticker_at_time, company,
                    start_date, end_date, start_known, source, note, built_at FROM p_new""")
        con.unregister("p_new")
        c = changes.copy()
        if "source" not in c.columns:
            c["source"] = source
        c = c.assign(index_name=index_name, built_at=pd.Timestamp.now())
        con.register("c_new", c)
        run(con, """INSERT INTO index_changes SELECT index_name, date, action, ticker, ticker_at_time,
                    company, reason, source, applied, built_at FROM c_new""")
        con.unregister("c_new")
        con.commit()
    except Exception:
        con.rollback()
        raise


# ------------------------------------------------------------------ snapshots (future-proofing)
def save_snapshot(con, index_name, snap_date, members, source):
    con.execute(SCHEMA).fetchall()
    run(con, "DELETE FROM index_snapshots WHERE index_name = ? AND snap_date = ?", [index_name, snap_date])
    m = members[["ticker", "company"]].drop_duplicates("ticker").assign(
        index_name=index_name, snap_date=snap_date, source=source, captured_at=pd.Timestamp.now())
    con.register("s_new", m)
    run(con, """INSERT INTO index_snapshots SELECT index_name, snap_date, ticker, company, source, captured_at
                FROM s_new""")
    con.unregister("s_new")


_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|cos|company|companies|ltd|limited|plc|holdings?|group|"
                     r"class [a-c]|the|n\.?v|s\.?a|se|ag)\b\.?")


def _norm_name(n):
    n = re.sub(r"[^a-z0-9 ]", " ", str(n).lower())
    return " ".join(_SUFFIX.sub(" ", n).split())


def similar_names(a, b):
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return False
    ca, cb = a.replace(" ", ""), b.replace(" ", "")          # 'super micro computer' vs 'supermicro'
    short, long_ = sorted((ca, cb), key=len)
    return (len(short) >= 5 and long_.startswith(short)) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def reconcile_snapshots(con, index_name, wiki_changes, renames, cal_days):
    """
    Compare consecutive official-list snapshots with the Wikipedia change list.
    Returns (extra change rows seen only in the official lists, problems).
    """
    rows = run(con, """SELECT snap_date, ticker, company FROM index_snapshots WHERE index_name = ?
                       ORDER BY snap_date""", [index_name])
    snaps = {}
    for d, t, c in rows:
        snaps.setdefault(d, {})[t] = c
    dates = sorted(snaps)
    known_renames = {(o, n) for o, n, _ in renames}
    extra, problems = [], []
    for d0, d1 in zip(dates, dates[1:]):
        prev, cur = snaps[d0], snaps[d1]
        added, removed = sorted(set(cur) - set(prev)), sorted(set(prev) - set(cur))
        renamed = {(o, n) for o in removed for n in added if (o, n) in known_renames}
        added = [a for a in added if a not in {n for _, n in renamed}]
        removed = [r for r in removed if r not in {o for o, _ in renamed}]
        win = wiki_changes[(wiki_changes["date"] > d0) & (wiki_changes["date"] <= d1)]
        approx = next((s for s in cal_days if s > d0), d1)
        for action, tickers, names in (("add", added, cur), ("remove", removed, prev)):
            for t in tickers:
                if ((win["action"] == action) & (win["ticker"] == t)).any():
                    continue                                        # Wikipedia has it, with the exact date
                extra.append({"date": approx, "action": action, "ticker": t, "company": names.get(t, ""),
                              "reason": f"seen in the official list between {d0} and {d1}",
                              "source": "official list"})
                problems.append(("official_only", approx, t, "REVIEW",
                                 f"{action} seen in the official list between {d0} and {d1} but not on Wikipedia - "
                                 f"date set to {approx}; confirm the exact date"))
        for o in removed:
            for n in added:
                if similar_names(prev.get(o, ""), cur.get(n, "")):
                    problems.append(("possible_rename", approx, f"{o}->{n}", "REVIEW",
                                     f"'{prev.get(o)}' left and '{cur.get(n)}' joined - if it is the same company, "
                                     f"add old={o} new={n} to config/ticker_changes.toml"))
        for _, r in win.iterrows():
            seen = (r["ticker"] in added) if r["action"] == "add" else (r["ticker"] in removed)
            if not seen and (r["ticker"], r["action"]) not in {(n, "add") for _, n in renamed} | \
                    {(o, "remove") for o, _ in renamed}:
                problems.append(("wiki_not_in_official", r["date"], r["ticker"], "REVIEW",
                                 f"Wikipedia shows {r['action']} on {r['date']} but the official lists of "
                                 f"{d0} and {d1} don't"))
    return pd.DataFrame(extra, columns=["date", "action", "ticker", "company", "reason", "source"]), problems, dates
