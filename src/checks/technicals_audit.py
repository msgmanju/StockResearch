"""
Check the stored 52-week / 6-month values with a second, independent calculation
(pandas rolling windows instead of SQL). Both must agree on every day.
"""
import numpy as np
import pandas as pd

from src.calculate.technicals import LOOKBACK_52W, LOOKBACK_6M


def independent(px: pd.DataFrame) -> pd.DataFrame:
    p = px.sort_values("date").reset_index(drop=True)
    prev_low, prev_high, prev_close = p["low"].shift(1), p["high"].shift(1), p["close"].shift(1)
    return pd.DataFrame({
        "date": p["date"],
        "low_52w": prev_low.rolling(LOOKBACK_52W, min_periods=LOOKBACK_52W).min(),
        "high_52w": prev_high.rolling(LOOKBACK_52W, min_periods=LOOKBACK_52W).max(),
        "avg_close_52w": prev_close.rolling(LOOKBACK_52W, min_periods=LOOKBACK_52W).mean(),
        "low_6m": prev_low.rolling(LOOKBACK_6M, min_periods=LOOKBACK_6M).min(),
    })


def check(con, ticker):
    """Returns (list of (check, status, detail), side-by-side DataFrame)."""
    px = con.execute("SELECT date, low, high, close FROM prices_daily WHERE ticker = ? ORDER BY date",
                     [ticker]).df()
    td = con.execute("""SELECT date, low_52w, high_52w, avg_close_52w, low_6m
                        FROM technicals_daily WHERE ticker = ? ORDER BY date""", [ticker]).df()
    ind = independent(px)
    m = td.merge(ind, on="date", how="outer", suffixes=("", "_check"), indicator=True)
    results = []

    rows_ok = (m["_merge"] == "both").all() and len(td) == len(px)
    results.append(("rows", "PASS" if rows_ok else "FAIL",
                    f"{len(td):,} technicals rows for {len(px):,} price days"))

    worst, bad = 0.0, 0
    for c in ("low_52w", "high_52w", "avg_close_52w", "low_6m"):
        a, b = m[c].astype(float), m[f"{c}_check"].astype(float)
        both_blank = a.isna() & b.isna()
        diff = (a / b - 1).abs().where(~both_blank, 0).fillna(1.0)   # one blank, other not = mismatch
        bad += int((diff > 1e-9).sum())
        worst = max(worst, float(diff.max()))
    results.append(("two_methods_agree", "PASS" if bad == 0 else "FAIL",
                    f"SQL vs independent pandas calculation: {bad} differences, largest {worst:.2e}"))

    full = td.dropna(subset=["low_52w"])
    expected_full = max(len(px) - LOOKBACK_52W, 0)
    results.append(("full_windows", "PASS" if len(full) == expected_full else "FAIL",
                    f"{len(full):,} days with a complete 52-week window (expected {expected_full:,}); "
                    f"first complete day {pd.Timestamp(full['date'].min()).date() if len(full) else '-'}"))

    order_bad = int(((full["low_52w"] > full["avg_close_52w"] + 1e-9) |
                     (full["avg_close_52w"] > full["high_52w"] + 1e-9) |
                     (full["low_52w"] > full["low_6m"] + 1e-9)).sum())
    results.append(("value_order", "PASS" if order_bad == 0 else "FAIL",
                    f"low_52w <= avg_close_52w <= high_52w and low_52w <= low_6m on every day "
                    f"({order_bad} exceptions)"))
    return results, m
