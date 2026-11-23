"""
Portfolio backtest with a fixed starting capital (docs/BACKTEST.md).

    python scripts/backtest.py                                   # $50,000 from 2017, +20% / -30% / 1 year
    python scripts/backtest.py --tp 100 --stop 18 --capital 100000 --start 2018-01-01
    python scripts/backtest.py --strategy support_buyer --tp 50  # Support Buyer: buy at support, sell at +50%
    python scripts/backtest.py --sizing fixed --cost 0 --universe BOTH
    python scripts/backtest.py --universe BOTH --pe-max 30 --de-max 1
    python scripts/backtest.py --universe SP100          # S&P 100 only (or NDX+SP100)
    python scripts/backtest.py --strategy bigcap_dip_buyer   # 30 largest companies, idle money in QQQ
    python scripts/backtest.py --strategy bigcap_dip_buyer --idle qqq-sgov   # SGOV while QQQ < 200-day average
    python scripts/backtest.py --strategy bigcap_dip_buyer --largest 20 --positions 10 --de-max 1 --rev-growth 10

Writes exports/backtests/backtest_<start>_<end>_tp<tp>_stop<stop>.xlsx
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import EXPORTS_DIR, FUND_DB, TECH_DB, connect  # noqa: E402
from src.strategy import backtest as bt  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the buy-price strategy with a fixed starting capital")
    ap.add_argument("--strategy", default="dip_buyer", help="dip_buyer, support_buyer or bigcap_dip_buyer")
    ap.add_argument("--largest", type=int, default=None,
                    help="only the N largest companies by market cap that day (default: the strategy's; 0 = off)")
    ap.add_argument("--sell-out-of-top", action="store_true", help="sell a holding when it drops out of the largest N")
    ap.add_argument("--qqq-cash", choices=["yes", "no"], default=None,
                    help="money not in stocks held in QQQ (default: the strategy's setting)")
    ap.add_argument("--idle", choices=["cash", "qqq", "qqq-sgov", "sgov"], default=None,
                    help="money not in stocks: cash, qqq, qqq-sgov (QQQ, but SGOV while QQQ < its 200-day average) "
                         "or sgov (default: the strategy's setting)")
    ap.add_argument("--sell-all-below-200", action="store_true",
                    help="sell every stock when QQQ closes below its 200-day average; no buys until it is back above")
    ap.add_argument("--scale", nargs=5, type=float, default=None,
                    metavar=("STEP1", "SELL1", "STEP2", "SELL2", "TRAIL"),
                    help="scale out instead of one take-profit, e.g. --scale 15 25 30 50 10 = sell 25%% at +15%% "
                         "(stop -> buy price), 50%% at +30%%, rest with a 10%% trailing stop")
    ap.add_argument("--rev-growth", type=float, default=None, help="buy only if revenue growth (%%) is at least this")
    ap.add_argument("--rev-basis", choices=["ttm", "q"], default="ttm",
                    help="revenue growth: ttm = 12 months vs the 12 before, q = latest quarter vs a year earlier")
    ap.add_argument("--min-upside", type=float, default=5.0, help="Support Buyer: resistance at least this %% above support")
    ap.add_argument("--sr-target", choices=["daily", "fixed", "daily_above_buy"], default="daily",
                    help="Support Buyer: sell at each day's resistance, the one known on the buy day, or each day's only above the buy price")
    ap.add_argument("--rank-by", choices=["near_52w", "touches"], default=None,
                    help="Support Buyer: support closest to the 52w low/avg first, or strongest support first")
    ap.add_argument("--near-pct", type=float, default=None,
                    help="Support Buyer: only supports within this %% of the 52w low/avg (0 = no limit)")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--capital", type=float, default=50_000)
    ap.add_argument("--positions", type=int, default=30)
    ap.add_argument("--sizing", choices=["equity", "fixed"], default="equity")
    ap.add_argument("--tp", type=float, default=20)
    ap.add_argument("--stop", type=float, default=30)
    ap.add_argument("--bear-stops", action="store_true", help="use -18%% / -30%% by bear year instead of --stop")
    ap.add_argument("--hold", type=int, default=365)
    ap.add_argument("--universe", default=None,
                    help="NDX, SP500, SP100, BOTH (NDX+SP500) or NDX+SP100 (default: the strategy's own - "
                         "NDX for dip_buyer / support_buyer, BOTH for bigcap_dip_buyer)")
    ap.add_argument("--no-hl-filter", action="store_true")
    ap.add_argument("--top-n", action="store_true", help="also require discount rank within the top N")
    ap.add_argument("--pe-max", type=float, default=None, help="buy only if P/E <= this (loss-makers excluded)")
    ap.add_argument("--de-max", type=float, default=None, help="buy only if debt/equity <= this (negative equity excluded)")
    ap.add_argument("--keep-missing", action="store_true", help="keep stocks with no SEC figure for a filter")
    ap.add_argument("--cooldown", type=int, default=0, help="optional: days to wait before re-buying a stopped-out stock")
    ap.add_argument("--market-filter", action="store_true", help="optional: no buys while QQQ < its 200-day average")
    ap.add_argument("--cost", type=float, default=0.05)
    ap.add_argument("--no-dividends", action="store_true")
    a = ap.parse_args(argv)
    s = bt.Settings(start=dt.date.fromisoformat(a.start), end=dt.date.fromisoformat(a.end) if a.end else None,
                    capital=a.capital, max_positions=a.positions, sizing=a.sizing, take_profit_pct=a.tp,
                    stop_pct=a.stop, stop_rule="bear_years" if a.bear_stops else "fixed", hold_days=a.hold,
                    universe=a.universe.upper() if a.universe else None, hl_filter=not a.no_hl_filter, only_top_n=a.top_n,
                    cost_pct=a.cost, dividends=not a.no_dividends, pe_max=a.pe_max, de_max=a.de_max,
                    keep_missing=a.keep_missing, cooldown_days=a.cooldown, market_filter=a.market_filter,
                    strategy=a.strategy, min_upside_pct=a.min_upside, sr_target=a.sr_target,
                    rank_by=a.rank_by, near_pct=a.near_pct, top_n_mcap=a.largest,
                    sell_if_out_of_top=True if a.sell_out_of_top else None,
                    cash_in_qqq={"yes": True, "no": False}.get(a.qqq_cash) if a.idle is None
                    else a.idle in ("qqq", "qqq-sgov"),
                    idle_switch=None if a.idle is None else {"qqq-sgov": "sgov_below_200", "sgov": "sgov_always"}.get(a.idle, "none"),
                    rev_growth_min=a.rev_growth, sell_all_below_200=a.sell_all_below_200,
                    **({"exit_plan": "scale", "scale1_pct": a.scale[0], "scale1_sell": a.scale[1],
                        "scale2_pct": a.scale[2], "scale2_sell": a.scale[3], "trail_pct": a.scale[4]} if a.scale else {}),
                    rev_growth_basis=a.rev_basis)
    res = bt.run_backtest(connect(TECH_DB, read_only=True), s, fcon=connect(FUND_DB, read_only=True))
    S = res["summary"]
    print(f"{S['start']} to {S['end']}: ${S['starting_capital']:,.0f} -> ${S['final_value']:,.0f} "
          f"({S['total_return_pct']:+.1f}%, {S['cagr_pct']:.1f}% a year, worst fall {S['max_drawdown_pct']:.1f}%)")
    print(f"QQQ buy & hold: ${S['qqq_final_value']:,.0f} ({S['qqq_cagr_pct']:.1f}% a year, "
          f"worst fall {S['qqq_max_drawdown_pct']:.1f}%)")
    print(f"{S['trades_closed']} trades closed, win rate {S['win_rate_pct']:.0f}%, average {S['avg_trade_return_pct']:+.1f}%"
          f" over {S['avg_days_held']:.0f} days; {S['open_positions']} still open; exits {S.get('exits')}")
    if "gain_on_idle_money" in S:
        print(f"idle money ({S['idle_money']}) earned ${S['gain_on_idle_money']:,.0f}: "
              + ", ".join(f"{k} ${v:,.0f} over {S['idle_days'].get(k, 0)} days" for k, v in S["idle_gain_by_fund"].items())
              + (f"; {S['idle_switches']} switches; the rule alone: ${S['idle_rule_only_final_value']:,.0f} "
                 f"({S['idle_rule_only_cagr_pct']:.1f}% a year)" if "idle_switches" in S else ""))
        print(f"QQQ with dividends: ${S['qqq_with_dividends_final_value']:,.0f} ({S['qqq_with_dividends_cagr_pct']:.1f}% a year)")
        if S.get("warning"):
            print("WARNING:", S["warning"])
    print(res["yearly"].round(1).to_string(index=False))
    extra = f"_{s.universe.lower().replace('+', '_')}" + (f"_top{s.top_n_mcap}" if s.top_n_mcap else "") \
        + ("_qqqcash" if s.cash_in_qqq else "") + ("_sellall200" if s.sell_all_below_200 else "") \
        + ("_scale" if s.exit_plan == "scale" else "") + (f"_{s.idle_switch}" if s.idle_switch not in (None, "none") else "") + (f"_rg{a.rev_growth:g}" if a.rev_growth is not None else "") + (f"_pe{a.pe_max:g}" if a.pe_max else "") + (f"_de{a.de_max:g}" if a.de_max else "")
    rules = (f"sr_{a.sr_target}_up{a.min_upside:g}" if bt.sells_at_resistance(s.s) else f"tp{a.tp:g}")
    out = EXPORTS_DIR / "backtests" / f"backtest_{s.strategy}_{S['start']}_{S['end']}_{rules}_stop{a.stop:g}{extra}.xlsx"
    bt.write_excel(out, res)
    print(f"saved: {out.relative_to(EXPORTS_DIR.parent)}")


if __name__ == "__main__":
    main()
