"""
Step 9 - strategy engine: screener and trade check. Rules come from config/strategies.toml.
See docs/STRATEGY.md.

Everything uses only what was known on the day:
  * technicals of day D use the 252 sessions BEFORE D (technicals_daily)
  * fundamentals: the latest SEC report filed BEFORE D (fund_snapshots, used from the day after filing)
  * index membership on D (index_membership)
Prices are split-adjusted, not dividend-adjusted; dividends are reported separately.
"""
import datetime as dt
import tomllib

import numpy as np
import pandas as pd

from src.common import FUND_DB, ROOT, TECH_DB, connect, run
from src.calculate import fundamentals as fu
from src.membership import members_on

STRATEGY_FILE = ROOT / "config" / "strategies.toml"


# Strategy keys before Nov 22, 2026 -> today's readable keys (old commands and saved runs keep working)
OLD_KEYS = {"mr_v3": "dip_buyer", "sr_v1": "support_buyer", "mr_top30": "bigcap_dip_buyer"}


def strategy_key(key):
    return OLD_KEYS.get(key, key)


def load_strategy(key="dip_buyer"):
    key = strategy_key(key)
    with open(STRATEGY_FILE, "rb") as f:
        all_ = tomllib.load(f)
    if key not in all_:
        raise ValueError(f"unknown strategy '{key}' - known: {', '.join(all_)}")
    s = dict(all_[key])
    s["key"] = key
    return s


def _d(x):
    return x if isinstance(x, dt.date) and not isinstance(x, dt.datetime) else pd.Timestamp(x).date()


# ------------------------------------------------------------------ market regime
def regime(con, day, s, method=None):
    """QQQ market regime on `day` (see regime_method in config/strategies.toml):
      ytd   - drawdown of the close from the highest high so far in the calendar year (the day included)
      52w   - drawdown of the close from the highest high of the last 252 sessions (the day included,
              as the thinkorswim study Highest(high, 252))
      years - bear if the year is in bear_years (the backtest's list; known only afterwards)
    Bear when the drawdown >= bear_drawdown_pct."""
    method = method or s.get("regime_method", "ytd")
    if method == "52w":
        rows = run(con, """SELECT date, high, close FROM prices_daily WHERE ticker = ? AND date <= ?
                           ORDER BY date DESC LIMIT 252""", [s["regime_ticker"], day])
    else:
        rows = run(con, """SELECT date, high, close FROM prices_daily WHERE ticker = ? AND date <= ?
                           AND date >= ? ORDER BY date DESC""", [s["regime_ticker"], day, dt.date(day.year, 1, 1)])
    if not rows or rows[0][0] != day:
        return None
    peak = max(r[1] for r in rows)
    dd = (peak - rows[0][2]) / peak
    bear = day.year in s.get("bear_years", []) if method == "years" else dd * 100 >= s["bear_drawdown_pct"]
    return {"date": day, "method": method, "qqq_close": rows[0][2], "qqq_peak": peak, "qqq_drawdown": dd,
            "bear": bear, "stop_pct": s["stop_bear_pct"] if bear else s["stop_normal_pct"]}


def session_on_or_before(con, day, ticker=None):
    ticker = ticker or "QQQ"
    r = run(con, "SELECT max(date) FROM prices_daily WHERE ticker = ? AND date <= ?", [ticker, day])
    return r[0][0]


# ------------------------------------------------------------------ fundamentals as known
_SPLITS = {}


def splits_of(con, ticker):
    if ticker not in _SPLITS:
        _SPLITS[ticker] = fu.real_splits(run(con, """SELECT date, value FROM corporate_actions
                                                    WHERE ticker = ? AND action = 'split'""", [ticker]))
    return _SPLITS[ticker]


def fundamentals_known(fcon, tickers, day):
    """Latest snapshot per ticker filed BEFORE `day` (a report is usable from the day after filing)."""
    if not tickers:
        return pd.DataFrame()
    q = ",".join("?" * len(tickers))
    df = fcon.execute(f"""
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY filed DESC) AS rn
            FROM fund_snapshots WHERE ticker IN ({q}) AND filed < ?) WHERE rn = 1""",
                      list(tickers) + [day]).df()
    return df.drop(columns=["rn"])


# ------------------------------------------------------------------ screener
def screen(day, strategy="dip_buyer", tol_pct=5.0, universe=None, tcon=None, fcon=None, regime_method=None):
    """All index members on `day` with the strategy's values; `in_range` = close within +/- tol_pct
    of the buy price. Returns (info dict, DataFrame sorted by % from buy price)."""
    s = load_strategy(strategy)
    own = tcon is None
    tcon = tcon or connect(TECH_DB, read_only=True)
    fcon = fcon or connect(FUND_DB, read_only=True)
    asked = _d(day)
    day = session_on_or_before(tcon, asked)
    uni = (universe or s["universe"]).upper()
    members = members_on(tcon, uni, day)
    reg = regime(tcon, day, s, regime_method)
    info = {"strategy": s["name"], "asked_date": asked, "date": day, "universe": uni,
            "members": len(members), "tolerance_pct": tol_pct, "regime": reg}
    if not members:
        return info, pd.DataFrame()
    q = ",".join("?" * len(members))
    df = tcon.execute(f"""
        SELECT p.ticker, p.open, p.high, p.low, p.close, t.low_52w, t.high_52w, t.avg_close_52w, t.low_6m,
               s.support, s.support_touches, s.resistance, s.resistance_touches
        FROM prices_daily p
        LEFT JOIN technicals_daily t ON t.ticker = p.ticker AND t.date = p.date
        LEFT JOIN sr_daily s ON s.ticker = p.ticker AND s.date = p.date
        WHERE p.date = ? AND p.ticker IN ({q})""", [day] + members).df()
    missing = sorted(set(members) - set(df["ticker"]))
    info["no_price_that_day"] = missing
    df = derive(df, s, reg, tol_pct, tcon, fcon, day)
    df = add_peak_after(tcon, df, day, s.get("hold_days", 365))
    if own:
        tcon.close()
        fcon.close()
    return info, df


def derive(df, s, reg, tol_pct, tcon, fcon, day):
    """Strategy columns from the day's open/high/low/close and the 52-week values known before the day
    (shared by the screener and the live screen, src/strategy/live.py)."""
    df["hl_ratio"] = df["high_52w"] / df["low_52w"]
    df["passes_hl_filter"] = df["hl_ratio"] <= s["hl_max"]
    if s.get("kind") == "support_resistance":     # strategy 2: buy at support, target = resistance
        df["buy_price"] = df["support"]
        df["upside_pct"] = (df["resistance"] / df["support"] - 1) * 100
        from src.strategy.backtest import near_52w_pct
        df["near_52w_pct"] = near_52w_pct(df["support"], df["low_52w"], df["avg_close_52w"])
        df["near_which"] = np.where((df["support"] / df["low_52w"] - 1).abs()
                                    <= (df["support"] / df["avg_close_52w"] - 1).abs(), "52w low", "52w avg")
        if s.get("rank_by", "touches") == "near_52w":
            df["discount_score"] = df["near_52w_pct"] - df["support_touches"].fillna(0) / 1000
        else:
            df["discount_score"] = -df["support_touches"].fillna(0) * 1000 - df["upside_pct"].fillna(0) / 100
        ok = df["passes_hl_filter"] & df["buy_price"].notna()
        if s.get("rank_by") == "near_52w" and s.get("near_pct"):
            ok &= df["near_52w_pct"] <= s["near_pct"]
        if s.get("sell", "resistance") == "resistance":
            ok &= df["upside_pct"] >= s.get("min_upside_pct", 0)
    else:
        df["buy_price"] = s["buy_low_weight"] * df["low_52w"] + s["buy_avg_weight"] * df["avg_close_52w"]
        df["discount_score"] = df["low_52w"] / df["buy_price"]
        ok = df["passes_hl_filter"] & df["discount_score"].notna()
    if s.get("top_n_mcap"):                       # strategy 3: only the N largest companies (point in time)
        from src.calculate import market_cap_history as mh
        x = df[["ticker", "close"]].assign(date=day, member=True)
        x = mh.add_mcap_rank(mh.add_market_cap(x, fcon, tcon), fcon)
        df["mcap_rank"] = df["ticker"].map(dict(zip(x["ticker"], x["mcap_rank"])))
        df["mcap_point_in_time"] = df["ticker"].map(dict(zip(x["ticker"], x["market_cap"])))
        ok &= df["mcap_rank"] <= s["top_n_mcap"]
    df["pct_from_buy"] = df["close"] / df["buy_price"] - 1
    df["low_touched_buy"] = df["low"] <= df["buy_price"]
    df.loc[ok, "discount_rank"] = df.loc[ok, "discount_score"].rank(method="first")
    df["top_n"] = df["discount_rank"] <= s["max_positions"]
    df["in_range"] = df["pct_from_buy"].abs() * 100 <= tol_pct
    if s.get("top_n_mcap"):
        df["in_range"] &= df["mcap_rank"] <= s["top_n_mcap"]
    df["note"] = ""
    df.loc[df["buy_price"].isna(), "note"] = "less than 252 sessions of history"
    # levels if bought at today's close
    if reg:
        df["stop_if_bought"] = df["close"] * (1 - reg["stop_pct"] / 100)
    df["take_profit_if_bought"] = df["close"] * (1 + s["take_profit_pct"] / 100)
    # fundamentals as known on the day
    f = fundamentals_known(fcon, list(df["ticker"]), day)
    if len(f):
        keep = ["ticker", "filed", "period_end", "revenue_ttm", "eps_diluted_ttm", "revenue_growth_q_yoy",
                "revenue_growth_ttm_yoy", "op_margin_ttm", "debt_to_equity", "debt_note", "equity"]
        f = f[[c for c in keep if c in f.columns]].rename(columns={"filed": "fund_filed"})
        df = df.merge(f, on="ticker", how="left")
        df["pe_ttm"] = [fu.pe_ratio(c, e, pd.Timestamp(fd).date(), splits_of(tcon, t))
                        if pd.notna(e) and pd.notna(fd) else None
                        for t, c, e, fd in zip(df["ticker"], df["close"], df["eps_diluted_ttm"], df["fund_filed"])]
        df = add_peg(fcon, tcon, df, day)
    # market cap (display/sorting only): Yahoo's share count x the day's close - src/collect/market_cap.py
    mc = market_caps(tcon, list(df["ticker"]))
    df["market_cap"] = [c * mc[t][0] * fu.split_factor_after(mc[t][1], splits_of(tcon, t))
                        if t in mc and pd.notna(c) else None for t, c in zip(df["ticker"], df["close"])]
    df["market_cap"] = df["market_cap"].astype(float)
    df["market_cap_asof"] = df["ticker"].map(lambda t: mc[t][1] if t in mc else None)
    return df.sort_values("pct_from_buy", na_position="last").reset_index(drop=True)


def add_peg(fcon, tcon, df, day):
    """PEG = P/E / EPS growth in % (display/sorting only - no strategy uses it). EPS growth compares the TTM EPS of
    the latest report filed before `day` with the TTM EPS of the period ending 1 (or 3) years earlier, as known
    before `day`; both are divided by the stock splits after their filing, so they are on the same share basis.
      eps_growth_1y  = EPS now / EPS a year earlier - 1
      eps_growth_3y  = (EPS now / EPS three years earlier) ^ (1/3) - 1   (per year)
      peg_1y, peg_3y = P/E / (growth x 100); blank when P/E or growth is not positive (losses, falling EPS)."""
    cols = ["eps_growth_1y", "eps_growth_3y", "peg_1y", "peg_3y"]
    df = df.drop(columns=[c for c in cols if c in df.columns])
    tickers = list(df.loc[df["eps_diluted_ttm"].notna(), "ticker"])
    if not tickers:
        return df.assign(**{c: np.nan for c in cols})
    q = ",".join("?" * len(tickers))
    h = fcon.execute(f"""SELECT ticker, filed, period_end, eps_diluted_ttm FROM fund_snapshots
                         WHERE ticker IN ({q}) AND filed < ? AND filed >= ? AND eps_diluted_ttm IS NOT NULL""",
                     tickers + [day, day - dt.timedelta(days=4 * 366 + 200)]).df()
    h["period_end"] = pd.to_datetime(h["period_end"])
    h["eps_b"] = [e / fu.split_factor_after(pd.Timestamp(f).date(), splits_of(tcon, t))
                  for t, f, e in zip(h["ticker"], h["filed"], h["eps_diluted_ttm"])]
    now = df.loc[df["eps_diluted_ttm"].notna(), ["ticker", "eps_diluted_ttm", "fund_filed", "period_end"]].copy()
    now["e_now"] = [e / fu.split_factor_after(pd.Timestamp(f).date(), splits_of(tcon, t))
                    for t, f, e in zip(now["ticker"], now["fund_filed"], now["eps_diluted_ttm"])]
    now["p_end"] = pd.to_datetime(now["period_end"])
    pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
    for yrs in (1, 3):
        m = now[["ticker", "e_now", "p_end"]].merge(h[["ticker", "filed", "period_end", "eps_b"]], on="ticker")
        m = m[(m["period_end"] - (m["p_end"] - pd.DateOffset(years=yrs))).abs() <= pd.Timedelta(days=45)]
        m = m.sort_values(["ticker", "filed"]).drop_duplicates("ticker", keep="last")   # latest version known
        ok = m["eps_b"] > 0
        both = ok & (m["e_now"] > 0)
        g = pd.Series(np.nan, index=m.index)
        g[both] = (m.loc[both, "e_now"] / m.loc[both, "eps_b"]) ** (1 / yrs) - 1
        g[ok & ~both] = m.loc[ok & ~both, "e_now"] / m.loc[ok & ~both, "eps_b"] - 1   # turned into a loss
        growth = df["ticker"].map(dict(zip(m["ticker"], g))).astype(float)
        df[f"eps_growth_{yrs}y"] = growth
        df[f"peg_{yrs}y"] = np.where((pe > 0) & (growth > 0), pe / (growth * 100), np.nan)
    return df


def add_peak_after(tcon, df, day, days=365):
    """HINDSIGHT columns (display only - never used by a strategy, the ranking or the backtest): the highest
    price in the year AFTER `day` (sessions after the day up to day + `days` calendar days), its date, and how
    far it is above the buy price. A window that runs past the last stored day is the peak so far."""
    cols = ["peak_after", "peak_after_date", "peak_after_pct", "peak_after_sessions", "peak_after_complete"]
    if df.empty:
        return df.assign(**{c: None for c in cols})
    end = day + dt.timedelta(days=days)
    last = run(tcon, "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]
    q = ",".join("?" * len(df))
    p = tcon.execute(f"""SELECT ticker, max(high) AS peak_after, arg_max(date, high) AS peak_after_date,
                                count(*) AS peak_after_sessions
                         FROM prices_daily WHERE ticker IN ({q}) AND date > ? AND date <= ?
                         GROUP BY ticker""", list(df["ticker"]) + [day, end]).df()
    df = df.drop(columns=[c for c in cols if c in df.columns]).merge(p, on="ticker", how="left")
    df["peak_after_pct"] = df["peak_after"] / df["buy_price"] - 1
    df["peak_after_complete"] = bool(last and last >= end)
    return df


def market_caps(tcon, tickers):
    """{ticker: (shares on today's basis, date downloaded)} from table market_cap ({} if not built yet)."""
    if not tickers:
        return {}
    try:
        q = ",".join("?" * len(tickers))
        rows = run(tcon, f"SELECT ticker, shares, fetched_on FROM market_cap WHERE ticker IN ({q})", list(tickers))
    except Exception:                 # table not built yet (scripts/load_market_cap.py / daily update)
        return {}
    return {t: (sh, d) for t, sh, d in rows if sh}


# ------------------------------------------------------------------ trade check
def trade_check(ticker, buy_date, strategy="dip_buyer", take_profit_pct=None, stop_pct=None, hold_days=None,
                tcon=None, regime_method=None):
    """Buy at the close of buy_date; exits (first one wins, checked from the next session):
      stop     low <= stop price  -> sold at the stop price (or at the open if it opened below)
      target   high >= take-profit -> sold at the target (or at the open if it opened above)
      both on one day (no gap decides it): stop first if 52w low / 52w avg < crash_ratio, else target
      time     close of the first session on or after buy_date + hold_days
    stop_pct defaults to the QQQ regime on the buy date (18% normal / 30% bear).
    Returns (result dict, daily DataFrame of the holding period)."""
    s = load_strategy(strategy)
    if s.get("kind") == "support_resistance" and s.get("sell", "resistance") == "resistance":
        raise ValueError("trade check is for fixed-% targets - for selling at resistance use the Backtest page")
    own = tcon is None
    tcon = tcon or connect(TECH_DB, read_only=True)
    ticker, buy_date = ticker.upper(), _d(buy_date)
    tp_pct = s["take_profit_pct"] if take_profit_pct is None else float(take_profit_pct)
    hold = s["hold_days"] if hold_days is None else int(hold_days)
    px = tcon.execute("""SELECT p.date, p.open, p.high, p.low, p.close, t.low_52w, t.avg_close_52w
                         FROM prices_daily p LEFT JOIN technicals_daily t USING (ticker, date)
                         WHERE p.ticker = ? AND p.date >= ? ORDER BY p.date""", [ticker, buy_date]).df()
    if px.empty or pd.Timestamp(px["date"].iloc[0]).date() != buy_date:
        if own:
            tcon.close()
        raise ValueError(f"{ticker} has no price on {buy_date} (not a trading day, or not listed then)")
    px["date"] = pd.to_datetime(px["date"]).dt.date
    reg = regime(tcon, buy_date, s, regime_method)
    if stop_pct is None:
        if reg is None:
            raise ValueError(f"no QQQ price on {buy_date} to decide the market regime")
        stop_pct = reg["stop_pct"]
    entry = float(px["close"].iloc[0])
    stop = entry * (1 - float(stop_pct) / 100)
    target = entry * (1 + tp_pct / 100)
    limit_day = buy_date + dt.timedelta(days=hold)
    path = px.iloc[1:].copy()
    exit_ = None
    n = 0
    for r in path.itertuples(index=False):
        hit_stop, hit_tp = r.low <= stop, r.high >= target
        reason = price = None
        if hit_stop and r.open <= stop:
            reason, price = "stop loss (opened below the stop)", r.open
        elif hit_tp and r.open >= target:
            reason, price = "take profit (opened above the target)", r.open
        elif hit_stop and hit_tp:
            crash = (r.low_52w / r.avg_close_52w) if r.low_52w and r.avg_close_52w else None
            if crash is not None and crash < s["crash_ratio"]:
                reason, price = f"stop loss (same day as target; 52w low/avg {crash:.2f} < {s['crash_ratio']})", stop
            else:
                reason, price = "take profit (same day as stop; rule: target first)", target
        elif hit_stop:
            reason, price = "stop loss", stop
        elif hit_tp:
            reason, price = "take profit", target
        elif r.date >= limit_day:
            reason, price = f"time exit ({hold} days)", r.close
        n += 1
        if reason:
            exit_ = (r.date, price, reason)
            break
    held = path.iloc[:n].reset_index(drop=True)
    last_day = px["date"].iloc[-1]
    if exit_ is None:
        latest = run(tcon, "SELECT max(date) FROM prices_daily WHERE ticker = ?", [s["regime_ticker"]])[0][0]
        if last_day < latest:
            status, reason = "data ends", "price data ends (delisted / acquired) - last close"
        else:
            status, reason = "open", "still open - latest close"
        exit_ = (held["date"].iloc[-1] if len(held) else buy_date,
                 float(held["close"].iloc[-1]) if len(held) else entry, reason)
    else:
        status = "closed"
    x_date, x_price, x_reason = exit_
    res = {"ticker": ticker, "strategy": s["name"], "buy_date": buy_date, "entry_price": entry,
           "regime_on_buy_date": ("bear" if reg and reg["bear"] else "normal") if reg else None,
           "regime_method": reg["method"] if reg else None,
           "qqq_drawdown_on_buy_date": reg["qqq_drawdown"] if reg else None,
           "stop_pct": float(stop_pct), "stop_price": stop, "take_profit_pct": tp_pct, "take_profit_price": target,
           "time_exit_on_or_after": limit_day, "status": status, "exit_date": x_date, "exit_price": x_price,
           "exit_reason": x_reason, "return_pct": (x_price / entry - 1) * 100,
           "days_held": (x_date - buy_date).days}
    if len(held):
        i_hi, i_lo, i_cl = held["high"].idxmax(), held["low"].idxmin(), held["close"].idxmax()
        res.update({"max_return_pct": (held.loc[i_hi, "high"] / entry - 1) * 100,
                    "max_return_date": held.loc[i_hi, "date"],
                    "max_close_return_pct": (held.loc[i_cl, "close"] / entry - 1) * 100,
                    "max_close_date": held.loc[i_cl, "date"],
                    "max_drawdown_pct": (held.loc[i_lo, "low"] / entry - 1) * 100,
                    "max_drawdown_date": held.loc[i_lo, "date"]})
    divs = run(tcon, """SELECT coalesce(sum(value), 0) FROM corporate_actions WHERE ticker = ?
                        AND action = 'dividend' AND date > ? AND date <= ?""", [ticker, buy_date, x_date])[0][0]
    res["dividends_per_share"] = float(divs)
    res["return_with_dividends_pct"] = ((x_price + float(divs)) / entry - 1) * 100
    held = held.assign(return_pct=(held["close"] / entry - 1) * 100) if len(held) else held
    if own:
        tcon.close()
    return res, held
