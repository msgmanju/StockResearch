"""
Daily support and resistance, using the same method as your stock screener.

  Swing low  = a day whose LOW  is the lowest  within +/- pivot_window days.
  Swing high = a day whose HIGH is the highest within +/- pivot_window days.
  Swing points within cluster_pct of each other are merged into one level;
  "touches" = how many swing points form the level (more = stronger).
  Support    = nearest level below the reference close (by more than band_pct),
  Resistance = nearest level above it. Old resistance can become support and
  vice versa, so highs and lows are pooled.
  Fallbacks: support = 6-month low, resistance = 52-week high (touches = 0).

Point in time: the value stored for day D uses only the 252 trading days up to
and including D-1, with D-1's close as the reference price. That is exactly what
your screener would have shown if run after the close of D-1.
A swing point needs pivot_window days after it, so it only counts once confirmed.
"""
import numpy as np
import pandas as pd

from src.common import SETTINGS

CFG = {"pivot_window": 5, "cluster_pct": 0.02, "lookback": 252, "band_pct": 0.005, "fallback_6m": 126}
CFG.update(SETTINGS.get("support_resistance", {}))
CALC_VERSION = "v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS swing_points (
    ticker          VARCHAR NOT NULL,
    date            DATE    NOT NULL,     -- the day of the swing high/low
    kind            VARCHAR NOT NULL,     -- 'high' or 'low'
    price           DOUBLE,
    confirmed_date  DATE,                 -- first day it is known (pivot_window days later)
    PRIMARY KEY (ticker, date, kind)
);
CREATE TABLE IF NOT EXISTS sr_daily (
    ticker              VARCHAR NOT NULL,
    date                DATE    NOT NULL,
    ref_close           DOUBLE,           -- previous day's close used as the reference
    support             DOUBLE,
    support_touches     INTEGER,          -- 0 = fallback (6-month low)
    resistance          DOUBLE,
    resistance_touches  INTEGER,          -- 0 = fallback (52-week high)
    calc_version        VARCHAR,
    calculated_at       TIMESTAMP,
    PRIMARY KEY (ticker, date)
);
"""


def find_swings(low: np.ndarray, high: np.ndarray, w: int):
    """Indexes of swing lows and swing highs over the full history."""
    n = len(low)
    lows, highs = [], []
    for i in range(w, n - w):
        if low[i] == low[i - w:i + w + 1].min():
            lows.append(i)
        if high[i] == high[i - w:i + w + 1].max():
            highs.append(i)
    return np.array(lows, dtype=int), np.array(highs, dtype=int)


def cluster(levels):
    """Merge nearby levels (same rule as the screener). Returns [(level, touches)]."""
    if not len(levels):
        return []
    levels = sorted(levels)
    out, cur = [], [levels[0]]
    for lv in levels[1:]:
        if lv <= np.mean(cur) * (1 + CFG["cluster_pct"]):
            cur.append(lv)
        else:
            out.append(cur)
            cur = [lv]
    out.append(cur)
    return [(float(np.mean(c)), len(c)) for c in out]


def levels_at(e, low, high, close, sw_idx, sw_price):
    """Support/resistance from the window of bars ending at index e (inclusive)."""
    w, look = CFG["pivot_window"], CFG["lookback"]
    s = e - look + 1
    # swing points whose whole +/- w neighbourhood lies inside the window
    lo_i, hi_i = np.searchsorted(sw_idx, s + w), np.searchsorted(sw_idx, e - w, side="right")
    lv = cluster(sw_price[lo_i:hi_i])
    price = close[e]
    below = [(l, n) for l, n in lv if l < price * (1 - CFG["band_pct"])]
    above = [(l, n) for l, n in lv if l > price * (1 + CFG["band_pct"])]
    if below:
        sup, sup_n = max(below, key=lambda x: x[0])
    else:
        sup, sup_n = float(low[max(s, e - CFG["fallback_6m"] + 1):e + 1].min()), 0
    if above:
        res, res_n = min(above, key=lambda x: x[0])
    else:
        res, res_n = float(high[s:e + 1].max()), 0
    return price, sup, sup_n, res, res_n


def calculate(con, ticker):
    """Rebuild swing points and daily support/resistance for one ticker."""
    con.execute(SCHEMA).fetchall()
    px = con.execute("SELECT date, low, high, close FROM prices_daily WHERE ticker = ? ORDER BY date",
                     [ticker]).df()
    px["date"] = pd.to_datetime(px["date"]).dt.date
    low, high, close = (px[c].to_numpy(float) for c in ("low", "high", "close"))
    dates = px["date"].tolist()
    w, look = CFG["pivot_window"], CFG["lookback"]

    lows, highs = find_swings(low, high, w)
    sw = pd.DataFrame({"i": np.r_[lows, highs], "kind": ["low"] * len(lows) + ["high"] * len(highs),
                       "price": np.r_[low[lows], high[highs]]}).sort_values(["i", "kind"]).reset_index(drop=True)
    sw_idx, sw_price = sw["i"].to_numpy(), sw["price"].to_numpy()

    rows = []
    for d in range(look, len(px)):            # value for day d uses bars d-look .. d-1
        price, sup, sup_n, res, res_n = levels_at(d - 1, low, high, close, sw_idx, sw_price)
        rows.append((ticker, dates[d], price, sup, sup_n, res, res_n))
    sr = pd.DataFrame(rows, columns=["ticker", "date", "ref_close", "support", "support_touches",
                                     "resistance", "resistance_touches"])
    swings = pd.DataFrame({
        "ticker": ticker, "date": [dates[i] for i in sw["i"]], "kind": sw["kind"], "price": sw["price"],
        "confirmed_date": [dates[i + w] if i + w < len(dates) else None for i in sw["i"]]})

    con.begin()
    try:
        con.execute("DELETE FROM swing_points WHERE ticker = ?", [ticker]).fetchall()
        con.execute("DELETE FROM sr_daily WHERE ticker = ?", [ticker]).fetchall()
        con.register("sw_new", swings)
        con.execute("INSERT INTO swing_points SELECT ticker, date, kind, price, confirmed_date FROM sw_new").fetchall()
        con.unregister("sw_new")
        con.register("sr_new", sr)
        con.execute(f"""INSERT INTO sr_daily SELECT ticker, date, ref_close, support, support_touches,
                        resistance, resistance_touches, '{CALC_VERSION}', current_timestamp FROM sr_new""").fetchall()
        con.unregister("sr_new")
        con.commit()
    except Exception:
        con.rollback()
        raise
    return len(swings), len(sr)


def next_session(con, ticker):
    """Support/resistance for the next trading day (window ends at the last stored day)."""
    px = con.execute("SELECT low, high, close FROM prices_daily WHERE ticker = ? ORDER BY date", [ticker]).df()
    if len(px) < CFG["lookback"]:
        return None
    low, high, close = (px[c].to_numpy(float) for c in ("low", "high", "close"))
    lows, highs = find_swings(low, high, CFG["pivot_window"])
    order = np.argsort(np.r_[lows, highs], kind="stable")
    idx, price = np.r_[lows, highs][order], np.r_[low[lows], high[highs]][order]
    return levels_at(len(px) - 1, low, high, close, idx, price)
