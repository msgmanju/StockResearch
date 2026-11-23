"""
Portfolio backtest with a fixed starting capital (step 9b). See docs/BACKTEST.md.

Day by day, using only what was known on each day:
  1. sells   - for every holding bought before today: stop, take-profit, time exit (first one hit),
               or the last close if the stock's prices end (acquired / delisted)
  2. dividends on holdings are added to cash (ex-date)
  3. buys    - index members that day passing the 52w high/low filter whose LOW touches the buy price;
               filled at the buy price (at the open if it opened below); deepest discount first
               while there are free slots and cash
  4. the portfolio is valued at the close
Prices are split-adjusted, so fractional shares are used (whole shares would be wrong on adjusted prices).

Strategy kind "support_resistance" (Support Buyer, [support_buyer]): the buy price is the day's SUPPORT (sr_daily: the
level known before the day's open). Which first (rank_by): "near_52w" = support closest to the 52-week low
or the 52-week average close (both from the 252 sessions before the day), optionally only within near_pct %;
"touches" = strongest support (most touches). Selling:
  sell = "take_profit" (strategy 2 now): fixed take-profit % from the buy price, stop and time exit as for v1
  sell = "resistance" (first experiment): target = resistance (see sr_target); only stocks whose resistance
                                         is at least min_upside_pct above support

Options for the Big-Cap Dip Buyer ([bigcap_dip_buyer]; all off by default, so the others are unchanged):
  top_n_mcap        buy only stocks whose COMPANY is among the N largest by market cap of the universe members
                    that day (market cap = close x shares known before the day; src/calculate/market_cap_history.py)
  sell_if_out_of_top  also sell a holding at the close on a day its company is not in the top N any more
  rev_growth_min    buy only if revenue growth (rev_growth_basis: "ttm" = last 12 months vs the 12 before,
                    "q" = latest quarter vs a year earlier) from SEC reports filed before the day is at least this %
  cash_in_qqq       money not in stocks is held in QQQ: it earns QQQ's daily return (price + dividends) and
                    buys / sells are settled against QQQ at that day's close (cost_pct charged on each switch)
  idle_switch       "sgov_below_200": with cash_in_qqq, the idle money moves to SGOV (0-3 month T-bills) at the
                    close of a day QQQ closes below its 200-day average, and back to QQQ at the close of a day it
                    closes at or above it (cost_pct charged twice per switch: sell one, buy the other).
                    "sgov_always": idle money always in SGOV. SGOV started in May 2020; before that BIL (1-3 month
                    T-bills, since 2007) stands in. T-bill funds earn their dividends (that is their interest).
  exit_plan = "scale"  scaled exit instead of one take-profit (Dip Buyer rules; not used by the Support Buyer):
                    +scale1_pct: sell scale1_sell % of the shares, move the stop up to the buy price (breakeven);
                    +scale2_pct: sell scale2_sell % of the shares, the rest gets a trail_pct trailing stop
                    (trailing level = highest high since step 2 x (1 - trail_pct), raised at each close);
                    the rest is sold when the (breakeven / trailing) stop is hit. The time exit applies only
                    before step 2. A day that hits a stop and a step: the stop is assumed first (worst case).
                    Each partial sale is a row in the trades table.
  sell_all_below_200  market-timing exit: on a day QQQ CLOSES below its 200-day average every holding is sold at
                    that close ("QQQ below its 200-day average"); no new buys on a day after a close below the
                    average (known before the open) until QQQ closes at or above it again.
"""
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.calculate import fundamentals as fu
from src.common import FUND_DB, connect, run
from src import membership as mb
from src.strategy import engine as en


@dataclass
class Settings:
    start: dt.date = dt.date(2017, 1, 1)
    end: dt.date | None = None
    capital: float = 50_000.0
    max_positions: int = 30
    sizing: str = "equity"            # "equity" = total portfolio value / max_positions at the time of the buy
                                      # "fixed"  = starting capital / max_positions
    take_profit_pct: float = 20.0
    stop_pct: float = 30.0
    stop_rule: str = "fixed"          # "fixed" = stop_pct always; "bear_years" = strategy's normal/bear stops
    hold_days: int = 365
    universe: str | None = "NDX"      # None = the strategy's own index (strategies.toml)
    hl_filter: bool = True
    only_top_n: bool = False          # also require discount rank <= max_positions that day
    pe_max: float | None = None       # buy only if P/E (buy price / TTM EPS known that day) <= this; loss-makers out
    de_max: float | None = None       # buy only if debt / equity (latest report) <= this; negative equity out
    keep_missing: bool = False        # keep stocks with no SEC figure for an active filter (default: skip them)
    cooldown_days: int = 0            # optional: don't buy a stock again for N days after it was stopped out
    market_filter: bool = False       # optional: no new buys while QQQ closes below its 200-day average
    cost_pct: float = 0.05            # per buy and per sell
    dividends: bool = True
    strategy: str = "dip_buyer"       # dip_buyer, support_buyer, bigcap_dip_buyer (old keys mr_v3, sr_v1, mr_top30 work too)
    min_upside_pct: float = 5.0       # support/resistance strategies: resistance at least this % above support
    rank_by: str | None = None        # strategy 2: "near_52w" or "touches" (None = the strategy's setting)
    near_pct: float | None = None     # strategy 2 + near_52w: only supports within this % of the 52w low/avg (0 = no limit)
    sr_target: str = "daily"          # "daily" = each day's resistance; "fixed" = resistance known on the buy day;
                                      # "daily_above_buy" = each day's resistance, but only levels above the buy price
    top_n_mcap: int | None = None     # only the N largest companies by market cap (universe members that day);
                                      # None = the strategy's setting ([bigcap_dip_buyer]: 30), 0 = off
    sell_if_out_of_top: bool | None = None  # sell at the close when the company drops out of the top N
    rev_growth_min: float | None = None   # buy only if revenue growth (%) known that day is at least this
    rev_growth_basis: str = "ttm"     # "ttm" = last 12 months vs the 12 before; "q" = latest quarter vs a year earlier
    cash_in_qqq: bool | None = None   # money not in stocks is held in QQQ instead of cash (None = strategy's setting)
    idle_switch: str | None = None    # "none"; "sgov_below_200" = QQQ, but SGOV while QQQ is below its 200-day
                                      # average; "sgov_always" = SGOV always (None = strategy's setting)
    sell_all_below_200: bool = False  # sell every holding when QQQ closes below its 200-day average; no buys until back above
    exit_plan: str = "fixed"          # "fixed" = one take-profit / stop; "scale" = scale out + trailing stop
    scale1_pct: float = 15.0          # scale: first step, % above the buy price
    scale1_sell: float = 25.0         # scale: % of the shares sold at the first step (stop then -> buy price)
    scale2_pct: float = 30.0          # scale: second step, % above the buy price
    scale2_sell: float = 50.0         # scale: % of the shares sold at the second step (the rest: trailing stop)
    trail_pct: float = 10.0           # scale: trailing stop, % below the highest high since the second step
    s: dict = field(default_factory=dict)


def is_sr(s):
    return s.get("kind") == "support_resistance"


def near_52w_pct(support, low_52w, avg_52w):
    """% distance of the support from the nearer of the 52-week low and the 52-week average close."""
    return np.minimum((support / low_52w - 1).abs(), (support / avg_52w - 1).abs()) * 100


def sells_at_resistance(s):
    return is_sr(s) and s.get("sell", "resistance") == "resistance"


def load_data(con, st, fcon=None):
    """One row per (date, ticker) for every stock that was in the universe at some point in the period."""
    names = mb.index_names(st.universe)
    q = ",".join("?" * len(names))
    end = st.end or run(con, "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]
    mem = con.execute(f"""SELECT ticker, start_date, coalesce(end_date, DATE '2100-01-01') AS end_date
                          FROM index_membership WHERE index_name IN ({q}) AND start_date <= ?
                          AND (end_date IS NULL OR end_date >= ?)""", names + [end, st.start]).df()
    for c in ("start_date", "end_date"):
        mem[c] = pd.to_datetime(mem[c]).dt.date
    tick = sorted(mem["ticker"].unique())
    tq = ",".join("?" * len(tick))
    d = con.execute(f"""
        SELECT p.date, p.ticker, p.open, p.high, p.low, p.close, t.low_52w, t.high_52w, t.avg_close_52w,
               sr.support, sr.support_touches, sr.resistance, sr.resistance_touches,
               EXISTS (SELECT 1 FROM index_membership m WHERE m.ticker = p.ticker AND m.index_name IN ({q})
                       AND m.start_date <= p.date AND (m.end_date IS NULL OR m.end_date >= p.date)) AS member
        FROM prices_daily p LEFT JOIN technicals_daily t USING (ticker, date)
        LEFT JOIN sr_daily sr USING (ticker, date)
        WHERE p.ticker IN ({tq}) AND p.date BETWEEN ? AND ?""", names + tick + [st.start, end]).df()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    if st.top_n_mcap:                                  # strategy 3: market cap rank of each company that day
        from src.calculate import market_cap_history as mh
        fcon = fcon or connect(FUND_DB, read_only=True)
        d = mh.add_mcap_rank(mh.add_market_cap(d, fcon, con), fcon)
        d = d.sort_values(["date", "ticker"]).reset_index(drop=True)
    s = st.s
    d["hl_ratio"] = d["high_52w"] / d["low_52w"]
    if is_sr(s):
        d = d.sort_values(["date", "ticker"]).reset_index(drop=True)   # same order every run (ties, see buys)
        d["buy_price"] = d["support"]
        d["upside"] = d["resistance"] / d["support"] - 1
        d["near_52w_pct"] = near_52w_pct(d["support"], d["low_52w"], d["avg_close_52w"])
        rank_by = st.rank_by or s.get("rank_by", "touches")
        near = st.near_pct if st.near_pct is not None else s.get("near_pct", 0)
        if rank_by == "near_52w":                # sort key: closest to the 52w low/avg, then most touches
            d["discount"] = d["near_52w_pct"] - d["support_touches"].fillna(0) / 1000
        else:                                    # most touches, then larger upside
            d["discount"] = -d["support_touches"] * 1000 - d["upside"].fillna(0)
        elig = d["member"] & d["support"].notna() & d["low_52w"].notna() \
            & ((d["hl_ratio"] <= s["hl_max"]) | (not st.hl_filter))
        if rank_by == "near_52w" and near:
            elig &= d["near_52w_pct"] <= near
        if sells_at_resistance(s):
            elig &= d["resistance"].notna() & (d["upside"] * 100 >= st.min_upside_pct)
        if st.top_n_mcap:
            elig &= d["mcap_rank"] <= st.top_n_mcap
    else:
        d["buy_price"] = s["buy_low_weight"] * d["low_52w"] + s["buy_avg_weight"] * d["avg_close_52w"]
        d["discount"] = d["low_52w"] / d["buy_price"]
        elig = d["member"] & d["buy_price"].notna() & ((d["hl_ratio"] <= s["hl_max"]) | (not st.hl_filter))
        if st.top_n_mcap:
            elig &= d["mcap_rank"] <= st.top_n_mcap
    d["rank"] = np.nan
    d.loc[elig, "rank"] = d[elig].groupby("date")["discount"].rank(method="first")
    divs = con.execute(f"""SELECT ticker, date, value FROM corporate_actions WHERE action = 'dividend'
                           AND ticker IN ({tq}) AND date BETWEEN ? AND ?""", tick + [st.start, end]).df()
    divs["date"] = pd.to_datetime(divs["date"]).dt.date
    qqq = con.execute("""SELECT date, close, avg(close) OVER (ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)
                         AS ma200 FROM prices_daily WHERE ticker = 'QQQ' AND date <= ? ORDER BY date""", [end]).df()
    qqq["date"] = pd.to_datetime(qqq["date"]).dt.date
    qqq = qqq[qqq["date"] >= st.start].reset_index(drop=True)
    last = d.groupby("ticker")["date"].max().to_dict()
    return d, divs, qqq, last, end


def add_fundamentals(con, fcon, d):
    """P/E inputs and debt/equity as KNOWN on each day: the latest SEC report filed before the day.
    EPS is restated to today's share count (prices are split-adjusted to today)."""
    tick = sorted(d["ticker"].unique())
    q = ",".join("?" * len(tick))
    f = fcon.execute(f"""SELECT ticker, filed, eps_diluted_ttm, debt_to_equity, equity, revenue_growth_q_yoy,
                                revenue_growth_ttm_yoy FROM fund_snapshots WHERE ticker IN ({q})""", tick).df()
    sp = con.execute(f"SELECT ticker, date, value FROM corporate_actions WHERE action = 'split' AND ticker IN ({q})",
                     tick).fetchall()
    splits = {}
    for t, day, v in sp:
        splits.setdefault(t, []).append((day, v))
    splits = {t: fu.real_splits(v) for t, v in splits.items()}
    f["filed"] = pd.to_datetime(f["filed"]).dt.date
    f["eps_adj"] = [fu.eps_today_basis(e, fd, splits.get(t, [])) if pd.notna(e) else np.nan
                    for t, e, fd in zip(f["ticker"], f["eps_diluted_ttm"], f["filed"])]
    f["usable_from"] = pd.to_datetime(f["filed"]) + pd.Timedelta(days=1)
    d = d.assign(_ts=pd.to_datetime(d["date"]).astype("datetime64[ns]")).sort_values("_ts")
    f["usable_from"] = f["usable_from"].astype("datetime64[ns]")
    f = f.sort_values("usable_from")[["ticker", "usable_from", "filed", "eps_adj", "debt_to_equity", "equity",
                                      "revenue_growth_q_yoy", "revenue_growth_ttm_yoy"]]
    d = pd.merge_asof(d, f, left_on="_ts", right_on="usable_from", by="ticker", direction="backward")
    return d.drop(columns=["_ts", "usable_from"]).rename(columns={"filed": "fund_filed"})


def fund_ok(c, st):
    """Rows of today's candidates passing the P/E and debt/equity limits."""
    ok = pd.Series(True, index=c.index)
    if st.pe_max is not None:
        pe = c["buy_price"] / c["eps_adj"]
        good = (c["eps_adj"] > 0) & (pe <= st.pe_max)
        ok &= good | (c["eps_adj"].isna() & st.keep_missing)
    if st.de_max is not None:
        good = (c["equity"] > 0) & (c["debt_to_equity"] <= st.de_max)
        ok &= good | (c["debt_to_equity"].isna() & st.keep_missing)
    if st.rev_growth_min is not None:
        col = "revenue_growth_q_yoy" if st.rev_growth_basis == "q" else "revenue_growth_ttm_yoy"
        good = c[col] * 100 >= st.rev_growth_min
        ok &= good | (c[col].isna() & st.keep_missing)
    return ok


def run_backtest(con, st: Settings, progress=None, fcon=None):
    st.strategy = en.strategy_key(st.strategy)
    st.s = en.load_strategy(st.strategy)
    if not st.universe:
        st.universe = st.s.get("universe", "NDX")
    if st.top_n_mcap is None:                          # strategy defaults (only [bigcap_dip_buyer] sets them)
        st.top_n_mcap = st.s.get("top_n_mcap") or None
    st.top_n_mcap = int(st.top_n_mcap) if st.top_n_mcap else None
    if st.cash_in_qqq is None:
        st.cash_in_qqq = bool(st.s.get("cash_in_qqq", False))
    if st.idle_switch is None:
        st.idle_switch = st.s.get("idle_switch", "none")
    if st.idle_switch not in IDLE_RULES:
        raise ValueError(f"idle_switch must be one of {', '.join(IDLE_RULES)}")
    if st.sell_if_out_of_top is None:
        st.sell_if_out_of_top = bool(st.s.get("sell_if_out_of_top", False))
    sr_mode = sells_at_resistance(st.s)              # target = resistance (first strategy-2 experiment)
    d, divs, qqq, last_date, end = load_data(con, st, fcon)
    use_fund = st.pe_max is not None or st.de_max is not None or st.rev_growth_min is not None
    if use_fund:
        d = add_fundamentals(con, fcon or connect(FUND_DB, read_only=True), d)
    days = list(qqq["date"])
    by_day = {k: g.set_index("ticker") for k, g in d.groupby("date")}
    div_by_day = {k: g.set_index("ticker")["value"].to_dict() for k, g in divs.groupby("date")}
    final_day = days[-1]
    fee = st.cost_pct / 100
    cash, pos, trades, curve, last_close = st.capital, {}, [], [], {}
    stopped = {}                                   # ticker -> day it was last stopped out
    qqq_ok = {r.date: r.close >= r.ma200 for r in qqq.itertuples()}
    # idle money in QQQ / SGOV - growth factor of each day (close + dividend) / previous close
    qqq_growth, qqq_gain, qqq_fees = {}, 0.0, 0.0
    idle_on = st.cash_in_qqq or st.idle_switch == "sgov_always"
    idle = {"held": None, "days": {}, "gain": {}, "switches": 0, "rule_eq": st.capital}
    tbill, below = {}, {}
    if idle_on:
        qd = dict(run(con, "SELECT date, value FROM corporate_actions WHERE ticker = 'QQQ' AND action = 'dividend' "
                           "AND date BETWEEN ? AND ?", [st.start, end])) if st.dividends else {}
        closes, ma200 = list(qqq["close"]), list(qqq["ma200"])
        qqq_growth = {day: (closes[i] + qd.get(day, 0.0)) / closes[i - 1] for i, day in enumerate(days) if i > 0}
        below = {day: closes[i] < ma200[i] for i, day in enumerate(days)}
        if st.idle_switch != "none":
            tbill = tbill_growth(con, days)

    def idle_asset(day):
        """Where idle money sits from the close of `day` to the next close."""
        if st.idle_switch == "sgov_always" or (st.idle_switch == "sgov_below_200" and below.get(day)):
            return "T-bills"
        return "QQQ"

    if idle_on:
        idle["held"] = idle_asset(days[0])
        qqq_fees += cash * fee                         # the starting money is put to work at the first close
        cash -= cash * fee
        idle["rule_eq"] *= 1 - fee

    def sell(t, day, price, reason, n=None):
        """Sell all shares of t (n=None) or n of them (a partial sale; the position keeps the rest)."""
        nonlocal cash, qqq_fees
        full = n is None or n >= pos[t]["shares"] - 1e-9
        if full:
            p = pos.pop(t)
        else:                                          # split the position: the part sold becomes a trade row
            q = pos[t]
            k = n / q["shares"]
            p = {**q, "shares": n, "cost": q["cost"] * k, "divs": q["divs"] * k}
            q["shares"] -= n
            q["cost"] -= p["cost"]
            q["divs"] -= p["divs"]
        if reason.startswith("stop"):
            stopped[t] = day
        gross = p["shares"] * price
        cash += gross * (1 - fee)
        if idle_on:                                    # proceeds go into QQQ / SGOV at the close
            qqq_fees += gross * (1 - fee) * fee
            cash -= gross * (1 - fee) * fee
        trades.append({"ticker": t, "buy_date": p["date"], "buy_price": p["price"], "shares": p["shares"],
                       "cost": p["cost"], "sell_date": day, "sell_price": price, "proceeds": gross * (1 - fee),
                       "dividends": p["divs"], "reason": reason,
                       "profit": gross * (1 - fee) + p["divs"] - p["cost"],
                       "return_pct": (gross * (1 - fee) + p["divs"]) / p["cost"] * 100 - 100,
                       "price_return_pct": (price / p["price"] - 1) * 100,
                       "days_held": (day - p["date"]).days, "discount_rank": p["rank"],
                       "support_touches": p.get("touches"), "resistance_at_buy": p.get("target_at_buy"),
                       "support_vs_52w_pct": p.get("near"),
                       "pe_at_buy": p["pe"], "debt_to_equity_at_buy": p["de"],
                       "mcap_rank_at_buy": p.get("mcap_rank"), "mcap_at_buy": p.get("mcap"),
                       "rev_growth_at_buy_pct": p.get("rev_growth"), "partial": not full})

    q_below = {r.date: r.close < r.ma200 for r in qqq.itertuples()}
    scale_mode = st.exit_plan == "scale" and not sr_mode
    if st.exit_plan not in ("fixed", "scale"):
        raise ValueError("exit_plan must be 'fixed' or 'scale'")

    def scale_exit(t, p, r, day):
        """Scaled exit for one position on one day (see the module notes)."""
        lo, hi, op, cl = float(r["low"]), float(r["high"]), float(r["open"]), float(r["close"])
        entry, stage = p["price"], p.get("stage", 0)
        if lo <= p["stop"]:                                      # stop first (worst case)
            px = op if op <= p["stop"] else p["stop"]
            name = ["stop", "breakeven stop", "trailing stop"][stage]
            sell(t, day, float(px), name + (" (gap)" if op <= p["stop"] else ""))
            return
        for step, lvl_pct, part in ((0, st.scale1_pct, st.scale1_sell), (1, st.scale2_pct, st.scale2_sell)):
            lvl = entry * (1 + lvl_pct / 100)
            if p.get("stage", 0) == step and hi >= lvl:
                px = op if op >= lvl else lvl
                n = p["shares0"] * part / 100
                p["stage"] = step + 1
                if step == 0:
                    p["stop"] = max(p["stop"], entry)            # breakeven
                else:
                    p["peak"] = hi
                    p["stop"] = max(p["stop"], hi * (1 - st.trail_pct / 100))
                sell(t, day, float(px), f"scale-out {step + 1} (+{lvl_pct:g}%)", n=n)
                if t not in pos:
                    return
        if p.get("stage", 0) == 2:                               # trailing stop rises with new highs (at the close)
            p["peak"] = max(p.get("peak", hi), hi)
            p["stop"] = max(p["stop"], p["peak"] * (1 - st.trail_pct / 100))
        elif day >= p["limit"]:
            sell(t, day, cl, "time exit")
    for i, day in enumerate(days):
        if progress and i % 50 == 0:
            progress(i / len(days))
        today = by_day.get(day)
        if idle_on and i > 0:                          # yesterday's idle money earned QQQ's / SGOV's return today
            if idle["held"] == "QQQ":
                g, src = qqq_growth.get(day, 1.0), "QQQ"
            else:
                g, src = tbill.get(day, (1.0, "no T-bill price (counted as cash)"))
            qqq_gain += cash * (g - 1)
            idle["gain"][src] = idle["gain"].get(src, 0.0) + cash * (g - 1)
            idle["days"][src] = idle["days"].get(src, 0) + 1
            cash *= g
            idle["rule_eq"] *= g
        # 1. sells
        for t in list(pos):
            p = pos[t]
            if today is None or t not in today.index:
                continue
            r = today.loc[t]
            lo, hi, op = r["low"], r["high"], r["open"]
            if scale_mode:
                scale_exit(t, p, r, day)
                continue
            if sr_mode and st.sr_target != "fixed":     # target = today's resistance (known before the open)
                res_today = float(r["resistance"]) if pd.notna(r["resistance"]) else np.inf
                if st.sr_target == "daily" or res_today > p["price"]:
                    p["target"] = res_today
            hit_s, hit_t = lo <= p["stop"], hi >= p["target"]
            reason = price = None
            if hit_s and op <= p["stop"]:
                reason, price = "stop (gap)", op
            elif hit_t and op >= p["target"]:
                reason, price = "target (gap)", op
            elif hit_s and hit_t:
                crash = r["low_52w"] / r["avg_close_52w"] if r["avg_close_52w"] else None
                if crash is not None and crash < st.s["crash_ratio"]:
                    reason, price = "stop (same day as target)", p["stop"]
                else:
                    reason, price = "target (same day as stop)", p["target"]
            elif hit_s:
                reason, price = "stop", p["stop"]
            elif hit_t:
                reason, price = "target", p["target"]
            elif day >= p["limit"]:
                reason, price = "time exit", r["close"]
            if reason:
                if sr_mode and reason.startswith("target"):
                    reason = reason.replace("target", "resistance") + (" (below buy price)" if price < p["price"] else "")
                sell(t, day, float(price), reason)
        if st.top_n_mcap and st.sell_if_out_of_top and today is not None:
            for t in list(pos):
                if t in today.index and not (today.loc[t, "mcap_rank"] <= st.top_n_mcap):
                    sell(t, day, float(today.loc[t, "close"]), f"out of the top {st.top_n_mcap} by market cap")
        # 2. dividends
        if st.dividends:
            for t, v in div_by_day.get(day, {}).items():
                if t in pos:
                    amt = pos[t]["shares"] * v
                    cash += amt
                    pos[t]["divs"] += amt
        # 3. buys
        timing_ok = not (st.sell_all_below_200 and i > 0 and q_below.get(days[i - 1]))   # known before the open
        if today is not None and len(pos) < st.max_positions and (not st.market_filter or qqq_ok.get(day, True)) \
                and timing_ok:
            c = today[today["member"] & today["rank"].notna() & (today["low"] <= today["buy_price"])]
            if st.only_top_n:
                c = c[c["rank"] <= st.max_positions]
            if use_fund and len(c):
                c = c[fund_ok(c, st)]
            c = c[~c.index.isin(list(pos))]
            # strategy 2: many stocks tie (e.g. support = 52w low) - break ties by ticker so every run is the same
            c = c.sort_index().sort_values("discount", kind="mergesort") if is_sr(st.s) else c.sort_values("discount")
            if st.cooldown_days:
                c = c[[not (t in stopped and (day - stopped[t]).days <= st.cooldown_days) for t in c.index]]
            equity = cash + sum(p["shares"] * last_close.get(t, p["price"]) for t, p in pos.items())
            for t, r in c.iterrows():
                if len(pos) >= st.max_positions:
                    break
                size = (equity if st.sizing == "equity" else st.capital) / st.max_positions
                size = min(size, cash / (1 + fee) if idle_on else cash)
                if size < 1:
                    break
                price = float(min(r["open"], r["buy_price"]))
                shares = size * (1 - fee) / price
                stop_pct = st.stop_pct
                if st.stop_rule == "bear_years":
                    stop_pct = st.s["stop_bear_pct"] if day.year in st.s.get("bear_years", []) \
                        else st.s["stop_normal_pct"]
                cash -= size
                if idle_on:                            # QQQ / SGOV sold at the close to pay for it
                    qqq_fees += size * fee
                    cash -= size * fee
                rg = r.get("revenue_growth_q_yoy" if st.rev_growth_basis == "q" else "revenue_growth_ttm_yoy")
                pos[t] = {"mcap_rank": r.get("mcap_rank"), "mcap": r.get("market_cap"),
                          "rev_growth": rg * 100 if rg is not None and pd.notna(rg) else None,"date": day, "price": price, "shares": shares, "cost": size, "divs": 0.0,
                          "stop": price * (1 - stop_pct / 100),
                          "target": float(r["resistance"]) if sr_mode else price * (1 + st.take_profit_pct / 100),
                          "touches": r.get("support_touches"), "near": r.get("near_52w_pct"),
                          "target_at_buy": r.get("resistance") if sr_mode else price * (1 + st.take_profit_pct / 100),
                          "limit": day + dt.timedelta(days=st.hold_days), "rank": r["rank"], "shares0": shares,
                          "pe": (r["buy_price"] / r["eps_adj"]) if use_fund and r.get("eps_adj", 0) and r["eps_adj"] > 0
                          else None,
                          "de": r.get("debt_to_equity") if use_fund else None}
        # 4. value at the close; prices that end -> sold at the last close
        if today is not None and pos:
            closes = today["close"].to_dict()
            for t in pos:
                if t in closes:
                    last_close[t] = float(closes[t])
        if st.sell_all_below_200 and q_below.get(day) and pos:     # QQQ closed below its 200-day average
            for t in list(pos):
                sell(t, day, last_close.get(t, pos[t]["price"]), "QQQ below its 200-day average")
        for t in list(pos):
            if last_date.get(t) == day and day < final_day:
                sell(t, day, last_close[t], "prices end (acquired / delisted)")
        if idle_on and st.idle_switch == "sgov_below_200" and i < len(days) - 1:
            nxt = idle_asset(day)                      # QQQ crossed its 200-day average: switch at the close
            if nxt != idle["held"]:
                qqq_fees += cash * fee * 2
                cash -= cash * fee * 2
                idle["rule_eq"] *= 1 - 2 * fee
                idle["switches"] += 1
                idle["held"] = nxt
        invested = sum(p["shares"] * last_close.get(t, p["price"]) for t, p in pos.items())
        curve.append({"date": day, "cash": cash, "invested": invested, "equity": cash + invested,
                      "positions": len(pos)})

    open_pos = [{"ticker": t, "buy_date": p["date"], "buy_price": p["price"], "shares": p["shares"],
                 "cost": p["cost"], "last_close": last_close.get(t, p["price"]),
                 "value": p["shares"] * last_close.get(t, p["price"]), "dividends": p["divs"],
                 "return_pct": (p["shares"] * last_close.get(t, p["price"]) + p["divs"]) / p["cost"] * 100 - 100,
                 "stop": p["stop"], "target": p["target"], "time_exit_on_or_after": p["limit"]}
                for t, p in pos.items()]
    curve = pd.DataFrame(curve)
    curve = curve.merge(qqq[["date", "close"]].rename(columns={"close": "qqq_close"}), on="date", how="left")
    curve["qqq_equity"] = st.capital * curve["qqq_close"] / curve["qqq_close"].iloc[0]
    res = summarize(st, curve, pd.DataFrame(trades), pd.DataFrame(open_pos))
    if idle_on:
        S = res["summary"]
        S["idle_money"] = IDLE_RULES[st.idle_switch] if st.idle_switch != "none" else "QQQ"
        S["gain_on_idle_money"] = qqq_gain
        S["idle_switch_costs"] = qqq_fees
        S["idle_days"] = idle["days"]
        S["idle_gain_by_fund"] = idle["gain"]
        if st.idle_switch != "none":
            yrs_ = (days[-1] - days[0]).days / 365.25
            S["idle_switches"] = idle["switches"]
            S["idle_rule_only_final_value"] = idle["rule_eq"]   # the parking rule alone, no stocks
            S["idle_rule_only_cagr_pct"] = ((idle["rule_eq"] / st.capital) ** (1 / yrs_) - 1) * 100 if yrs_ > 0 else np.nan
            if not tbill:
                S["warning"] = "no SGOV / BIL prices stored - T-bill days counted as cash (run ./start.sh)"
        tr = st.capital * float(np.prod(list(qqq_growth.values()))) if qqq_growth else st.capital
        yrs = (days[-1] - days[0]).days / 365.25
        res["summary"]["qqq_with_dividends_final_value"] = tr
        res["summary"]["qqq_with_dividends_cagr_pct"] = ((tr / st.capital) ** (1 / yrs) - 1) * 100 if yrs > 0 else np.nan
    return res


IDLE_RULES = {"none": "cash or QQQ (cash_in_qqq)",
              "sgov_below_200": "QQQ; SGOV (T-bills) while QQQ is below its 200-day average",
              "sgov_always": "SGOV (T-bills) always"}


def tbill_growth(con, days):
    """{day: (growth factor, fund)} of 0-3 month T-bills: SGOV where it has prices, BIL before SGOV started
    (May 2020). Growth = (close + dividend that day) / previous close - the dividends are the interest."""
    out = {}
    for t, label in (("BIL", "BIL (stand-in before SGOV)"), ("SGOV", "SGOV")):   # SGOV overwrites BIL
        px = dict(run(con, "SELECT date, close FROM prices_daily WHERE ticker = ? AND date BETWEEN ? AND ?",
                      [t, days[0] - dt.timedelta(days=10), days[-1]]))
        if not px:
            continue
        dv = dict(run(con, "SELECT date, value FROM corporate_actions WHERE ticker = ? AND action = 'dividend' "
                           "AND date BETWEEN ? AND ?", [t, days[0], days[-1]]))
        for a, b in zip(days, days[1:]):
            if a in px and b in px and px[a]:
                out[b] = ((px[b] + dv.get(b, 0.0)) / px[a], label)
    return out


def _stats(eq, dates):
    years = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return cagr, dd


def summarize(st, curve, trades, open_pos):
    cagr, dd = _stats(curve["equity"], curve["date"])
    qcagr, qdd = _stats(curve["qqq_equity"], curve["date"])
    c = curve.assign(year=[d.year for d in curve["date"]])
    yearly = c.groupby("year").agg(start=("equity", "first"), end=("equity", "last"),
                                   q_start=("qqq_equity", "first"), q_end=("qqq_equity", "last"))
    prev_end = yearly["end"].shift(1).fillna(st.capital)
    q_prev = yearly["q_end"].shift(1).fillna(st.capital)
    yearly = pd.DataFrame({"portfolio_return_pct": (yearly["end"] / prev_end - 1) * 100,
                           "qqq_return_pct": (yearly["q_end"] / q_prev - 1) * 100,
                           "value_at_year_end": yearly["end"]}).reset_index()
    if len(trades):
        yearly = yearly.merge(trades.assign(year=[d.year for d in trades["sell_date"]]).groupby("year")
                              .agg(trades_closed=("ticker", "size"),
                                   winners=("profit", lambda x: int((x > 0).sum()))).reset_index(),
                              on="year", how="left")
    s = {"start": curve["date"].iloc[0], "end": curve["date"].iloc[-1], "starting_capital": st.capital,
         "final_value": curve["equity"].iloc[-1], "total_return_pct": (curve["equity"].iloc[-1] / st.capital - 1) * 100,
         "cagr_pct": cagr * 100, "max_drawdown_pct": dd * 100,
         "qqq_final_value": curve["qqq_equity"].iloc[-1], "qqq_cagr_pct": qcagr * 100, "qqq_max_drawdown_pct": qdd * 100,
         "trades_closed": len(trades), "open_positions": len(open_pos),
         "win_rate_pct": (trades["profit"] > 0).mean() * 100 if len(trades) else np.nan,
         "avg_trade_return_pct": trades["return_pct"].mean() if len(trades) else np.nan,
         "avg_days_held": trades["days_held"].mean() if len(trades) else np.nan,
         "avg_invested_pct": (curve["invested"] / curve["equity"]).mean() * 100,
         "dividends_received": (trades["dividends"].sum() if len(trades) else 0)
                               + (open_pos["dividends"].sum() if len(open_pos) else 0)}
    if len(trades):
        s["exits"] = trades["reason"].value_counts().to_dict()
    return {"settings": st, "summary": s, "curve": curve, "trades": trades, "open": open_pos, "yearly": yearly}


def write_excel(path, res):
    from src.strategy.report import _format
    st = res["settings"]
    path.parent.mkdir(parents=True, exist_ok=True)
    sett = {k: v for k, v in st.__dict__.items() if k != "s"}
    rows = [("SETTINGS", "")] + [(k, str(v)) for k, v in sett.items()] + [("", ""), ("RESULTS", "")] + \
        [(k, (round(v, 2) if isinstance(v, float) else str(v))) for k, v in res["summary"].items()]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows, columns=["item", "value"]).to_excel(xw, sheet_name="summary", index=False)
        xw.sheets["summary"].column_dimensions["A"].width = 28
        xw.sheets["summary"].column_dimensions["B"].width = 60
        for name in ("yearly", "trades", "open", "curve"):
            df = res[name]
            df.to_excel(xw, sheet_name={"open": "open_positions", "curve": "daily_value"}.get(name, name), index=False)
            _format(xw.sheets[{"open": "open_positions", "curve": "daily_value"}.get(name, name)], df)
