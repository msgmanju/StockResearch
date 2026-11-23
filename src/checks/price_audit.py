"""
Price audit - implements docs/AUDIT_PROCEDURE.md.

Check results:  PASS | INFO (recorded, never blocks) | REVIEW (needs a person) | FAIL (blocks)
Issue results:  FAIL | REVIEW | INFO | RESOLVED (handled by an automatic rule)

Tier 1 (blocking): data_present, raw_vs_db, ohlc_sanity, trading_days, repeated_days,
                   split_days, splits vs second source, close vs second source
Tier 2 (automatic, else review): big_moves, high_low
Tier 3 (information): open/volume differences, dividends vs second source, adj_close

Stock status: VERIFIED | VERIFIED - SINGLE SOURCE | NEEDS REVIEW | BLOCKED | NO DATA
"""
import datetime as dt
import re
import tomllib
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src import corrections
from src.common import RAW_DIR, ROOT, SETTINGS, START_DATE as START, run
from src.collect.second_sources import SOURCES
from src.collect import tiingo_prices

CFG = {
    "second_sources": ["tiingo", "nasdaq"],
    "compare_tolerance_pct": 0.5,
    "volume_tolerance_pct": 5.0,
    "adj_close_tolerance_pct": 0.1,
    "big_move_pct": 15.0,
    "split_day_max_move_pct": 20.0,
    "bad_tick_pct": 5.0,
    "stale_days": 5,
    "min_days": 252,
    "max_issues_per_check": 200,
    "tie_minor_pct": 1.0,
    "tie_max_items": 5,
}
CFG.update(SETTINGS.get("audit", {}))
NY = ZoneInfo(SETTINGS["market"]["timezone"])
REVIEWED_FILE = ROOT / "config" / "audit_reviewed.toml"

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_runs (
    run_id VARCHAR, run_at TIMESTAMP, ticker VARCHAR, dataset VARCHAR,
    check_name VARCHAR, status VARCHAR, n_checked INTEGER, n_failed INTEGER, detail VARCHAR
);
CREATE TABLE IF NOT EXISTS audit_issues (
    run_id VARCHAR, ticker VARCHAR, check_name VARCHAR, date DATE, field VARCHAR,
    our_value DOUBLE, other_value DOUBLE, diff_pct DOUBLE, note VARCHAR
);
CREATE TABLE IF NOT EXISTS stock_status (
    ticker VARCHAR PRIMARY KEY, status VARCHAR, reason VARCHAR, price_source VARCHAR,
    first_date DATE, last_date DATE, audit_type VARCHAR, run_id VARCHAR, audited_at TIMESTAMP
);
"""
ORDER = {"PASS": 0, "INFO": 1, "REVIEW": 2, "FAIL": 3}


# ------------------------------------------------------------------ bookkeeping
class Audit:
    def __init__(self, ticker, source):
        self.ticker, self.source = ticker, source
        self.corrected_dates = set()
        self.results, self.issues, self.compare, self.disputes = [], [], {}, []
        self.summary, self.stock_status, self.reason = {}, None, ""

    def add(self, check, status, n_checked, n_failed, detail):
        self.results.append({"check": check, "status": status, "n_checked": int(n_checked),
                             "n_failed": int(n_failed), "detail": detail})

    def issue(self, check, date, field, ours, other, result, note):
        if sum(1 for i in self.issues if i["check"] == check) >= CFG["max_issues_per_check"]:
            return
        diff = (ours / other - 1) * 100 if _ok(ours) and _ok(other) and other else None
        self.issues.append({"check": check, "date": date, "field": field, "our_value": _f(ours),
                            "other_value": _f(other), "diff_pct": diff, "result": result, "note": note})


def _ok(x):
    return x is not None and not (isinstance(x, float) and np.isnan(x))


def _f(x):
    return float(x) if _ok(x) else None


def _status(fails, reviews=0, infos=0):
    return "FAIL" if fails else "REVIEW" if reviews else "INFO" if infos else "PASS"


# ------------------------------------------------------------------ loading
def load_prices(con, ticker):
    df = con.execute("""SELECT date, open, high, low, close, adj_close, volume
                        FROM prices_daily WHERE ticker = ? ORDER BY date""", [ticker]).df()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_actions(con, ticker):
    df = con.execute("SELECT date, action, value FROM corporate_actions WHERE ticker = ? ORDER BY date",
                     [ticker]).df()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def split_factor_after(dates, actions):
    """For each date, product of split ratios that happen AFTER it."""
    splits = actions[actions["action"] == "split"]
    f = np.ones(len(dates))
    for d, v in zip(splits["date"], splits["value"]):
        if v and v > 0:
            f[np.array([x < d for x in dates])] *= v
    return f


# ------------------------------------------------------------------ Tier 1
def check_data_present(a, px, latest=None):
    n = len(px)
    ok = n >= CFG["min_days"]
    new_listing = (not ok and latest is not None and px["date"].max() >= latest - dt.timedelta(days=10)
                   and px["date"].min() > START + dt.timedelta(days=7))
    if new_listing:
        a.add("data_present", "INFO", n, 0, f"{n:,} days stored - a new listing (first trade {px['date'].min()}); "
              f"52-week values start once it has a year of prices")
        return
    a.add("data_present", "PASS" if ok else "FAIL", n, 0 if ok else 1,
          f"{n:,} days stored" + ("" if ok else f" - less than {CFG['min_days']} (one year)"))


def _raw_symbol(con, ticker):
    """The symbol the stored prices were downloaded under (another symbol, e.g. FRCB for FRC, is
    recorded in load_log when config/price_symbols.toml was used)."""
    r = run(con, """SELECT message FROM load_log WHERE ticker = ? AND status = 'OK'
                    ORDER BY run_at DESC LIMIT 1""", [ticker])
    m = re.search(r"from symbol (\S+)", (r[0][0] or "") if r else "")
    return m.group(1) if m else ticker


def _expected_from_raw(a, symbol=None):
    """Stored values as they should be, rebuilt from the saved original downloads."""
    symbol = symbol or a.ticker
    if a.source == tiingo_prices.SOURCE:
        raw, files = tiingo_prices.replay_raw(RAW_DIR, symbol)
        if raw is None:
            return None, 0
        return raw.set_index("date")[["open", "high", "low", "close", "volume"]], len(files)
    files = sorted((RAW_DIR / "yahoo" / "prices" / symbol).glob("*.csv"))
    if not files:
        return None, 0
    frames = []
    for i, f in enumerate(files):
        r = pd.read_csv(f)
        r["date"] = pd.to_datetime(r["Date"], utc=True).dt.tz_convert(NY).dt.date
        r["_order"] = i
        frames.append(r)
    raw = (pd.concat(frames).sort_values("_order").drop_duplicates("date", keep="last").set_index("date")
           .rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}))
    return raw[["open", "high", "low", "close", "volume"]], len(files)


def check_raw_vs_db(a, px, con):
    symbol = _raw_symbol(con, a.ticker)
    exp, n_files = _expected_from_raw(a, symbol)
    if exp is None:
        a.add("raw_vs_db", "FAIL", 0, 1, "no original download files found")
        return
    exp = exp.copy()
    corr = corrections.for_ticker(con, a.ticker)
    for (d, field), (value, origin, note) in corr.items():
        if d in exp.index and field in exp.columns:
            exp.at[d, field] = value
    m = px.set_index("date").join(exp, how="left", rsuffix="_raw")
    missing = m["close_raw"].isna()
    bad = 0
    for col in ("open", "high", "low", "close", "volume"):
        diff = ~np.isclose(m[col].astype(float), m[f"{col}_raw"].astype(float), rtol=1e-9, atol=1e-9) & ~missing
        for d in m.index[diff]:
            a.issue("raw_vs_db", d, col, m.at[d, col], m.at[d, f"{col}_raw"], "FAIL",
                    "database differs from original download")
        bad += int(diff.sum())
    for d in m.index[missing]:
        a.issue("raw_vs_db", d, "date", None, None, "FAIL", "row in database but not in any original download")
    n_bad = bad + int(missing.sum())
    a.add("raw_vs_db", _status(n_bad), len(m), n_bad,
          f"{n_files} original download file(s) from {a.source}"
          + (f" (symbol {symbol})" if symbol != a.ticker else "") + " replayed and compared field by field"
          + (f"; {len(corr)} correction(s) taken into account" if corr else ""))


def check_ohlc(a, px):
    # a bar whose high, low and close agree, with only the OPEN outside the day's range: the open is
    # not used to pick or exit trades, so this is information only
    hl_ok = (px["low"] <= px["close"] + 1e-9) & (px["close"] <= px["high"] + 1e-9) & (px["low"] <= px["high"])
    open_out = ((px["open"] > px["high"] + 1e-9) | (px["open"] < px["low"] - 1e-9)) & hl_ok
    for i in px.index[open_out]:
        a.issue("ohlc_sanity", px.at[i, "date"], "open", px.at[i, "open"], None, "INFO",
                f"open {px.at[i, 'open']:.4f} outside the day's low-high {px.at[i, 'low']:.4f}-{px.at[i, 'high']:.4f}; "
                f"close/high/low consistent (open is not used by the strategy)")
    rules = {
        "high below close/low": (px["high"] < px[["close", "low"]].max(axis=1) - 1e-9),
        "low above close": (px["low"] > px["close"] + 1e-9),
        "zero or negative price": (px[["open", "high", "low", "close"]] <= 0).any(axis=1),
        "missing value": px[["open", "high", "low", "close", "volume"]].isna().any(axis=1),
    }
    fails = pd.Series(False, index=px.index)
    for note, mask in rules.items():
        for i in px.index[mask]:
            a.issue("ohlc_sanity", px.at[i, "date"], "bar", px.at[i, "close"], None, "FAIL", note)
        fails |= mask
    zero_vol = (px["volume"] <= 0) & ~fails
    for i in px.index[zero_vol]:
        a.issue("ohlc_sanity", px.at[i, "date"], "volume", px.at[i, "volume"], None, "INFO", "zero volume")
    a.add("ohlc_sanity", _status(int(fails.sum()), 0, int(zero_vol.sum()) + int(open_out.sum())), len(px),
          int(fails.sum()),
          f"high/low/close consistent, prices positive; zero-volume days {int(zero_vol.sum())} (info)"
          + (f"; open outside the day's range on {int(open_out.sum())} day(s) (info)" if open_out.any() else ""))


def check_trading_days(a, px, joined=None, left=None):
    try:
        import exchange_calendars as xc
    except ImportError:
        a.add("trading_days", "REVIEW", 0, 1, "skipped: run  pip install exchange_calendars")
        return
    cal = xc.get_calendar("XNYS")
    first, last = px["date"].min(), px["date"].max()
    sessions = {d.date() for d in cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))}
    ours = set(px["date"])
    missing, extra = sorted(sessions - ours), sorted(ours - sessions)
    early = {d.date() for d in cal.early_closes} if hasattr(cal, "early_closes") else set()
    first30 = set(sorted(sessions)[:30])
    n_info = 0
    for d in missing:
        if d in early:
            a.issue("trading_days", d, "date", None, None, "INFO", "half-day session missing (early close) - info")
            n_info += 1
        elif left is not None and left - dt.timedelta(days=10) <= d <= left + dt.timedelta(days=21):
            a.issue("trading_days", d, "date", None, None, "INFO",
                    f"no trading around the stock's index exit ({left}) - halted, taken over or delisted - info")
            n_info += 1
        elif d in first30 and joined is not None and d < joined:
            a.issue("trading_days", d, "date", None, None, "INFO",
                    "gap in the first weeks of trading (when-issued), before the stock joined the index - info")
            n_info += 1
        else:
            a.issue("trading_days", d, "date", None, None, "FAIL", "NYSE trading day missing from our data")
    missing_fail = len(missing) - n_info
    n_rev = 0
    if 0 < missing_fail <= 2 and not extra:      # one or two days: often a trading halt - a person checks
        for i in a.issues:
            if i["check"] == "trading_days" and i["result"] == "FAIL":
                i["result"] = "REVIEW"
                i["note"] += " (1-2 days only: a trading halt? check)"
        n_rev, missing_fail = missing_fail, 0
    for d in extra:
        a.issue("trading_days", d, "date", None, None, "FAIL", "date in our data is not an NYSE trading day")
    a.add("trading_days", _status(missing_fail + len(extra), n_rev, n_info), len(sessions),
          missing_fail + len(extra) + n_rev,
          f"{len(sessions)} NYSE sessions {first} to {last}; missing {len(missing)}"
          + (f" ({n_info} half-day/when-issued, info)" if n_info else "") + f", extra {len(extra)}")


def check_repeated(a, px):
    same_bar = (px[["open", "high", "low", "close", "volume"]].diff().abs().sum(axis=1) == 0)
    same_bar.iloc[0] = False
    for i in px.index[same_bar]:
        a.issue("repeated_days", px.at[i, "date"], "bar", px.at[i, "close"], None, "FAIL",
                "identical to previous day (copied forward?)")
    run_len = (px["close"].diff() == 0).astype(int)
    run_len = run_len.groupby((run_len == 0).cumsum()).cumsum()
    frozen = (run_len >= CFG["stale_days"]) & ~same_bar
    for i in px.index[frozen]:
        a.issue("repeated_days", px.at[i, "date"], "close", px.at[i, "close"], None, "REVIEW",
                f"close unchanged for {run_len[i]} days")
    a.add("repeated_days", _status(int(same_bar.sum()), int(frozen.sum())), len(px),
          int(same_bar.sum() + frozen.sum()), "no repeated bars, no frozen closes")


def check_splits(a, px, actions):
    splits = actions[actions["action"] == "split"]
    if splits.empty:
        a.add("split_days", "PASS", 0, 0, "no splits in this period")
        return
    ret = px.set_index("date")["close"].pct_change()
    lim = CFG["split_day_max_move_pct"] / 100
    bad = 0
    for d, v in zip(splits["date"], splits["value"]):
        r = ret.get(d)
        if d <= px["date"].min():
            a.issue("split_days", d, "close", None, None, "INFO",
                    f"split {v:g}:1 on/before the first stored day - nothing to check")
        elif r is None or np.isnan(r):
            a.issue("split_days", d, "close", None, None, "FAIL", f"split {v:g}:1 date not in price data")
            bad += 1
        elif abs(r) > lim:
            a.issue("split_days", d, "close", r * 100, None, "FAIL",
                    f"{r:+.1%} move on {v:g}:1 split day - history may not be split-adjusted")
            bad += 1
    desc = ", ".join(f"{d} {v:g}:1" for d, v in zip(splits["date"], splits["value"]))
    a.add("split_days", _status(bad), len(splits), bad, f"splits checked: {desc}")


# ------------------------------------------------------------------ Tier 3
def check_adj_close(a, px, actions):
    divs = actions[actions["action"] == "dividend"]
    close = px.set_index("date")["close"]
    factor = pd.Series(1.0, index=close.index)
    for d, v in zip(divs["date"], divs["value"]):
        prev = close[close.index < d]
        if not prev.empty:
            factor[factor.index < d] *= 1 - v / prev.iloc[-1]
    calc = close * factor
    adj = px.set_index("date")["adj_close"]
    diff = (adj / calc - 1).abs()
    bad = diff > CFG["adj_close_tolerance_pct"] / 100
    for d in diff.index[bad]:
        a.issue("adj_close", d, "adj_close", adj[d], calc[d], "INFO",
                "adj_close differs from recalculation (dividend data) - not used by the strategy")
    a.add("adj_close", _status(0, 0, int(bad.sum())), len(close), int(bad.sum()),
          f"recalculated from {len(divs)} dividends; largest difference {diff.max() * 100:.3f}% (info only)")


# ------------------------------------------------------------------ second sources
def clean_ratio(r):
    """True for an ordinary split ratio (2:1, 3:2, 1:3, 4:3 ...); spin-off adjustments are not."""
    if not r or r <= 0:
        return False
    for x in (r, 1 / r):
        for den in (1, 2, 3, 4):
            num = round(x * den)
            if num > 0 and num != den and abs(x * den - num) < 0.0005 * x * den:
                return True
    return False


def check_actions_vs(a, px, actions, name, other):
    check = f"actions_vs_{name}"
    lo, hi = max(px["date"].min(), other["date"].min()), min(px["date"].max(), other["date"].max())
    o = other[(other["date"] >= lo) & (other["date"] <= hi)]
    ours = actions[(actions["date"] >= lo) & (actions["date"] <= hi)]
    o_splits = {d: float(v) for d, v in zip(o["date"], o["split_factor"]) if v and abs(float(v) - 1) > 1e-9}
    y_splits = {d: float(v) for d, v in zip(ours.loc[ours.action == "split", "date"],
                                            ours.loc[ours.action == "split", "value"])}
    later = {d: 1.0 for d in o["date"]}
    for sd, sv in o_splits.items():
        for d in later:
            if d < sd:
                later[d] *= sv
    o_divs = {d: float(v) / later[d] for d, v in zip(o["date"], o["div_cash"]) if v and float(v) > 0}
    y_divs = {d: float(v) for d, v in zip(ours.loc[ours.action == "dividend", "date"],
                                          ours.loc[ours.action == "dividend", "value"])}
    n_split_bad = n_div_bad = 0
    ret_on = px.set_index("date")["close"].pct_change().to_dict()
    for kind, mine, theirs, tol, result in (("split", y_splits, o_splits, 1e-6, "FAIL"),
                                            ("dividend", y_divs, o_divs, 0.01, "INFO")):
        for d in sorted(set(mine) | set(theirs)):
            m, t = mine.get(d), theirs.get(d)
            if m is None:
                note = f"{name} reports a {kind} that our source does not have"
            elif t is None:
                note = f"our source reports a {kind} that {name} does not have"
            elif abs(m / t - 1) > tol:
                note = f"{kind} amount differs from {name}"
            else:
                continue
            res = result
            if kind == "split":
                extra = (m if t is None else t if m is None else m / t)
                r_day = ret_on.get(d)
                if d <= px["date"].min():
                    r_day = 0.0                              # first stored day: nothing to jump from
                if not clean_ratio(extra) and r_day is not None and abs(r_day) < CFG["big_move_pct"] / 100:
                    res = "INFO"
                    note += (f" - factor {extra:.4f} is a spin-off/special-distribution adjustment, not a "
                             f"share split; our prices are continuous that day ({r_day:+.1%})")
            a.issue(check, d, kind, m, t, res,
                    note + (" (only affects adj_close - info)" if kind == "dividend" else ""))
            if kind == "split" and res == "FAIL":
                n_split_bad += 1
            elif kind == "dividend":
                n_div_bad += 1
    a.add(check, _status(n_split_bad, 0, n_div_bad),
          len(set(y_splits) | set(o_splits)) + len(set(y_divs) | set(o_divs)), n_split_bad,
          f"splits {len(y_splits)} ours vs {len(o_splits)} {name} ({n_split_bad} differ); "
          f"dividends {len(y_divs)} vs {len(o_divs)} ({n_div_bad} differ - info only); {lo} to {hi}")


def compare_source(a, px, actions, name, fetch, con=None):
    check = f"compare_{name}"
    start, end = px["date"].min(), px["date"].max()
    try:
        other = fetch(a.ticker, start, end)
    except Exception as e:
        a.add(check, "INFO", 0, 0, f"could not download from {name}: {type(e).__name__}: {str(e)[:150]}")
        return
    if other is None or other.empty:
        a.add(check, "INFO", 0, 0, f"{name} returned no data")
        return
    if "div_cash" in other.columns:
        check_actions_vs(a, px, actions, name, other)
    split_f = other["split_factor"].tolist() if "split_factor" in other.columns else None
    other = other[["date", "open", "high", "low", "close", "volume"]]
    m = px.merge(other, on="date", how="inner", suffixes=("", "_o"))
    F = split_factor_after(list(m["date"]), actions)
    tol = CFG["compare_tolerance_pct"] / 100
    # Always compare OUR close (what the strategy uses). The only question is whether the
    # other source's history is split-adjusted already or shows the actual traded prices.
    options = {
        "close vs close": (m["close"], m["close_o"], np.ones(len(m))),
        "close vs close / later splits": (m["close"], m["close_o"] / F, F),
    }
    basis = max(options, key=lambda k: (((options[k][0] / options[k][1] - 1).abs() <= tol).sum(),
                                        -(options[k][0] / options[k][1] - 1).abs().median()))
    ours_c, other_c, scale = options[basis]
    k = np.ones(len(m))

    d_close = (ours_c / other_c - 1).abs()
    bad_close = d_close > tol
    factors = {float(v) for v in actions.loc[actions["action"] == "split", "value"]} | \
        {float(v) for v in (split_f if split_f is not None else []) if v and abs(float(v) - 1) > 1e-9}
    explained = pd.Series(False, index=m.index)
    factor_of = {}
    close_rule_on = int(bad_close.sum()) <= CFG["tie_max_items"]      # isolated disagreements only
    for i in m.index[bad_close]:
        ratio = ours_c[i] / other_c[i]
        f = next((f for f in factors if not clean_ratio(f) and
                  (abs(ratio * f - 1) < 0.003 or abs(ratio / f - 1) < 0.003)), None)
        if f is not None:
            explained[i] = True
            factor_of[i] = f
            a.issue(check, m.at[i, "date"], "close", ours_c[i], other_c[i], "INFO",
                    f"differs by exactly the {f:.4f} spin-off adjustment factor - one source adjusts "
                    f"older prices for the spin-off, the other doesn't (not a price error)")
            continue
        # a copied-forward bar (same price as the day before, no real volume) is the wrong one
        prev_ours = m["close"].shift(1)[i] if i in m.index else None
        prev_ours = None if prev_ours is None or pd.isna(prev_ours) else float(prev_ours)
        our_flat = (m.at[i, "open"] == m.at[i, "close"] == m.at[i, "high"] == m.at[i, "low"]
                    and prev_ours is not None and abs(m.at[i, "close"] - prev_ours) < 1e-9
                    and m.at[i, "volume"] < 0.01 * max(1.0, float(m["volume"].median())))
        their_stale = prev_ours is not None and abs(other_c[i] / prev_ours - 1) < 1e-6 and m.at[i, "volume"] > 0
        if our_flat and con is not None:
            sc = float(scale[i]) if hasattr(scale, "__getitem__") else 1.0
            for f in ("open", "high", "low", "close"):
                corrections.add_auto(con, a.ticker, m.at[i, "date"], f, float(m.at[i, f"{f}_o"]) / sc, name,
                                     f"auto: our bar was copied forward from the day before (no volume)")
            corrections.add_auto(con, a.ticker, m.at[i, "date"], "volume", float(m.at[i, "volume_o"]) * sc, name,
                                 "auto: our bar was copied forward from the day before (no volume)")
            corrections.apply(con, a.ticker)
            explained[i] = True
            a.issue(check, m.at[i, "date"], "close", ours_c[i], other_c[i], "RESOLVED",
                    f"OUR bar was copied forward from the day before with no volume - replaced by {name}'s bar "
                    f"(auto correction)")
            a.corrected_dates.add(m.at[i, "date"])
            continue
        if their_stale:
            explained[i] = True
            a.issue(check, m.at[i, "date"], "close", ours_c[i], other_c[i], "RESOLVED",
                    f"{name} repeats the previous day's close; ours has normal trading - ours kept")
            continue
        gap = abs(ours_c[i] / other_c[i] - 1)
        if close_rule_on and con is not None and prev_ours is not None and gap <= CFG["bad_tick_pct"] / 100:
            # tie rule (no third source): keep ours if the gap is minor, else keep the close nearer the
            # previous close (the smaller, less extreme move). Gaps above bad_tick_pct stay open.
            explained[i] = True
            if gap <= CFG["tie_minor_pct"] / 100 or abs(ours_c[i] - prev_ours) <= abs(other_c[i] - prev_ours):
                why = (f"gap {gap:.2%} <= {CFG['tie_minor_pct']:g}%" if gap <= CFG["tie_minor_pct"] / 100
                       else "ours is the smaller move from the previous close")
                a.issue(check, m.at[i, "date"], "close", ours_c[i], other_c[i], "RESOLVED",
                        f"tie rule: close differs from {name} ({why}) - ours kept")
            else:
                v = float(other_c[i])
                corrections.add_auto(con, a.ticker, m.at[i, "date"], "close", v, name,
                                     "auto: tie rule - our close was the bigger move from the previous close")
                if v > m.at[i, "high"]:
                    corrections.add_auto(con, a.ticker, m.at[i, "date"], "high", v, name, "auto: raised to the corrected close")
                if v < m.at[i, "low"]:
                    corrections.add_auto(con, a.ticker, m.at[i, "date"], "low", v, name, "auto: lowered to the corrected close")
                corrections.apply(con, a.ticker)
                a.corrected_dates.add(m.at[i, "date"])
                a.issue(check, m.at[i, "date"], "close", ours_c[i], v, "RESOLVED",
                        f"tie rule: close differs from {name} by {gap:.2%}; ours was the bigger move from the "
                        f"previous close - set to {name}'s close (auto correction)")
            continue
        a.issue(check, m.at[i, "date"], "close", ours_c[i], other_c[i], "FAIL",
                f"close differs from {name} by more than {CFG['compare_tolerance_pct']:g}%")
    n_explained = int(explained.sum())
    bad_close = bad_close & ~explained
    oth = {f: (m[f"{f}_o"] / scale).to_numpy() for f in ("open", "high", "low", "close")}
    ours_k = {f: (m[f] * k).to_numpy() for f in ("open", "high", "low", "close")}
    d_open = np.abs(ours_k["open"] / oth["open"] - 1)
    for j in np.where(d_open > tol)[0]:
        a.issue(check, m.at[j, "date"], "open", ours_k["open"][j], oth["open"][j], "INFO",
                "open differs - providers define it differently (info)")
    n_hl = 0
    for field in ("high", "low"):
        dd = np.abs(ours_k[field] / oth[field] - 1)
        for j in np.where(dd > tol)[0]:
            if explained.iloc[j]:
                continue
            a.disputes.append({"source": name, "date": m.at[j, "date"], "field": field,
                               "ours": float(m.at[j, field]), "theirs": float(oth[field][j] / k[j]),
                               "our_open": float(m.at[j, "open"]), "our_close": float(m.at[j, "close"]),
                               "their_open": float(oth["open"][j] / k[j]),
                               "their_close": float(oth["close"][j] / k[j]),
                               "storable": True})
            n_hl += 1
    vol_o = m["volume_o"] * (F if "splits" in basis else 1.0)
    v_bad = int(((m["volume"] / vol_o - 1).abs() > CFG["volume_tolerance_pct"] / 100).sum())

    in_range = px[(px["date"] >= other["date"].min()) & (px["date"] <= other["date"].max())]
    only_ours = sorted(set(in_range["date"]) - set(other["date"]))
    only_other = sorted(set(other["date"]) - set(px["date"]))
    for d in only_other:
        a.issue(check, d, "date", None, None, "FAIL", f"{name} has this trading day, we don't")
    for d in only_ours[:20]:
        a.issue(check, d, "date", None, None, "INFO", f"{name} is missing this day (their gap)")

    n_fail = int(bad_close.sum()) + len(only_other)
    n_open = int((d_open > tol).sum())
    a.add(check, _status(n_fail, 0, n_open), len(m), n_fail,
          f"{len(m)} days compared ({len(m) / len(px):.1%} of ours); basis: {basis}; "
          f"close mismatches {int(bad_close.sum())} (largest {d_close[~explained].max() * 100 if (~explained).any() else 0:.3f}%)"
          + (f", {n_explained} explained by a spin-off adjustment factor (info)" if n_explained else "") + "; "
          f"high/low differences {n_hl} (checked in high_low); open differences {n_open} (info); "
          f"volume >{CFG['volume_tolerance_pct']:g}% apart on {v_bad} days (info); days only in {name}: "
          f"{len(only_other)}")
    diff_pct = (ours_c / other_c - 1) * 100
    for i, f in factor_of.items():          # days that differ only by a spin-off factor: compare without it
        r = ours_c[i] / other_c[i]
        diff_pct[i] = (min(abs(r * f - 1), abs(r / f - 1))) * 100
    a.compare[name] = pd.DataFrame({"date": m["date"], "our_close": ours_c, f"{name}_close": other_c,
                                    "diff_pct": diff_pct,
                                    "our_volume": m["volume"], f"{name}_volume": vol_o})


# ------------------------------------------------------------------ Tier 2
_MARKET_DAYS = None
MARKET_MOVE = 0.08        # a stock moving more than this ...
MARKET_SHARE = 0.25       # ... together with at least this share of all stocks = market-wide day


def market_days(con):
    """{date: +1/-1} for days when a quarter of all stored stocks moved more than 8% the same way."""
    global _MARKET_DAYS
    if _MARKET_DAYS is None:
        rows = run(con, f"""WITH r AS (SELECT date, close / lag(close) OVER (PARTITION BY ticker ORDER BY date) - 1 AS ret
                                       FROM prices_daily)
                            SELECT date, count(*) AS n, sum(CASE WHEN ret > {MARKET_MOVE} THEN 1 ELSE 0 END) AS up,
                                   sum(CASE WHEN ret < -{MARKET_MOVE} THEN 1 ELSE 0 END) AS dn
                            FROM r WHERE ret IS NOT NULL GROUP BY date HAVING count(*) >= 50""")
        _MARKET_DAYS = {}
        for d, n, up, dn in rows:
            if up >= MARKET_SHARE * n:
                _MARKET_DAYS[d] = 1
            elif dn >= MARKET_SHARE * n:
                _MARKET_DAYS[d] = -1
    return _MARKET_DAYS


def check_big_moves(a, px, single_source, con=None):
    close = px.set_index("date")["close"]
    dates = list(close.index)
    ret = close.pct_change()
    lim, tol = CFG["big_move_pct"] / 100, CFG["compare_tolerance_pct"]
    agree = {}
    for name, side in a.compare.items():
        ok = side.set_index("date")["diff_pct"].abs() <= tol
        agree[name] = set(ok.index[ok])
    n_auto = n_review = 0
    for pos, d in enumerate(dates):
        r = ret.iloc[pos]
        if pos == 0 or np.isnan(r) or abs(r) <= lim:
            continue
        prev = dates[pos - 1]
        by = [n for n, s in agree.items() if d in s and prev in s]
        if by:
            a.issue("big_moves", d, "close", r * 100, None, "RESOLVED", f"real move - {by[0]} shows the same closes")
            n_auto += 1
            continue
        mkt = market_days(con).get(d) if con is not None else None
        if mkt is not None and mkt == (1 if r > 0 else -1):
            a.issue("big_moves", d, "close", r * 100, None, "RESOLVED",
                    "market-wide move - a quarter or more of all stocks moved over 8% the same way that day")
            n_auto += 1
            continue
        if single_source and pos + 1 < len(dates):
            undo = close.iloc[pos + 1] / close.iloc[pos - 1] - 1   # next day's close vs before the move
            if abs(undo) > 0.2 * abs(r):
                a.issue("big_moves", d, "close", r * 100, None, "RESOLVED",
                        "move persisted the next day (not a one-day spike)")
                n_auto += 1
                continue
        a.issue("big_moves", d, "close", r * 100, None, "REVIEW",
                "large one-day move not confirmed automatically - check news/earnings")
        n_review += 1
    a.add("big_moves", _status(0, n_review), len(dates) - 1, n_review,
          f"one-day moves above {CFG['big_move_pct']:g}%: {n_auto} confirmed automatically, {n_review} to review")


def _effect(px, i, field, alt):
    """(days, largest relative change) of the 52-week high/low or 6-month low if day i's value were `alt`."""
    s = px[field].to_numpy(float)
    n, affected, biggest = len(s), 0, 0.0
    looks = [252, 126] if field == "low" else [252]
    for look in looks:
        for D in range(i + 1, min(n, i + look) + 1):
            if D - look < 0:
                continue
            seg = s[D - look:D].copy()
            cur = seg.min() if field == "low" else seg.max()
            seg[i - (D - look)] = alt
            new = seg.min() if field == "low" else seg.max()
            if abs(cur - new) > 1e-9:
                affected += 1
                biggest = max(biggest, abs(new / cur - 1) if cur else 0.0)
    return affected, biggest


def _effect_days(px, i, field, alt):
    return _effect(px, i, field, alt)[0]


def check_high_low(a, px, con):
    p = CFG["bad_tick_pct"] / 100
    idx = {d: i for i, d in enumerate(px["date"])}
    n_auto = n_review = n_fixed = 0
    # the tie rule is for isolated disagreements only; many of them point to a systematic problem
    rule_on = sum(1 for x in a.disputes if _effect_days(px, idx[x["date"]], x["field"], x["theirs"])) \
        <= CFG["tie_max_items"]
    for x in a.disputes:
        i, f = idx[x["date"]], x["field"]
        affected = _effect_days(px, i, f, x["theirs"])
        if affected == 0:
            a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "RESOLVED",
                    f"differs from {x['source']} but changes no 52-week/6-month value")
            n_auto += 1
            continue

        def outlier(v, o, c):
            return v < min(o, c) * (1 - p) if f == "low" else v > max(o, c) * (1 + p)
        ours_bad = outlier(x["ours"], x["our_open"], x["our_close"])
        theirs_bad = outlier(x["theirs"], x["their_open"], x["their_close"])
        if theirs_bad and not ours_bad:
            a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "RESOLVED",
                    f"{x['source']} value is a bad tick (>{CFG['bad_tick_pct']:g}% outside its open/close); ours kept")
            n_auto += 1
        elif ours_bad and not theirs_bad and x["storable"]:
            corrections.add_auto(con, a.ticker, x["date"], f, x["theirs"], x["source"],
                                 f"auto: our {f} {x['ours']:.4f} was a bad tick")
            corrections.apply(con, a.ticker)
            a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "RESOLVED",
                    f"OUR value was a bad tick - corrected to {x['source']} value (auto correction)")
            n_fixed += 1
        elif rule_on:
            # tie rule (no third source): keep ours if the effect is minor, else keep the LESS extreme value
            _, biggest = _effect(px, i, f, x["theirs"])
            minor = CFG["tie_minor_pct"] / 100
            if biggest <= minor:
                a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "RESOLVED",
                        f"tie rule: {x['source']}'s value would move the 52-week/6-month {f} by at most "
                        f"{biggest:.2%} (<= {CFG['tie_minor_pct']:g}%) - ours kept")
                n_auto += 1
            else:
                o, c = x["our_open"], x["our_close"]
                keep = (min(max(x["ours"], x["theirs"]), min(o, c)) if f == "low"
                        else max(min(x["ours"], x["theirs"]), max(o, c)))
                if abs(keep - x["ours"]) < 1e-9:
                    a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "RESOLVED",
                            f"tie rule: sources disagree (effect up to {biggest:.2%}); ours is the less extreme "
                            f"value - kept")
                    n_auto += 1
                else:
                    corrections.add_auto(con, a.ticker, x["date"], f, float(keep), x["source"],
                                         f"auto: tie rule - our {f} {x['ours']:.4f} was the more extreme value")
                    corrections.apply(con, a.ticker)
                    a.issue("high_low", x["date"], f, x["ours"], keep, "RESOLVED",
                            f"tie rule: sources disagree (effect up to {biggest:.2%}); OUR value was the more "
                            f"extreme one - set to {keep:.4f} (auto correction)")
                    n_fixed += 1
        else:
            a.issue("high_low", x["date"], f, x["ours"], x["theirs"], "REVIEW",
                    f"differs from {x['source']} and changes {affected} day(s) of 52-week/6-month values"
                    f" (more than {CFG['tie_max_items']} disputed days - tie rule not used)")
            n_review += 1
    a.add("high_low", _status(0, n_review), len(a.disputes), n_review,
          f"{len(a.disputes)} high/low differences: {n_auto} resolved automatically, "
          f"{n_fixed} auto-corrected, {n_review} to review")


# ------------------------------------------------------------------ reviewed list
def load_reviewed():
    """{(ticker, date, check): note}; an entry with date_from/date_to covers every item of that
    check in the range (stored under date None as {(ticker, None, check): [(from, to, note)]})."""
    if not REVIEWED_FILE.exists():
        return {}
    with open(REVIEWED_FILE, "rb") as f:
        items = tomllib.load(f).get("reviewed", [])
    out = {}
    for i in items:
        checks = i["check"] if isinstance(i["check"], list) else [i["check"]]
        for c in checks:
            if "date" in i:
                out[(i["ticker"].upper(), dt.date.fromisoformat(str(i["date"])), c)] = i.get("note", "")
            else:
                out.setdefault((i["ticker"].upper(), None, c), []).append(
                    (dt.date.fromisoformat(str(i["date_from"])), dt.date.fromisoformat(str(i["date_to"])),
                     i.get("note", "")))
    return out


def _reviewed_note(reviewed, ticker, date, check):
    if (ticker, date, check) in reviewed:
        return reviewed[(ticker, date, check)] or "confirmed correct"
    for lo, hi, note in reviewed.get((ticker, None, check), []):
        if date is not None and lo <= date <= hi:
            return note or "confirmed correct"
    return None


def apply_reviewed(a, reviewed):
    for i in a.issues:
        note = _reviewed_note(reviewed, a.ticker, i["date"], i["check"])
        if note is not None and i["result"] in ("FAIL", "REVIEW"):
            i["result"] = "RESOLVED"
            i["note"] = f"REVIEWED: {note}"
            i["reviewed"] = True
    for r in a.results:
        if r["status"] not in ("REVIEW", "FAIL"):
            continue
        open_items = [i for i in a.issues if i["check"] == r["check"] and i["result"] in ("FAIL", "REVIEW")]
        reviewed_n = sum(1 for i in a.issues if i["check"] == r["check"] and i.get("reviewed"))
        if not open_items and reviewed_n:
            r["status"] = "PASS"
            r["detail"] += f" - {reviewed_n} item(s) reviewed and confirmed"


# ------------------------------------------------------------------ backtest window
WINDOW_DAYS = 370     # 52-week lookback before joining, and up to a 1-year hold after leaving
STRICT_CHECKS = {"raw_vs_db", "data_present"}     # data integrity: always counts, any date


def resolve_repeated(a, joined):
    """Repeated bars fixed by an auto correction are resolved; ones before the stock joined the index
    (when-issued trading, no volume) are information only."""
    changed = False
    for i in a.issues:
        if i["check"] != "repeated_days" or i["result"] not in ("FAIL", "REVIEW"):
            continue
        if i["date"] in a.corrected_dates:
            i["result"], i["note"] = "RESOLVED", "replaced by the second source's bar (auto correction) - " + i["note"]
            changed = True
        elif joined is not None and i["date"] < joined and i["field"] == "bar":
            i["result"], i["note"] = "INFO", "before the stock joined the index (when-issued trading) - " + i["note"]
            changed = True
    if changed:
        for r in a.results:
            if r["check"] == "repeated_days":
                left = [i for i in a.issues if i["check"] == "repeated_days"]
                r["n_failed"] = sum(1 for i in left if i["result"] in ("FAIL", "REVIEW"))
                fails = sum(1 for i in left if i["result"] == "FAIL")
                revs = sum(1 for i in left if i["result"] == "REVIEW")
                r["status"] = _status(fails, revs, 1)


def first_member_day(con, ticker):
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'index_membership'")[0][0]
    if not has:
        return None
    return run(con, "SELECT min(start_date) FROM index_membership WHERE ticker = ?", [ticker])[0][0]


def last_member_day(con, ticker):
    """Last day in either index; None while it is still a member."""
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'index_membership'")[0][0]
    if not has:
        return None
    r = run(con, """SELECT bool_or(end_date IS NULL), max(end_date) FROM index_membership WHERE ticker = ?""", [ticker])[0]
    return None if (r[0] or r[1] is None) else r[1]


def backtest_window(con, ticker):
    """(first, last) dates whose prices can affect a backtest: 1 year before the stock first joined
    either index to 1 year after it last left. None if the stock has no membership record."""
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'index_membership'")[0][0]
    if not has:
        return None
    r = run(con, """SELECT min(start_date), max(coalesce(end_date, DATE '2999-12-31')), count(*)
                    FROM index_membership WHERE ticker = ?""", [ticker])[0]
    if not r[2]:
        return None
    return r[0] - dt.timedelta(days=WINDOW_DAYS), r[1] + dt.timedelta(days=WINDOW_DAYS)


def apply_backtest_window(a, con):
    """Items dated outside the window can't affect any backtest: they become information only."""
    w = backtest_window(con, a.ticker)
    if w is None:
        return
    lo, hi = w
    had = {}
    for i in a.issues:
        if i["check"] in STRICT_CHECKS or i["result"] not in ("FAIL", "REVIEW"):
            continue
        had[i["check"]] = True
        d = i["date"]
        if d is not None and not pd.isna(d) and (d < lo or d > hi):
            i["result"] = "INFO"
            i["note"] = f"outside the backtest window ({lo} to {min(hi, dt.date.today())}) - {i['note']}"
    for r in a.results:
        if r["check"] not in had or r["status"] not in ("FAIL", "REVIEW"):
            continue
        still = sum(1 for i in a.issues if i["check"] == r["check"] and i["result"] in ("FAIL", "REVIEW"))
        moved = r["n_failed"] - still
        r["n_failed"] = still
        if still == 0:
            r["status"] = "INFO"
        if moved > 0:
            r["detail"] += f" - {moved} item(s) outside the backtest window (info only)"
    a.window = (lo, hi)


# ------------------------------------------------------------------ driver
def audit_ticker(con, ticker, online=True, sources=None):
    con.execute(AUDIT_SCHEMA).fetchall()
    corrections.sync_manual(con)
    corrections.apply(con, ticker)
    src_row = run(con, "SELECT any_value(source) FROM prices_daily WHERE ticker = ?", [ticker])
    a = Audit(ticker, src_row[0][0] if src_row else None)
    px, actions = load_prices(con, ticker), load_actions(con, ticker)
    if px.empty:
        a.add("data_present", "FAIL", 0, 1, "no prices stored for this ticker")
        a.stock_status, a.reason = "NO DATA", "no source has prices for this ticker"
        return a

    latest = run(con, "SELECT max(date) FROM prices_daily")[0][0]
    check_data_present(a, px, latest)
    check_raw_vs_db(a, px, con)
    check_ohlc(a, px)
    check_trading_days(a, px, first_member_day(con, ticker), last_member_day(con, ticker))
    check_repeated(a, px)
    check_splits(a, px, actions)
    check_adj_close(a, px, actions)
    if online:
        for name in (sources or CFG["second_sources"]):
            if name in SOURCES and name != a.source:
                compare_source(a, px, actions, name, SOURCES[name], con)
    resolve_repeated(a, first_member_day(con, ticker))
    if a.corrected_dates:                       # later checks use the corrected bars
        px = load_prices(con, ticker)
    full_check = [r for r in a.results if r["check"].startswith("compare_") and r["status"] in ("PASS", "INFO")
                  and r["n_checked"] >= 0.9 * len(px)]
    single = not full_check
    check_big_moves(a, px, single, con)
    check_high_low(a, px, con)
    apply_backtest_window(a, con)
    apply_reviewed(a, load_reviewed())

    worst = max((ORDER[r["status"]] for r in a.results), default=0)
    if worst == ORDER["FAIL"]:
        a.stock_status = "BLOCKED"
        a.reason = "failed: " + ", ".join(r["check"] for r in a.results if r["status"] == "FAIL")
    elif worst == ORDER["REVIEW"]:
        a.stock_status = "NEEDS REVIEW"
        a.reason = "to review: " + ", ".join(f"{r['check']} ({r['n_failed']})" for r in a.results
                                             if r["status"] == "REVIEW")
    elif single:
        a.stock_status = "VERIFIED - SINGLE SOURCE"
        a.reason = f"passed, but only one full-history price source ({a.source})"
    else:
        a.stock_status = "VERIFIED"
        a.reason = "all checks passed against " + ", ".join(r["check"][8:] for r in full_check)
    a.summary = {"rows": len(px), "first": px["date"].min(), "last": px["date"].max(),
                 "splits": int((actions["action"] == "split").sum()),
                 "dividends": int((actions["action"] == "dividend").sum())}
    return a


def save_to_db(con, a, run_id, audit_type="full"):
    con.execute(AUDIT_SCHEMA).fetchall()
    for r in a.results:
        run(con, "INSERT INTO audit_runs VALUES (?, current_timestamp, ?, 'prices', ?, ?, ?, ?, ?)",
            [run_id, a.ticker, r["check"], r["status"], r["n_checked"], r["n_failed"], r["detail"]])
    for i in a.issues:
        run(con, "INSERT INTO audit_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, a.ticker, i["check"], i["date"], i["field"], i["our_value"], i["other_value"],
             i["diff_pct"], f"[{i['result']}] {i['note']}"])
    run(con, "INSERT OR REPLACE INTO stock_status VALUES (?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
        [a.ticker, a.stock_status, a.reason, a.source, a.summary.get("first"), a.summary.get("last"),
         audit_type, run_id])


def text_report(a, run_id):
    s = a.summary
    lines = [f"PRICE AUDIT  {a.ticker}   STATUS: {a.stock_status}",
             f"  {a.reason}",
             f"run {run_id}  at {dt.datetime.now():%Y-%m-%d %H:%M}   price source: {a.source}",
             f"data: {s.get('rows', 0):,} days, {s.get('first')} to {s.get('last')}, "
             f"{s.get('splits', 0)} split(s), {s.get('dividends', 0)} dividend(s)", ""]
    for r in a.results:
        lines.append(f"  [{r['status']:<6}] {r['check']:<18} checked {r['n_checked']:>6,}  "
                     f"open items {r['n_failed']:>4}   {r['detail']}")
    open_items = [i for i in a.issues if i["result"] in ("FAIL", "REVIEW")]
    if open_items:
        lines += ["", "Items that need attention (up to 5 per check; full list in the Excel file):"]
        shown = {}
        for i in open_items:
            shown[i["check"]] = shown.get(i["check"], 0) + 1
            if shown[i["check"]] > 5:
                continue
            vals = ""
            if i["our_value"] is not None:
                vals = f" ours {i['our_value']:.4f}"
            if i["other_value"] is not None:
                vals += f" other {i['other_value']:.4f}"
            lines.append(f"  [{i['result']}] {i['date']}  {i['check']:<16} {i['field']:<9}{vals}  {i['note']}")
    resolved = sum(1 for i in a.issues if i["result"] == "RESOLVED")
    info = sum(1 for i in a.issues if i["result"] == "INFO")
    lines.append(f"\n  {resolved} item(s) resolved automatically, {info} information item(s) - see Excel 'issues'")
    return "\n".join(lines)


def excel_report(a, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame([{"ticker": a.ticker, "status": a.stock_status, "reason": a.reason}]).to_excel(
            xw, sheet_name="status", index=False)
        pd.DataFrame(a.results).to_excel(xw, sheet_name="checks", index=False)
        iss = pd.DataFrame(a.issues or [{"note": "no issues"}])
        if "reviewed" in iss.columns:
            iss = iss.drop(columns="reviewed")
        iss.to_excel(xw, sheet_name="issues", index=False)
        for name, side in a.compare.items():
            side.sort_values("date", ascending=False).to_excel(xw, sheet_name=f"vs_{name}", index=False)
        for ws in xw.sheets.values():
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 14
                if col[0].value == "date":
                    for c in col[1:]:
                        c.number_format = "yyyy-mm-dd"
                if col[0].value in ("detail", "note", "reason"):
                    ws.column_dimensions[col[0].column_letter].width = 100
