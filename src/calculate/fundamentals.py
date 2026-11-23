"""
Point-in-time fundamentals from SEC company-facts files.

Every number is kept with the date it was FILED. A value for a period can change later (a restatement,
or a new holding company re-reporting old years); each version is kept, and "as of" a date only the
versions filed before that date are used - so a backtest never sees numbers that weren't public yet.

Metrics (one standard definition for every company; the SEC tag used is recorded):
  revenue      net revenue (banks: after interest expense), else Revenues, else contract revenue, ...
  op_income    OperatingIncomeLoss (banks and insurers often don't report it - left empty)
  net_income   NetIncomeLoss (attributable to the company's shareholders)
  eps_diluted  EarningsPerShareDiluted, else basic; if neither is reported for the period:
               TTM net income / latest diluted share count (marked)
  equity       StockholdersEquity (shareholders of the company, excluding minority interests)
  total_debt   long-term debt incl. current part + short-term borrowings/commercial paper
               (finance leases only where the company tags them together with debt; operating leases
               excluded) - companies tag debt in many ways, so debt/equity is information only.
               If the latest balance sheet has no debt tag: the debt of the latest earlier balance sheet
               (up to 400 days back, noted); 0 when the company reports no debt (no debt tag ever, or the
               last one was 0) and no material interest expense; otherwise empty, with the reason
               (debt_note) - e.g. PACCAR and Berkshire tag their debt only in their own extension tags.
Quarterly values: 3-month figures as reported; a missing quarter (usually Q4, which is only in the
annual report) = annual - 9 months, or 6 months - 3 months, etc. (marked as derived).
TTM (trailing twelve months) = the annual figure when a fiscal year just ended, otherwise last annual +
this year-to-date - the same year-to-date a year earlier (one tag at a time; sum of 4 quarters only as a
fallback). Companies that only report yearly (foreign 20-F/40-F filers) use the latest annual figure.
"""
import datetime as dt

import numpy as np

VERSION = 10         # bump when the calculation changes - every stock is then rebuilt
import pandas as pd

from src.collect import sec_facts as sf

DURATION = {
    "revenue": ["RevenuesNetOfInterestExpense", "Revenues", "RegulatedAndUnregulatedOperatingRevenue",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
                "SalesRevenueServicesNet", "RealEstateRevenueNet", "OperatingLeasesIncomeStatementLeaseRevenue",
                "OperatingLeaseLeaseIncome"],
    "op_income": ["OperatingIncomeLoss"],
    # banks that stopped tagging total revenue: net interest income + non-interest income
    "net_interest_income": ["InterestIncomeExpenseNet"],
    "noninterest_income": ["NoninterestIncome"],
    "net_income": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding",
                       "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"],
}
INSTANT = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    # total debt in one tag
    "debt_total": ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount", "DebtAndCapitalLeaseObligations",
                   "NotesPayable", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
    # or long-term (non-current) part + current part
    "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "LongTermNotesAndLoans",
                        "LongTermNotesPayable", "ConvertibleNotesPayable"],
    "debt_current": ["DebtCurrent", "LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                     "NotesPayableCurrent", "ConvertibleDebtCurrent", "ConvertibleNotesPayableCurrent"],
    # short-term borrowings, added when the current part doesn't already include them
    "short_borrowings": ["ShortTermBorrowings", "CommercialPaper", "LineOfCredit"],
    "debt_other": ["ConvertibleDebt", "SecuredDebt"],
    # partial non-current tags: used only when the company reports no fuller debt figure that day
    "debt_fallback": ["ConvertibleLongTermNotesPayable", "ConvertibleDebtNoncurrent", "LongTermLineOfCredit",
                      "OtherLongTermDebtNoncurrent"],
}
# only used to tell 'no debt' from 'debt not tagged in standard tags' (never summed into TTM figures)
AUX = {"interest_expense": ["InterestExpense", "InterestExpenseDebt", "InterestExpenseNonoperating"]}
DEBT_METRICS = ("debt_total", "debt_noncurrent", "debt_current", "short_borrowings", "debt_other", "debt_fallback")
DEBT_LOOKBACK_DAYS = 400
# IFRS filers (some foreign companies): tags in the ifrs-full taxonomy, same metrics
IFRS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "op_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps_diluted": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"],
    "shares_diluted": ["AdjustedWeightedAverageShares"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "debt_noncurrent": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings"],
    "debt_current": ["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
                     "CurrentPortionOfNoncurrentBorrowings"],
    "short_borrowings": ["ShorttermBorrowings"],
}
REVENUE_PARTS = {"net_interest_income", "noninterest_income"}   # only used to build bank revenue
PER_SHARE = {"eps_diluted"}
SHARE_COUNTS = {"shares_diluted"}           # not additive: never summed or subtracted
FORMS = sf.MAIN_FORMS
# period lengths in days; wide enough for 12/16-week retail quarters (Costco: 84/84/84/112 days)
Q_DAYS, H_DAYS, N_DAYS, Y_DAYS = (77, 119), (160, 200), (245, 290), (350, 380)
Q_GAP = (77, 119)              # days between consecutive quarter ends


# ------------------------------------------------------------------ extraction
def reporting_currency(gaap, ifrs=None):
    """The currency the company reports in: the most used unit of its main income/balance tags."""
    count = {}
    for tax, tags in ((gaap, ("NetIncomeLoss", "StockholdersEquity", "Revenues",
                              "RevenueFromContractWithCustomerExcludingAssessedTax")),
                      (ifrs or {}, ("ProfitLoss", "Equity", "Revenue"))):
        for c in tags:
            for k, rows in tax.get(c, {}).get("units", {}).items():
                if "/" not in k and k != "shares":
                    count[k] = count.get(k, 0) + len(rows)
    return max(count, key=lambda k: (count[k], k == "USD")) if count else "USD"


def extract(facts, cik, valid_from=None, valid_to=None, currency=None):
    """All versions of the tracked tags from one company-facts file, in the company's reporting currency,
    limited to reports filed in [valid_from, valid_to].
    Columns: metric, prio, concept, unit, start, end, val, filed, accn, form, cik."""
    gaap = (facts.get("facts") or {}).get("us-gaap", {})
    ifrs = (facts.get("facts") or {}).get("ifrs-full", {})
    cur = currency or reporting_currency(gaap, ifrs)
    out = []
    for metric, concepts in list(DURATION.items()) + list(INSTANT.items()) + list(AUX.items()):
        unit = f"{cur}/shares" if metric in PER_SHARE else "shares" if metric in SHARE_COUNTS else cur
        tagged = [(gaap, c) for c in concepts] + [(ifrs, c) for c in IFRS.get(metric, [])]
        for prio, (tax, c) in enumerate(tagged):
            rows = tax.get(c, {}).get("units", {}).get(unit, [])
            for r in rows:
                form = str(r.get("form", "")).split("/")[0]
                if form not in FORMS or not r.get("filed") or r.get("val") is None:
                    continue
                filed = dt.date.fromisoformat(r["filed"])
                if (valid_from and filed < valid_from) or (valid_to and filed > valid_to):
                    continue
                out.append((metric, prio, c, cur, r.get("start"), r["end"], float(r["val"]), filed,
                            r.get("accn"), form, cik))
    df = pd.DataFrame(out, columns=["metric", "prio", "concept", "unit", "start", "end", "val", "filed", "accn",
                                    "form", "cik"])
    if len(df):
        df["start"] = pd.to_datetime(df["start"]).dt.date
        df["end"] = pd.to_datetime(df["end"]).dt.date
        df = df.drop_duplicates(["metric", "concept", "start", "end", "val", "filed"])
        df = drop_tagging_errors(df)
    return df


ANNUAL_REPORTS = ("10-K", "10-KT", "20-F", "40-F")


def drop_tagging_errors(df):
    """Remove two kinds of company tagging mistakes found in SEC filings:
    1. a full-year value in a quarterly report (10-Q) - e.g. Comfort Systems' Q1 2026 10-Q tags
       'Jan-Dec 2025' with one quarter's revenue; full years are only taken from annual reports;
    2. a 3-month value equal to the full-year value of the same filing and year-end - e.g. NiSource and
       L3Harris tag Q4 2025 with the whole year's revenue; Q4 is then derived (annual minus 9 months)."""
    days = np.array([(e - s).days if s is not None and not pd.isna(s) else -1 for s, e in zip(df["start"], df["end"])])
    year = (days >= Y_DAYS[0]) & (days <= Y_DAYS[1])
    bad1 = year & ~df["form"].isin(ANNUAL_REPORTS).to_numpy()
    quarter = (days >= Q_DAYS[0]) & (days <= Q_DAYS[1])
    annual_keys = set(zip(df.loc[year, "metric"], df.loc[year, "concept"], df.loc[year, "end"],
                          df.loc[year, "accn"], df.loc[year, "val"]))
    bad2 = quarter & np.array([(m, c, e, a, v) in annual_keys and v != 0 for m, c, e, a, v in
                               zip(df["metric"], df["concept"], df["end"], df["accn"], df["val"])], dtype=bool)
    df = df[~(bad1 | bad2)]
    return fix_annual_as_quarter(df)


def fix_annual_as_quarter(df):
    """3. an annual report that tags the full year's value as the fourth quarter and leaves the year out
    (L3Harris 10-K for 2025: 'Oct 4 2025 - Jan 2 2026' = 21.9B, the whole year). If such a 'quarter'
    is larger than the 9 months before it, it is the year: its start is moved to the fiscal-year start."""
    days = np.array([(e - s).days if s is not None and not pd.isna(s) else -1 for s, e in zip(df["start"], df["end"])])
    q4 = (days >= Q_DAYS[0]) & (days <= Q_DAYS[1]) & df["form"].isin(ANNUAL_REPORTS).to_numpy() \
        & ~df["metric"].isin(SHARE_COUNTS | PER_SHARE).to_numpy() & (df["val"] > 0).to_numpy()
    if not q4.any():
        return df
    nine = df[(days >= N_DAYS[0]) & (days <= N_DAYS[1]) & (df["val"] > 0).to_numpy()]
    if nine.empty:
        return df
    idx = {}
    for (m, c), g in nine.groupby(["metric", "concept"]):
        idx[(m, c)] = list(zip(g["start"], g["end"], g["val"]))
    df = df.copy()
    years = {(m, c, e) for m, c, e, d_ in zip(df["metric"], df["concept"], df["end"], days)
             if Y_DAYS[0] <= d_ <= Y_DAYS[1]}
    for i in np.flatnonzero(q4):
        r = df.iloc[i]
        if (r["metric"], r["concept"], r["end"]) in years:
            continue
        for s9, e9, v9 in idx.get((r["metric"], r["concept"]), []):
            if 0 <= (r["start"] - e9).days <= 10 and r["val"] > v9:
                df.iloc[i, df.columns.get_loc("start")] = s9
                break
    return df


# ------------------------------------------------------------------ point-in-time values
def prepare(df):
    """Sort once so known() can take the latest best version per period with drop_duplicates.
    Several values for one period in one filing: revenue - the largest unless they are within 10% (a
    filing can also tag a subsidiary's revenue, HollyFrontier 2016: 0.07B next to 10.5B); otherwise the
    most precise (33,866,000 rather than a rounded 33,900,000)."""
    if "_precise" in df.columns:
        return df
    df = df.assign(_precise=[len(str(int(abs(v)))) - len(str(int(abs(v))).rstrip("0")) if v == int(v) else -1
                             for v in df["val"]])
    big = df.groupby(["metric", "concept", "start", "end", "filed"], dropna=False)["val"].transform("max")
    df["_small"] = (df["metric"] == "revenue") & (df["val"] < 0.9 * big)
    return df.sort_values(["metric", "start", "end", "prio", "filed", "_small", "_precise", "val"],
                          ascending=[True, True, True, False, True, False, False, True], na_position="first")


def known(df, as_of, splits=()):
    """For each (metric, period): the value from the best tag, latest version filed on/before as_of
    (several values in one filing: the most precise). Per-share figures and share counts are restated
    to the share count on as_of (splits between the filing and as_of), so quarters from before and
    after a split can be added up."""
    df = prepare(df)
    k = df[(df["filed"] <= as_of) & ~((df["metric"] == "revenue") & (df["val"] <= 0))]
    if k.empty:
        return k
    # rows are sorted by period, then worst->best tag, then oldest->newest filing: keep the last
    k = k.drop_duplicates(["metric", "start", "end"], keep="last")
    if splits:
        ps = k["metric"].isin(PER_SHARE | SHARE_COUNTS)
        if ps.any():
            k = k.copy()
            f = k.loc[ps, "filed"].map(lambda d: split_factor_between(d, as_of, splits))
            per = ps & k["metric"].isin(PER_SHARE)
            cnt = ps & k["metric"].isin(SHARE_COUNTS)
            k.loc[per, "val"] = k.loc[per, "val"] / f[per[ps].values]
            k.loc[cnt, "val"] = k.loc[cnt, "val"] * f[cnt[ps].values]
    return k


def _days(s, e):
    return (e - s).days


def _rows(d, mask):
    """d filtered by a list of booleans (works for an empty frame too)."""
    return d.loc[np.asarray(mask, dtype=bool)] if len(d) else d


def _is_year(d):
    return _rows(d, [Y_DAYS[0] <= _days(s, e) <= Y_DAYS[1] for s, e in zip(d["start"], d["end"])])


def quarters(k, metric):
    """{quarter end: (value, how)} of 3-month values; missing quarters derived from 6/9/12-month figures."""
    d = k[(k["metric"] == metric) & k["start"].notna()].copy()
    if d.empty:
        return {}
    d["days"] = [_days(s, e) for s, e in zip(d["start"], d["end"])]

    def pick(lo_hi):
        x = d[(d["days"] >= lo_hi[0]) & (d["days"] <= lo_hi[1])]
        return {(r.start, r.end): r.val for r in x.itertuples()}
    q, h, n, y = pick(Q_DAYS), pick(H_DAYS), pick(N_DAYS), pick(Y_DAYS)
    out = {e: (v, "reported") for (s, e), v in q.items()}
    starts = {e: s for (s, e) in q}
    if metric in SHARE_COUNTS:
        return dict(sorted(out.items()))

    def q_at(end, near_start=None):
        for e, (v, how) in out.items():
            if abs(_days(e, end)) <= 5:
                return e, v
        return None, None
    for longer, shorter_days in ((h, Q_DAYS), (n, H_DAYS), (y, N_DAYS)):
        for (s, e), v in longer.items():
            if q_at(e)[0] is not None:
                continue
            # the shorter period of the same fiscal year: same start, ends ~3 months earlier
            parts = {(s2, e2): v2 for (s2, e2), v2 in {**q, **h, **n}.items()
                     if abs(_days(s, s2)) <= 5 and shorter_days[0] <= _days(s2, e2) <= shorter_days[1]}
            if parts:
                (s2, e2), v2 = max(parts.items(), key=lambda kv: kv[0][1])
                if Q_GAP[0] <= _days(e2, e) <= Q_GAP[1]:
                    out[e] = (v - v2, "derived")
                    starts[e] = e2
                    continue
            if longer is y:                       # annual minus three reported quarters
                inside = sorted((qe, qv) for qe, (qv, how) in out.items() if s < qe < e and qe in starts)
                if len(inside) == 3:
                    out[e] = (v - sum(qv for _, qv in inside), "derived")
    return dict(sorted(out.items()))


def ttm(qs, end_max):
    """Sum of the 4 latest consecutive quarters ending on/before end_max; (value, last end, derived?)."""
    ends = [e for e in qs if e <= end_max]
    if len(ends) < 4:
        return None, None, False
    last4 = ends[-4:]
    if not all(Q_GAP[0] <= _days(a, b) <= Q_GAP[1] for a, b in zip(last4, last4[1:])):
        return None, None, False
    return sum(qs[e][0] for e in last4), last4[-1], any(qs[e][1] == "derived" for e in last4)


def annual_latest(k, metric):
    d = k[(k["metric"] == metric) & k["start"].notna()]
    d = _is_year(d)
    if d.empty:
        return None, None
    r = d.sort_values("end").iloc[-1]
    return r["val"], r["end"]


def instant(k, metric):
    d = k[(k["metric"] == metric) & k["start"].isna()]
    if d.empty:
        return {}
    return dict(zip(d["end"], d["val"]))


def _first(k, metric, on):
    """(value, tag) of the highest-priority tag of an instant metric on a date."""
    d = k[(k["metric"] == metric) & k["start"].isna() & (k["end"] == on)]
    if d.empty:
        return None, None
    r = d.sort_values("prio").iloc[0]
    return r["val"], r["concept"]


def total_debt(k, on):
    """Total debt on balance-sheet date `on`: one total tag, or non-current + current (+ short-term
    borrowings when the current tag doesn't include them). None when no debt tag is reported that day."""
    nc, _ = _first(k, "debt_noncurrent", on)
    cur, cur_tag = _first(k, "debt_current", on)
    stb, _ = _first(k, "short_borrowings", on)
    if nc is not None:
        extra = stb if (stb is not None and cur_tag != "DebtCurrent") else 0.0
        return nc + (cur or 0.0) + extra
    tot, tot_tag = _first(k, "debt_total", on)
    if tot is not None:
        return tot + (stb if stb is not None and tot_tag in ("LongTermDebt", "NotesPayable") else 0.0)
    other, _ = _first(k, "debt_other", on)
    fb, _ = _first(k, "debt_fallback", on)
    if other is not None or cur is not None or fb is not None:
        base = other if other is not None else (fb or 0.0)
        return base + (cur or 0.0 if other is None else 0.0) + (stb or 0.0)
    return stb


def debt_as_of(k, bs_date, equity):
    """(total debt, note) for the balance sheet of `bs_date`; see the module notes."""
    d = total_debt(k, bs_date)
    if d is not None:
        return d, "reported"
    dk = k[k["metric"].isin(DEBT_METRICS) & k["start"].isna() & (k["end"] <= bs_date)]
    dates = sorted(set(dk["end"]))
    last_val = total_debt(k, dates[-1]) if dates else None
    if dates and (bs_date - dates[-1]).days <= DEBT_LOOKBACK_DAYS:
        if not last_val:
            return 0.0, f"no debt (last debt figure {dates[-1]}: 0)"
        return last_val, f"debt from the {dates[-1]} balance sheet (not tagged on {bs_date})"
    ie = k[(k["metric"] == "interest_expense") & k["start"].notna() & (k["end"] <= bs_date)
           & (k["end"] > bs_date - dt.timedelta(days=730))]
    yearly = _is_year(ie) if len(ie) else ie
    interest = (yearly.sort_values("end").iloc[-1]["val"] if len(yearly) else
                ie.sort_values("end").iloc[-1]["val"] * 4 if len(ie) else 0.0)
    material = equity not in (None, 0) and abs(interest) > 0.002 * abs(equity)
    if not material and (not dates or not last_val):
        return 0.0, ("no debt reported" if not dates else f"no debt (last debt figure {dates[-1]}: 0)")
    if material:
        return None, "debt not in standard SEC tags (interest expense reported)"
    return None, f"debt last reported {dates[-1]} ({last_val / 1e9:,.2f} bn), not since"


def ttm_ytd(d, last_q, versions=None, others=None):
    """TTM of one metric/tag ending at last_q, the standard way:
    fiscal year just ended -> the annual figure; otherwise
    last annual + this year-to-date - the same year-to-date a year earlier.
    (The quarterly report gives both year-to-date figures on the same basis, so restatements for
    discontinued operations etc. cannot create a negative 'derived' quarter.)
    versions (all filed versions, optional): if last year's year-to-date was restated after the annual
    report and is now LOWER (a spin-off or sale moved to discontinued operations), the annual figure is
    restated by the same proportion, so all three figures are on today's basis (DuPont 2019, eBay 2015, HP 2015).
    Returns (value, how)."""
    d = d[d["start"].notna()]
    if d.empty:
        return None, None
    days = np.array([_days(s_, e) for s_, e in zip(d["start"], d["end"])])
    near = np.array([abs(_days(e, last_q)) <= 5 for e in d["end"]])
    year = (days >= Y_DAYS[0]) & (days <= Y_DAYS[1])
    a = d[near & year]
    if len(a):
        return float(a.sort_values("filed").iloc[-1]["val"]), "annual"
    ytd = d[near & (days >= Q_DAYS[0]) & (days <= N_DAYS[1])]
    if ytd.empty:
        return None, None
    ytd = ytd.assign(_d=[_days(s_, e) for s_, e in zip(ytd["start"], ytd["end"])]).sort_values(["_d", "filed"])
    cur = ytd.iloc[-1]
    fy_start, length = cur["start"], cur["_d"]
    one_year = dt.timedelta(days=364)
    prev = d[[abs(_days(e, last_q - one_year)) <= 10 and abs(_days(s_, fy_start - one_year)) <= 10
              and abs(_days(s_, e) - length) <= 10 for s_, e in zip(d["start"], d["end"])]]
    ann = d[year & np.array([abs(_days(e, fy_start - dt.timedelta(days=1))) <= 10 for e in d["end"]])]
    if prev.empty or ann.empty:
        return None, None
    p_row = prev.sort_values("filed").iloc[-1]
    a_row = ann.sort_values("filed").iloc[-1]
    annual, p_val, how = float(a_row["val"]), float(p_row["val"]), "annual + year-to-date"
    if versions is not None and len(versions):
        old = versions[(versions["start"] == p_row["start"]) & (versions["end"] == p_row["end"])
                       & (versions["filed"] <= a_row["filed"])]
        if len(old):
            last = old[old["filed"] == old["filed"].max()]
            o_val = float(last["val"].max())
            if o_val > 0 and p_val > 0 and 0.15 <= p_val / o_val < 0.98 \
                    and _really_restated(p_row, o_val, others) and not _annual_already_restated(a_row, versions):
                annual *= p_val / o_val
                how = "annual (restated) + year-to-date"
    return annual + float(cur["val"]) - p_val, how


def _really_restated(p_row, o_val, others):
    """A lower value for last year's year-to-date is a restatement only if no other tag in the same
    report still gives the old total for that period (C.H. Robinson 2018 tagged a segment's revenue
    as 'Revenues' next to the full total under another tag)."""
    if others is None or not len(others):
        return True
    same = others[(others["accn"] == p_row["accn"]) & (others["start"] == p_row["start"])
                  & (others["end"] == p_row["end"])]
    return not (same["val"] >= 0.98 * o_val).any()


def _annual_already_restated(a_row, versions):
    """The annual report was filed AFTER the business was removed if it restates an earlier period of
    that year downward (Bath & Body Works' 10-K for 2021 already left out Victoria's Secret, spun off
    in Aug 2021): then the annual figure needs no further adjustment."""
    mine = versions[versions["accn"] == a_row["accn"]]
    for r in mine.itertuples():
        if r.start is None or pd.isna(r.start) or r.end >= a_row["end"]:
            continue
        before = versions[(versions["start"] == r.start) & (versions["end"] == r.end)
                          & (versions["filed"] < a_row["filed"])]
        if len(before) and float(before.sort_values("filed").iloc[-1]["val"]) > 1.02 * r.val:
            return True
    return False


TOTAL_REVENUE_TAGS = ("RevenuesNetOfInterestExpense", "Revenues", "RegulatedAndUnregulatedOperatingRevenue")
RESTATE_METRICS = ("revenue", "op_income")


def _annuals(v):
    """{(fiscal year end, filing): value} of every full-year figure in all filed versions of one tag."""
    v = v[v["start"].notna()]
    if v.empty:
        return {}
    days = np.array([_days(s_, e) for s_, e in zip(v["start"], v["end"])])
    a = v[(days >= Y_DAYS[0]) & (days <= Y_DAYS[1])]
    out = {}
    for e, n, x in zip(a["end"], a["accn"], a["val"]):           # two values in one report: the larger
        out[(e, n)] = max(x, out.get((e, n), x))
    return out


EXCL_TAX = "RevenueFromContractWithCustomerExcludingAssessedTax"
INCL_TAX = "RevenueFromContractWithCustomerIncludingAssessedTax"


def component_tags(annuals):
    """Revenue tags that cover only PART of the company's revenue: in the latest annual report that
    gives the tag and another tag for the same year, the tag is less than half of the other (a bank's
    lease income next to its total revenue). Only reports from the tag's last two years count. Compared within one report only, so restated years and
    spin-offs don't count. Revenue excluding excise taxes is never a part of revenue including them
    (Philip Morris: 35B net of excise vs 80B gross)."""
    comp = set()
    for c, ac in annuals.items():
        if not ac:
            continue
        for d_, ad in annuals.items():
            if d_ == c or (c == EXCL_TAX and d_ in (INCL_TAX, "Revenues", "SalesRevenueNet")):
                continue
            common = sorted(set(ac) & set(ad))             # same year in the same report
            recent = max(e for e, _ in ac) - dt.timedelta(days=800)
            if common and common[-1][0] >= recent and ac[common[-1]] < 0.5 * ad[common[-1]]:
                comp.add(c)
                break
    return comp


def best_ttm(df, metric, as_of, last_q, splits=(), cache=None):
    """(value, tag, how) of a metric's TTM at last_q. Each tag is tried on its own (never mixing two
    tags in one sum). Revenue: banks' net revenue if reported; tags that cover only part of revenue are
    left out (component_tags); revenue excluding sales/excise taxes is used when the company reports
    both; then the LARGEST total among the remaining tags. Other metrics: the first tag in priority order."""
    tags = DURATION.get(metric, []) + IFRS.get(metric, [])
    found, annuals, kept = {}, {}, {}
    for c in tags:
        sub = _subset(df, metric, c, cache)
        if sub.empty:
            continue
        k_c = known(sub, as_of, splits)
        if k_c.empty:
            continue
        versions = others = None
        if metric in RESTATE_METRICS:
            versions = sub[(sub["filed"] <= as_of) & ~((sub["metric"] == "revenue") & (sub["val"] <= 0))]
            others = _metric_rows(df, metric, as_of, cache)
        v, how = ttm_ytd(k_c, last_q, versions, others)
        if metric != "revenue":
            if v is not None:
                return v, c, how
            continue
        annuals[c] = _annuals(sub[sub["filed"] <= as_of])
        kept[c] = k_c
        if v is not None:
            if c == "RevenuesNetOfInterestExpense":
                return v, c, how
            found[c] = (v, c, how)
    if metric != "revenue":
        return None, None, None
    if not _subset(df, "noninterest_income", "NoninterestIncome", cache).empty:
        # a bank: net interest income + non-interest income (net revenue, like JPM's net tag)
        nii = best_ttm(df, "net_interest_income", as_of, last_q, splits, cache)
        non = best_ttm(df, "noninterest_income", as_of, last_q, splits, cache)
        if nii[0] is not None and non[0] is not None and nii[0] + non[0] > 0:
            return nii[0] + non[0], f"{nii[1]} + {non[1]}", nii[2]
    comp = component_tags(annuals)
    if cache is not None:
        cache[("components", as_of)] = comp
    found = {c: x for c, x in found.items() if c not in comp}
    oi = best_ttm(df, "op_income", as_of, last_q, splits, cache)[0]
    if oi is not None and oi > 0:                        # revenue can't be below operating income
        found = {c: x for c, x in found.items() if x[0] >= oi}
    excl, incl = EXCL_TAX, INCL_TAX
    if excl in found and incl in found and found[incl][0] > found[excl][0] * 1.02:
        gross = found.pop(incl)[0]                       # gross of excise taxes (Philip Morris, Altria)
        for c in [c for c, x in found.items() if c != excl and abs(x[0] / gross - 1) <= 0.02]:
            found.pop(c)
    if not any(t in found for t in TOTAL_REVENUE_TAGS):
        # a bank may start tagging net revenue mid-year (JPM 2015): last year's total under "Revenues"
        # + this year's year-to-date under the net tag - only if the two tags agree on a common year
        a1, a2 = annuals.get(TOTAL_REVENUE_TAGS[0], {}), annuals.get(TOTAL_REVENUE_TAGS[1], {})
        common = sorted(set(a1) & set(a2))
        if common and a2[common[-1]] and abs(a1[common[-1]] / a2[common[-1]] - 1) <= 0.05:
            both = pd.concat([kept[TOTAL_REVENUE_TAGS[0]].assign(prio=0), kept[TOTAL_REVENUE_TAGS[1]].assign(prio=1)])
            v, how = ttm_ytd(known(both.drop(columns=["_precise", "_small"], errors="ignore"), as_of), last_q)
            if v is not None:
                return v, "RevenuesNetOfInterestExpense + Revenues", how
    if not found:
        return None, None, None
    return max(found.values(), key=lambda x: x[0])


def _metric_rows(df, metric, as_of, cache):
    """All filed values of a metric (every tag) up to as_of."""
    key = ("metric", metric)
    if cache is None or key not in cache:
        rows = df[(df["metric"] == metric) & (df["val"] > 0)]
        if cache is None:
            return rows[rows["filed"] <= as_of]
        cache[key] = rows
    rows = cache[key]
    return rows[rows["filed"] <= as_of]


def _subset(df, metric, concept, cache):
    if cache is None:
        return df[(df["metric"] == metric) & (df["concept"] == concept)]
    key = (metric, concept)
    if key not in cache:
        cache[key] = df[(df["metric"] == metric) & (df["concept"] == concept)]
    return cache[key]


def snapshot(df, as_of, splits=(), cache=None):
    """Fundamentals as known on `as_of` (only reports filed on or before it). EPS is in the share
    count of as_of; use eps_today_basis() to compare it with today's split-adjusted prices."""
    k = known(df, as_of, splits)
    if k.empty:
        return None
    units = k[k["metric"] == "revenue"]["unit"]
    out = {"currency": units.iloc[-1] if len(units) else None}
    q = {m: quarters(k, m) for m in DURATION}
    last_q = max((max(v) for v in q.values() if v), default=None)
    annual_only = last_q is None
    out["annual_only"] = annual_only
    derived = False
    if not annual_only:
        out["period_end"] = last_q
        for m in DURATION:
            if m in SHARE_COUNTS or m in REVENUE_PARTS:
                continue
            val, tag, how = best_ttm(df, m, as_of, last_q, splits, cache)
            if val is None:                               # fall back: sum of the last 4 quarters
                qm = q[m]
                comp = (cache or {}).get(("components", as_of)) if m == "revenue" else None
                if comp:                                  # never from a tag covering part of revenue
                    qm = quarters(k[~k["concept"].isin(comp)], m)
                val, end, der = ttm(qm, last_q)
                if end != last_q:
                    val = None
                elif m == "revenue":                      # one quarter can't be more than half the year
                    last4 = [v for e, (v, _) in qm.items() if e <= last_q][-4:]
                    if any(v > 0.5 * val for v in last4) or val <= 0:
                        val = None
                derived |= der
                how = "sum of 4 quarters" if val is not None else None
            out[f"{m}_ttm"] = val
            if m == "revenue":
                out["revenue_tag"], out["revenue_ttm_how"] = tag, how
        # quarterly revenue growth: this quarter vs the same quarter a year earlier, from the SAME tag
        tag = out.get("revenue_tag")
        rq = q["revenue"]
        if tag and " + " not in tag:
            rq = quarters(known(_subset(df, "revenue", tag, cache), as_of), "revenue")
        elif tag:                                       # two tags: quarters of both, added up
            parts = []
            for c in tag.split(" + "):
                m_c = "revenue" if c in DURATION["revenue"] else next(
                    (m for m in REVENUE_PARTS if c in DURATION[m]), "revenue")
                parts.append(quarters(known(_subset(df, m_c, c, cache), as_of), m_c))
            if "Revenues" in tag and "RevenuesNetOfInterestExpense" in tag:
                rq = parts[0]                            # JPM 2015: same total, net tag for quarters
            else:
                rq = {e: (sum(p_[e][0] for p_ in parts), "derived" if any(p_[e][1] == "derived" for p_ in parts)
                          else "reported") for e in set.intersection(*[set(p_) for p_ in parts])}
        out["revenue_q"] = rq[last_q][0] if last_q in rq else None
        out["revenue_q_derived"] = rq[last_q][1] == "derived" if last_q in rq else None
        prev = [e for e in rq if 350 <= _days(e, last_q) <= 380]
        out["revenue_q_year_ago"] = rq[prev[-1]][0] if prev else None
        r_ttm = out.get("revenue_ttm")
        for key in ("revenue_q", "revenue_q_year_ago"):        # an implausible derived quarter is not used
            v = out[key]
            if v is not None and (v <= 0 or (r_ttm and r_ttm > 0 and v > 0.5 * r_ttm)):
                out[key] = None
        if tag and prev and " + " not in tag:
            sub = _subset(df, "revenue", tag, cache)
            out["revenue_ttm_year_ago"] = ttm_ytd(known(sub, as_of), prev[-1],
                                                  sub[(sub["filed"] <= as_of) & (sub["val"] > 0)],
                                                  _metric_rows(df, "revenue", as_of, cache))[0]
        elif tag and prev:
            out["revenue_ttm_year_ago"] = best_ttm(df, "revenue", as_of, prev[-1], splits, cache)[0]
        else:
            out["revenue_ttm_year_ago"] = ttm(rq, prev[-1])[0] if prev else None
    else:
        latest = {m: annual_latest(k, m) for m in DURATION if m not in SHARE_COUNTS | REVENUE_PARTS}
        ends = [e for _, e in latest.values() if e]
        out["period_end"] = max(ends) if ends else None
        for m, (val, end) in latest.items():
            out[f"{m}_ttm"] = val if end == out["period_end"] else None      # stale years are not used
        out["revenue_q"] = out["revenue_q_year_ago"] = None
        out["revenue_q_derived"], out["revenue_tag"], out["revenue_ttm_how"] = None, None, "annual"
        rev = k[(k["metric"] == "revenue") & k["start"].notna()]
        rev = _is_year(rev)
        rev = rev.sort_values("end") if len(rev) else rev
        out["revenue_ttm_year_ago"] = rev.iloc[-2]["val"] if len(rev) >= 2 else None
    out["ttm_has_derived_quarter"] = derived
    out["eps_source"] = "reported" if out.get("eps_diluted_ttm") is not None else None
    if out.get("eps_diluted_ttm") is None and out.get("net_income_ttm") is not None and out["period_end"]:
        sh = q["shares_diluted"].get(out["period_end"], (None,))[0] if not annual_only else None
        if sh is None:
            ann = annual_latest(k, "shares_diluted")
            sh = ann[0] if ann[1] == out["period_end"] else None
        if sh:
            out["eps_diluted_ttm"] = out["net_income_ttm"] / sh
            out["eps_source"] = "net income / diluted shares"
    eq = instant(k, "equity")
    bs_date = max(eq) if eq else None
    out["balance_sheet_date"] = bs_date
    out["equity"] = eq.get(bs_date) if bs_date else None
    out["total_debt"], out["debt_note"] = debt_as_of(k, bs_date, out["equity"]) if bs_date else (None, None)
    out["debt_to_equity"] = (out["total_debt"] / out["equity"]
                             if out["total_debt"] is not None and out["equity"] not in (None, 0) else None)
    r, oi = out.get("revenue_ttm"), out.get("op_income_ttm")
    out["op_margin_ttm"] = oi / r if r and oi is not None else None
    rq, rqa = out.get("revenue_q"), out.get("revenue_q_year_ago")
    out["revenue_growth_q_yoy"] = rq / rqa - 1 if rq is not None and rqa else None
    rt, rta = out.get("revenue_ttm"), out.get("revenue_ttm_year_ago")
    out["revenue_growth_ttm_yoy"] = rt / rta - 1 if rt is not None and rta else None
    return out


def snapshots(df, min_filed=None, splits=()):
    """One row per filing date (from min_filed on): the fundamentals as known right after that filing."""
    if df.empty:
        return pd.DataFrame()
    df = prepare(df)
    cache = {}
    rows = []
    for filed, g in df.groupby("filed"):
        if min_filed and filed < min_filed:
            continue
        s = snapshot(df, filed, splits, cache)
        if s:
            s["filed"] = filed
            s["accn"] = ", ".join(sorted(set(g["accn"].dropna())))[:200]
            s["form"] = ", ".join(sorted(set(g["form"])))
            s["cik"] = int(g["cik"].iloc[0])
            rows.append(s)
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ PE with split-adjusted prices
def real_splits(actions):
    """[(ex-date, ratio)] of ordinary share splits (2:1, 1:3 ...) from stored corporate actions;
    spin-off adjustments that a price source records as odd 'splits' are left out."""
    from src.checks.price_audit import clean_ratio
    return sorted((d, float(v)) for d, v in actions if v and clean_ratio(float(v)))


def split_factor_between(after, upto, splits):
    """Product of split ratios with an ex-date in (after, upto]."""
    f = 1.0
    for d, ratio in splits:
        if after < d <= upto:
            f *= ratio
    return f


def split_factor_after(day, splits):
    """Product of split ratios (e.g. 4.0 for 4-for-1) with an ex-date after `day`."""
    f = 1.0
    for d, ratio in splits:
        if d > day and ratio and ratio > 0:
            f *= ratio
    return f


def eps_today_basis(eps, filed, splits):
    """EPS from a report filed on `filed`, restated to today's share count (the basis of our split-adjusted
    prices): divided by every split after the filing date."""
    return None if eps is None else eps / split_factor_after(filed, splits)


def pe_ratio(close_adj, eps, filed, splits):
    """Price / TTM EPS on a day, with the stored (split-adjusted) close. None when EPS <= 0."""
    e = eps_today_basis(eps, filed, splits)
    return close_adj / e if e and e > 0 else None
