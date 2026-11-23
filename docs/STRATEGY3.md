# Big-Cap Dip Buyer (key bigcap_dip_buyer, was mr_top30 / "strategy 3") - Dip Buyer's rules, largest companies only, idle money in QQQ

Strategy names and keys (Nov 22, 2026): **Dip Buyer** `dip_buyer` (Version 1, was mr_v3), **Support Buyer** `support_buyer` (was sr_v1, "strategy 2"), **Big-Cap Dip Buyer** `bigcap_dip_buyer` (was mr_top30). The old keys still work.

Config: `[bigcap_dip_buyer]` in `config/strategies.toml`. Dip Buyer (`[dip_buyer]`) and Support Buyer (`[support_buyer]`) are unchanged:
with the new options off, their backtests give identical trades and daily values (checked Nov 2026).

## Rules
Everything as Version 1 (buy price 0.70 x 52w low + 0.30 x 52w average close, volatility filter H/L <= 3,
deepest discount first, take-profit / stop / time exit), plus:

| Option | Default (strategy 3) | Where |
|---|---|---|
| Only the N largest companies by market cap among the index members that day | N = 30 | Backtest, Compare, Screener |
| Sell a holding when its company drops out of the top N | off (keep until a normal exit) | Backtest, CLI |
| Money not in stocks held in QQQ | on | Backtest, Compare, CLI |
| Maximum positions (stocks at a time) | 30 | Backtest, Compare, CLI |
| Debt / equity limit, revenue growth limit (12 months or quarter) | off | Backtest, Compare (growth), CLI |

## Market cap on each day (src/calculate/market_cap_history.py)
close x shares known before the day, on today's share basis (like the split-adjusted prices). Shares, first that applies:
1. SEC diluted share count from the latest report filed before the day (period at most 400 days old)
2. TTM net income / TTM diluted EPS of that report
3. Yahoo's share count today (V, BRK-B, KKR, ARES - no SEC share count); constant, so approximate for older days
4. the last SEC count of any age (BIDU stopped tagging it after 2010)
5. the first SEC count filed after the day (foreign IFRS filers such as VOD have SEC data only from 2018)

Foreign ADSs: `config/share_basis.toml` (ordinary shares per ADS, share subdivisions), with sources.
Share classes of one company (GOOG/GOOGL) count as one company in the ranking. Counts more than 5x away from the
company's own median are dropped (GFS FY2021 tagged 505,758 million shares).

Checks (Nov 2026):
* today vs Yahoo's market cap, 509 current members: 95% within 10%, 97% within 20%; larger gaps are recent
  mergers (SEC count catches up with the next 10-Q) or holding-company structures (IBKR, BX) - none near the top 30
* NDX top 30 at the end of 2016, 2018, 2020, 2022, 2024 look right (e.g. 2016: AAPL $637B, BIDU $57B, VOD $68B)
* against the S&P 100 fund's (OEF) holdings at 10 quarter-ends: rank correlation 0.95-0.99, 26-30 of the same top 30;
  the differences are float adjustment (WMT, ORCL, TMUS have large insider holdings) and recent mergers

## Idle money in QQQ
Money not in stocks earns QQQ's daily return (close to close, plus QQQ's dividends when dividends are on). Buys are paid
by selling QQQ at that day's close and sale proceeds buy QQQ at the close; the cost per buy / sell is charged on each
switch. Check: with no trades the portfolio equals QQQ buy & hold minus one switch cost.

## First results (2017-01-03 to 2026-11-13, $50,000, NASDAQ-100, stop -30%, 1-year time exit)
| Run | Growth / year | Worst fall | Average invested in stocks |
|---|---|---|---|
| Version 1, +20% | 8.7% | -33.5% | 70% |
| S3 top 30, +20%, idle money in cash | 4.9% | -17.8% | 24% |
| S3 top 30, +20%, idle money in QQQ | 19.9% | -36.9% | 24% |
| S3 top 30, +100%, idle money in QQQ | 19.1% | -38.2% | 41% |
| S3 top 30, 10 positions, +20%, QQQ | 19.4% | -39.0% | 54% |
| S3 top 30, +20%, QQQ, sell when out of top 30 | 20.0% | -36.4% | 21% |
| QQQ buy & hold (price) / with dividends | 19.3% / 20.1% | -35.6% | |

Most of strategy 3's money sits in QQQ (only 24-54% in stocks on average), so its result is close to QQQ's.

## Option: SGOV while QQQ is below its 200-day average (Nov 22, 2026)
"Money that is not in stocks" (Backtest page, Compare column, CLI `--idle`): cash, QQQ, **QQQ but SGOV while QQQ is
below its 200-day average**, or SGOV always.
* The switch happens at the close of the day QQQ closes below (or back at/above) its 200-day average of closes;
  the cost per buy / sell is charged twice per switch (sell one fund, buy the other).
* SGOV (0-3 month T-bills) started in May 2020; before that BIL (1-3 month T-bills, since 2007) stands in. T-bill
  funds pay their interest as monthly dividends, so their return is (close + dividend) / previous close.
* SGOV and BIL are in tickers.txt, so ./start.sh downloads their prices and dividends. Until then the backtest
  warns and counts those days as cash.
* Checked against a hand calculation on test data: same final value to the cent, same 36 switches (2017-2026).
* Results also show "the parking rule alone (no stocks)" - QQQ/SGOV switching without any stock trades.

## Index (Nov 22, 2026)
The Big-Cap Dip Buyer now picks from the 30 largest companies of the S&P 500 AND the NASDAQ-100 (universe "BOTH"),
not the NASDAQ-100 alone. With 10 stocks, D/E <= 0.6, revenue growth >= 5%, +20% take-profit, idle money in QQQ:
2017-01-03 to 2026-11-13 $50,000 -> $362,741 (22.3% a year, worst fall -36.6%; NASDAQ-100 only: 22.6%, -44.5%).
The D/E <= 0.6 limit keeps out banks and most insurers, so the extra names were mostly V, JNJ, PFE, CVX, DIS, CRM.

    python scripts/backtest.py --strategy bigcap_dip_buyer                       # defaults above, +20% target
    python scripts/backtest.py --strategy bigcap_dip_buyer --tp 100 --positions 10 --largest 20 --de-max 1 --rev-growth 10
    python scripts/backtest.py --strategy bigcap_dip_buyer --qqq-cash no --sell-out-of-top
    python scripts/backtest.py --strategy bigcap_dip_buyer --idle qqq-sgov             # SGOV while QQQ < 200-day avg
