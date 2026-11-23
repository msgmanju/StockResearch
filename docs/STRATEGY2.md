# Strategy 2 - buy at support, sell at a fixed take-profit % (`sr_v1`, experiment, Nov 2026)

> **Strategy names (Nov 22, 2026):** `dip_buyer` = **Dip Buyer** (Version 1, was `mr_v3`), `support_buyer` = **Support Buyer** (was `sr_v1`, "strategy 2"), `bigcap_dip_buyer` = **Big-Cap Dip Buyer** (was `mr_top30`, "strategy 3"). The old keys still work in commands.

Version 1 (`mr_v3`) is unchanged and frozen (docs/VERSIONS.md). Strategy 2 is selectable next to it in the
Screener, Backtest and Compare pages, and with `--strategy sr_v1` in scripts/backtest.py.

Rules (config/strategies.toml [sr_v1]):
- Stocks: same as v1 (NASDAQ-100 by default; any index choice works), same volatility filter (52w H/L <= 3),
  optional P/E and D/E filters as for v1.
- Buy: the day's low touches the day's SUPPORT (sr_daily: nearest support level from the 252 sessions before
  the day - known before the open) -> bought at support, or at the open if it opened below.
- Which first when there are more candidates than free slots (`rank_by`, "Which first" in the app):
    near_52w  support closest to the 52-week low or the 52-week average close (default since 2026-11-18),
              optionally only supports within `near_pct` % of one of them (default 5; 0 = no limit, order only)
    touches   strongest support (most touches) - the first version
  Ties (many supports sit exactly on the 52w low) are broken by ticker, so every run gives the same result.
- Sell: take-profit % from the buy price (choose it in the app, e.g. 20, 50 or 100), stop -30% (or the
  bear-year stops), or the 1-year time exit. 30 positions, equal size, gains reinvested.

Backtest 2017-01-03 to 2026-11-13, $50,000, NASDAQ-100, stop -30%, no P/E or D/E limit:

| Run | CAGR | Final | Worst fall | Trades | Win rate | Avg days held |
|---|---|---|---|---|---|---|
| Version 1, +20% | 8.7% | $114,273 | -33.5% | 614 | 68% | 124 |
| S2, +20% | 13.0% | $167,525 | -28.6% | 601 | 70% | 174 |
| S2, +50% | 15.0% | $197,914 | -31.4% | 357 | 66% | 287 |
| S2, +100% | 12.6% | $161,408 | -28.2% | 326 | 62% | 313 |
| QQQ buy & hold | 19.3% | $284,378 | -35.6% | | | |

First experiment (dropped 2026-11-17): selling at resistance instead of a fixed %. Still available by setting
`sell = "resistance"` in [sr_v1]. With each day's resistance most trades were sold at or below the buy price
within days (an old support becomes the nearest resistance after a dip).

## Ranking by closeness to the 52-week low / average (2026-11-18)

Same period and settings as above (NASDAQ-100, stop -30%):

| Take-profit | Strongest support | Nearest 52w low/avg first | Only within 5% | Only within 10% |
|---|---|---|---|---|
| +20% | 13.0% a year ($167,525) | **15.8% ($213,297)** | 11.6% ($147,967) | 14.7% ($192,916) |
| +50% | **15.0% ($197,914)** | 12.0% ($152,415) | 10.2% ($130,122) | 11.4% ($144,435) |
| +100% | 12.6% ($161,408) | 13.0% ($167,409) | 13.2% ($170,439) | 11.5% ($146,575) |

The 5% filter keeps fewer stocks to choose from but does not improve results. The order of buys changes
results by several % a year (for example, breaking ties a different way moved +20% "nearest first" from
14.5% to 15.8%) - so differences of 1-2% a year between settings are within noise.
