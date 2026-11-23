"""
Trade check: buy at the close of a day, then follow the strategy's exits.

    python scripts/trade_check.py COST 2022-06-16              # strategy defaults (+100% target)
    python scripts/trade_check.py COST 2022-06-16 --tp 40      # your own take-profit: +40%
    python scripts/trade_check.py COST 2022-06-16 --stop 25    # override the stop (default from QQQ regime)
    python scripts/trade_check.py COST 2022-06-16 --hold 180   # different time exit (days)

Writes exports/trades/<TICKER>_<date>.xlsx (summary + every day held). Rules: docs/STRATEGY.md.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import EXPORTS_DIR  # noqa: E402
from src.strategy import engine as en  # noqa: E402
from src.strategy.report import write_trade  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check one trade bought at the close of a day")
    ap.add_argument("ticker")
    ap.add_argument("date")
    ap.add_argument("--strategy", default="dip_buyer", help="dip_buyer, support_buyer or bigcap_dip_buyer")
    ap.add_argument("--tp", type=float, default=None, help="take-profit percent (default: strategy, +100)")
    ap.add_argument("--stop", type=float, default=None, help="stop-loss percent (default: 18, or 30 in a bear year)")
    ap.add_argument("--hold", type=int, default=None, help="time exit in calendar days (default 365)")
    a = ap.parse_args(argv)
    try:
        r, held = en.trade_check(a.ticker, a.date, a.strategy, a.tp, a.stop, a.hold)
    except ValueError as e:
        print(f"Cannot check: {e}")
        return 1
    print(f"{r['ticker']}  bought {r['buy_date']} at close {r['entry_price']:.2f}  "
          f"({r['regime_on_buy_date']} market, rule: {r['regime_method']})")
    print(f"  stop      -{r['stop_pct']:.0f}%  = {r['stop_price']:.2f}")
    print(f"  target    +{r['take_profit_pct']:.0f}% = {r['take_profit_price']:.2f}")
    print(f"  time exit on/after {r['time_exit_on_or_after']}")
    print(f"  -> {r['status'].upper()}: {r['exit_reason']} on {r['exit_date']} at {r['exit_price']:.2f}  "
          f"= {r['return_pct']:+.2f}% after {r['days_held']} days"
          f"  (with dividends {r['return_with_dividends_pct']:+.2f}%)")
    if "max_return_pct" in r:
        print(f"  highest:  {r['max_return_pct']:+.2f}% on {r['max_return_date']} (intraday high); "
              f"best close {r['max_close_return_pct']:+.2f}% on {r['max_close_date']}")
        print(f"  lowest:   {r['max_drawdown_pct']:+.2f}% on {r['max_drawdown_date']}")
    extra = "".join(f"_{k}{v:g}" for k, v in (("tp", a.tp), ("stop", a.stop), ("hold", a.hold)) if v is not None)
    out = EXPORTS_DIR / "trades" / f"{r['ticker']}_{r['buy_date']}{extra}.xlsx"
    write_trade(out, r, held)
    print(f"saved: {out.relative_to(EXPORTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
