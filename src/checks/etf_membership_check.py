"""
Independent check of index membership against what an ETF tracking the index actually held
at each quarter-end (SEC Form N-PORT), plus a quarter-end price check from the same filings.

Membership rule (docs/MEMBERSHIP_PROCEDURE.md):
  - every stock the ETF held must be an index member that day, and every member must be held;
  - a difference is allowed (INFO) if the stock joined/left the index within 5 trading days;
  - a holding whose CUSIP can't be turned into a ticker is listed for review.
Price rule: holding value / shares = that stock's closing price on the quarter-end; it must
match our stored close (adjusted back for later splits) within 0.5%.
"""
import re

import pandas as pd

from src.common import run
from src.collect import sec_nport, cusip_map
from src import membership as mb
from src.checks.price_audit import load_reviewed

TIMING_SESSIONS = 5
PRICE_TOL = 0.005
RESIDUAL_SHARE = 1e-6    # a position under 0.0001% of the fund is a leftover (e.g. 1 share kept after leaving)

SCHEMA = """
CREATE TABLE IF NOT EXISTS etf_holdings (
    etf VARCHAR, period DATE, session DATE, ticker VARCHAR, name VARCHAR, title VARCHAR,
    cusip VARCHAR, shares DOUBLE, value_usd DOUBLE, price DOUBLE, match VARCHAR,
    PRIMARY KEY (etf, period, cusip)
);
"""


def _member_tickers(periods, day):
    m = periods[(periods["start_date"] <= day) & (periods["end_date"].isna() | (periods["end_date"] >= day))]
    return set(m["ticker"])


def _near_change(periods, ticker, s_lo, s_hi):
    """True if the ticker joined or left the index between sessions s_lo and s_hi."""
    p = periods[periods["ticker"] == ticker]
    starts = p["start_date"][p["start_known"]]
    ends = p["end_date"].dropna()
    return bool(((starts >= s_lo) & (starts <= s_hi)).any() or ((ends >= s_lo) & (ends <= s_hi)).any())


def _class_letter(title):
    m = re.search(r"\b(?:class|series)\s+([A-Z])\b", str(title), re.I)
    return m.group(1).upper() if m else None


def _pair_by_name(unexplained_held, unexplained_members, company_of):
    """
    Same company, different ticker (e.g. CUSIP lookup says FISV, our history says FI).
    unexplained_held: list of (ticker or None, name, title); unexplained_members: set of tickers.
    Returns (pairs [(held_ticker, member_ticker, name)], remaining held, remaining members).
    """
    members = set(unexplained_members)
    pairs, left = [], []
    # holdings whose share-class letter matches a ticker ending go first (LBTYA = Class/Series A)
    order = sorted(unexplained_held, key=lambda h: 0 if _class_letter(h[2]) else 1)
    for t, name, title in order:
        cands = sorted(m for m in members if similar_names_safe(name, company_of.get(m, "")))
        if not cands:
            left.append((t, name, title))
            continue
        letter = _class_letter(title)
        pick = next((m for m in cands if letter and m.endswith(letter)), cands[0])
        pairs.append((t, pick, name))
        members.discard(pick)
    return pairs, left, members


def similar_names_safe(a, b):
    return bool(a) and bool(b) and mb.similar_names(a, b)


def _name_match(holding, periods, day):
    """Fallback when the CUSIP gives no ticker: a member with a similar company name that day."""
    cands = periods[(periods["start_date"] <= day) & (periods["end_date"].isna() | (periods["end_date"] >= day))]
    hits = [t for t, c in zip(cands["ticker"], cands["company"]) if mb.similar_names(holding["name"], c)]
    return hits[0] if len(hits) == 1 else None


def run_check(con, index_name, etf, periods, renames, today, http_get=None, http_post=None):
    """Returns (results list, issues list, holdings DataFrame)."""
    results, issues = [], []
    kw_get = {"http": http_get} if http_get else {}
    kw_post = {"http": http_post} if http_post else {}
    con.execute(SCHEMA).fetchall()

    holdings, filed = sec_nport.all_holdings(etf, **kw_get)
    if holdings.empty:
        results.append({"check": f"{etf.lower()}_cross_check", "status": "INFO", "n_items": 0,
                        "detail": f"no {etf} filings found"})
        return results, issues, holdings
    # the 'cusip' column holds the CUSIP, or the ISIN for foreign companies filed without one
    holdings["cusip"] = [sec_nport.security_id(c, i) for c, i in
                         zip(holdings["cusip"], holdings["isin"] if "isin" in holdings else [""] * len(holdings))]
    tick = cusip_map.lookup(con, holdings["cusip"].unique(), **kw_post)
    holdings["ticker"] = holdings["cusip"].map(tick)
    holdings["match"] = holdings["ticker"].notna().map({True: "cusip", False: ""})

    cal = mb.sessions(mb.START, today)
    pos = {d: i for i, d in enumerate(cal)}
    n_fail = n_review = n_timing = n_q = n_alias = 0
    sess_of = {}
    for period in sorted(holdings["period"].unique()):
        session = max(d for d in cal if d <= period) if period >= cal[0] else None
        if session is None:
            continue
        sess_of[period] = session
        n_q += 1
        h = holdings[holdings["period"] == period]
        tiny = h["value_usd"].fillna(0) < RESIDUAL_SHARE * h["value_usd"].sum()
        for name, v in zip(h.loc[tiny, "name"], h.loc[tiny, "value_usd"]):
            issues.append(("etf_residual", session, "", "INFO",
                           f"{etf} held a leftover position in '{name}' (${v:,.0f}) - ignored"))
        h = h[~tiny]
        members = _member_tickers(periods, session)
        company_of = dict(zip(periods["ticker"], periods["company"]))
        held_rows = [(None if pd.isna(t) else mb.to_current_ticker(t, session, renames), n, ti)
                     for t, n, ti in zip(h["ticker"], h["name"], h["title"])]
        held = {t for t, _, _ in held_rows if t}
        i = pos[session]
        lo, hi = cal[max(0, i - TIMING_SESSIONS)], cal[min(len(cal) - 1, i + TIMING_SESSIONS)]
        # 1) same company, different ticker
        unexplained = [r for r in held_rows if r[0] is None or r[0] not in members]
        pairs, left, missing = _pair_by_name(unexplained, members - held, company_of)
        for ht, mt, name in pairs:
            issues.append(("etf_same_company", session, mt, "INFO",
                           f"'{name}': {etf}'s CUSIP gives {ht or 'no ticker'}, our history uses {mt} - same company"))
            n_alias += 1
            holdings.loc[(holdings["period"] == period) & (holdings["name"] == name) &
                         ((holdings["ticker"] == ht) if ht else holdings["ticker"].isna()), ["ticker", "match"]] = [mt, "name"]
        # 2) what's left
        for t, name, title in left:
            if t is None:
                issues.append(("etf_unmatched", session, "", "REVIEW",
                               f"{etf} holding '{title}' could not be matched to a ticker or a member's name - "
                               f"add its CUSIP to config/cusip_overrides.toml"))
                n_review += 1
            elif _near_change(periods, t, lo, hi):
                issues.append(("etf_timing", session, t, "INFO",
                               f"held by {etf} but not a member on {session}; index change within "
                               f"{TIMING_SESSIONS} trading days (fund timing)"))
                n_timing += 1
            else:
                issues.append(("etf_mismatch", session, t, "FAIL",
                               f"held by {etf} on {session} ('{name}') but our history says it was NOT in the index"))
                n_fail += 1
        for t in sorted(missing):
            if _near_change(periods, t, lo, hi):
                issues.append(("etf_timing", session, t, "INFO",
                               f"member on {session} but not held by {etf}; index change within "
                               f"{TIMING_SESSIONS} trading days (fund timing)"))
                n_timing += 1
            else:
                issues.append(("etf_mismatch", session, t, "FAIL",
                               f"member on {session} per our history but NOT held by {etf} - check the dates"))
                n_fail += 1

    # items a person has confirmed (config/audit_reviewed.toml, check = "etf_mismatch")
    reviewed = load_reviewed()
    for k, it in enumerate(issues):
        check, d, t, res, note = it
        if res in ("FAIL", "REVIEW") and (t, d, check) in {(rt, rd, rc) for (rt, rd, rc) in reviewed}:
            issues[k] = (check, d, t, "INFO", f"REVIEWED: {reviewed[(t, d, check)] or 'confirmed'} - {note}")
            n_fail -= res == "FAIL"
            n_review -= res == "REVIEW"
    first = min(sess_of.values()) if sess_of else None
    status = "FAIL" if n_fail else "REVIEW" if n_review else "PASS"
    results.append({"check": f"{etf.lower()}_cross_check", "status": status, "n_items": n_q,
                    "detail": f"{n_q} quarter-ends compared with {etf}'s SEC filings ({first} to "
                              f"{max(sess_of.values()) if sess_of else '-'}); mismatches {n_fail}, "
                              f"timing differences {n_timing} (allowed), same company under another ticker "
                              f"{n_alias} (allowed), unmatched holdings {n_review}"})
    if first and first > mb.BACKTEST_START:
        results.append({"check": f"{etf.lower()}_coverage", "status": "INFO", "n_items": 0,
                        "detail": f"{etf} filings in this format start {first}; quarter-ends from "
                                  f"{mb.BACKTEST_START} to then are not cross-checked (Wikipedia history only)"})

    # --- quarter-end price check (only for tickers whose prices are stored)
    holdings["session"] = holdings["period"].map(sess_of)
    has_prices = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'prices_daily'")[0][0]
    n_p = n_bad = 0
    comps = {}                                           # ticker -> [(session, ratio)]
    if has_prices:
        for _, row in holdings.dropna(subset=["ticker", "session", "price"]).iterrows():
            t = mb.to_current_ticker(row["ticker"], row["session"], renames)
            c = run(con, "SELECT close FROM prices_daily WHERE ticker = ? AND date = ?", [t, row["session"]])
            if not c:
                continue
            splits = run(con, """SELECT coalesce(product(value), 1) FROM corporate_actions
                                 WHERE ticker = ? AND action = 'split' AND date > ?""", [t, row["session"]])[0][0]
            ours = c[0][0] * (splits or 1)               # back to the price actually traded that day
            n_p += 1
            comps.setdefault(t, []).append((row["session"], ours / row["price"], ours, row["price"]))
    for t, rows in comps.items():
        rows.sort()
        bad = [r for r in rows if abs(r[1] - 1) > PRICE_TOL]
        if not bad:
            continue
        good_after = [r for r in rows if abs(r[1] - 1) <= PRICE_TOL and r[0] > bad[-1][0]]
        good_before = [r for r in rows if abs(r[1] - 1) <= PRICE_TOL and r[0] < bad[-1][0]]
        ratios = [r[1] for r in bad]
        constant = len(bad) >= 2 and (max(ratios) / min(ratios) - 1) < 0.003 and not good_before and good_after
        for s_, ratio, ours, filed in bad:
            if constant:
                issues.append(("etf_price", s_, t, "INFO",
                               f"our close {ours:.4f} vs {etf} filing {filed:.4f}: every quarter-end before "
                               f"{good_after[0][0]} differs by the same factor ({(ratio - 1) * 100:+.2f}%) - the price "
                               f"source scaled older prices for a corporate event (e.g. a spin-off); returns unaffected"))
            else:
                n_bad += 1
                issues.append(("etf_price", s_, t, "REVIEW",
                               f"our close {ours:.4f} vs {etf} filing {filed:.4f} ({(ratio - 1) * 100:+.2f}%)"))
    results.append({"check": "quarter_end_prices", "status": "REVIEW" if n_bad else ("PASS" if n_p else "INFO"),
                    "n_items": n_p, "detail": f"{n_p} quarter-end closes compared with {etf}'s filings "
                                              f"(stocks with stored prices only); differences >0.5%: {n_bad}"})

    run(con, "DELETE FROM etf_holdings WHERE etf = ?", [etf])
    store = holdings.dropna(subset=["session"]).assign(etf=etf)[
        ["etf", "period", "session", "ticker", "name", "title", "cusip", "shares", "value_usd", "price", "match"]]
    con.register("h_new", store)
    run(con, "INSERT OR REPLACE INTO etf_holdings SELECT * FROM h_new")
    con.unregister("h_new")
    return results, issues, holdings
