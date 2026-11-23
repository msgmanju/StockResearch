"""
Screener: stocks whose close on a date is within +/- a tolerance of the strategy's buy price.

    python scripts/screen.py 2022-06-16                  # default strategy, +/- 5%
    python scripts/screen.py 2022-06-16 --tol 3
    python scripts/screen.py 2022-06-16 --check          # also run the trade check on every stock in range
    python scripts/screen.py 2022-06-16 --check --tp 50  # ... with your own take-profit (+50%)
    python scripts/screen.py 2022-06-16 --universe SP500 --strategy dip_buyer
    python scripts/screen.py 2022-06-16 --universe SP100

Writes exports/screens/screen_<strategy>_<date>.xlsx. Rules: config/strategies.toml, docs/STRATEGY.md.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from src.common import EXPORTS_DIR, FUND_DB, TECH_DB, connect  # noqa: E402
from src.strategy import engine as en  # noqa: E402
from src.strategy.report import write_screen  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Screen index members against a strategy's buy price")
    ap.add_argument("date")
    ap.add_argument("--tol", type=float, default=5.0, help="+/- percent around the buy price (default 5)")
    ap.add_argument("--strategy", default="dip_buyer", help="dip_buyer, support_buyer or bigcap_dip_buyer")
    ap.add_argument("--universe", default=None, help="NDX, SP500, SP100, BOTH (NDX+SP500) or NDX+SP100 (default: the strategy's)")
    ap.add_argument("--check", action="store_true", help="run the trade check on every stock in range")
    ap.add_argument("--tp", type=float, default=None, help="take-profit percent for --check (default: strategy)")
    a = ap.parse_args(argv)
    tcon, fcon = connect(TECH_DB, read_only=True), connect(FUND_DB, read_only=True)
    info, df = en.screen(a.date, a.strategy, a.tol, a.universe, tcon, fcon)
    reg = info["regime"]
    print(f"{info['strategy']}  |  {info['date']} (asked {info['asked_date']})  |  {info['universe']}: "
          f"{info['members']} members  |  +/- {a.tol}%")
    if reg:
        print(f"QQQ: {'BEAR' if reg['bear'] else 'normal'} market (rule: {reg['method']}) -> stop -{reg['stop_pct']}%;"
              f" QQQ {reg['qqq_drawdown'] * 100:.1f}% below this year's peak")
    if info["no_price_that_day"]:
        print(f"no price that day: {', '.join(info['no_price_that_day'])}")
    inr = df[df["in_range"]] if len(df) else df
    print(f"\n{len(inr)} stock(s) within +/- {a.tol}% of the buy price:")
    if len(inr):
        show = inr[["ticker", "close", "buy_price", "pct_from_buy", "hl_ratio", "passes_hl_filter",
                    "discount_rank", "pe_ttm"]].copy()
        show["pct_from_buy"] = (show["pct_from_buy"] * 100).round(1).astype(str) + "%"
        print(show.round(2).to_string(index=False))
    checks = None
    if a.check and len(inr):
        rows = []
        for t in inr["ticker"]:
            try:
                r, _ = en.trade_check(t, info["date"], a.strategy, a.tp, tcon=tcon)
                rows.append(r)
            except ValueError as e:
                rows.append({"ticker": t, "exit_reason": str(e)})
        checks = pd.DataFrame(rows)
        print("\nTrade check (bought at the close that day):")
        cols = ["ticker", "entry_price", "exit_date", "exit_reason", "return_pct", "max_return_pct", "max_return_date"]
        print(checks[[c for c in cols if c in checks]].round(2).to_string(index=False))
        ok = checks.dropna(subset=["return_pct"])
        if len(ok):
            print(f"\naverage return {ok['return_pct'].mean():.1f}%  |  winners {(ok['return_pct'] > 0).sum()}/{len(ok)}")
    out = EXPORTS_DIR / "screens" / f"screen_{a.strategy}_{info['date']}.xlsx"
    write_screen(out, info, df, checks)
    print(f"\nsaved: {out.relative_to(EXPORTS_DIR.parent)}")


if __name__ == "__main__":
    main()
