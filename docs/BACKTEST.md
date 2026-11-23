# Portfolio backtest (step 9b)

App page **Backtest**, or `python scripts/backtest.py` (options: `--help`). Code: `src/strategy/backtest.py`.

## Rules (defaults = the user's rules, Nov 2026)
* Start $50,000 on the first trading day of 2017; at most 30 positions.
* Position size = total portfolio value (cash + holdings at the last close) / 30, or the starting
  capital / 30 with "fixed" sizing; never more than the cash available.
* Buy: a NASDAQ-100 member that day, 52-week high / low <= 3, whose LOW touches the v3 buy price
  (0.70 x 52w low + 0.30 x 52w average close, from the 252 sessions before the day). Filled at the buy price,
  or at the open if it opened below. Several on one day: deepest discount (52w low / buy price) first.
  Not bought again while held.
* Sell (checked from the day after the buy; first one hit):
  stop -30% (fixed; or the bear-year stops), target +20%, time exit at the close of the first session on or
  after buy + 365 days, or the last close when the stock's prices end (acquired / delisted). Stops and
  targets fill at their level, or at the open on a gap; both on one day -> the 0.55 crash rule.
* Universe: NDX, SP500 or BOTH (member of either index on the day).
* Fundamental filters (optional): P/E = buy price / TTM diluted EPS (EPS restated to today's share count),
  loss-makers excluded; debt/equity from the latest balance sheet, negative equity excluded. Both from the
  latest SEC report filed BEFORE the buy day. Stocks with no SEC figure for an active filter are skipped
  unless "keep missing" is set (foreign filers without quarterly reports, FRC/SBNY).
* Costs 0.05% of each buy and sell; dividends added to cash on the ex-date.
* Fractional shares (prices are split-adjusted, so whole shares would not match history).
* Benchmark: $50,000 in QQQ on the same start day (price only).

## Verification (2026-11-16, default rules)
Independent re-calculation from prices_daily: cash identity (start + all trade results = final value) to the
cent; all 643 buys re-checked (member, low <= buy price, H/L <= 3, fill price); all 614 sells re-calculated -
0 differences; QQQ benchmark identical.

## Results with the default rules (2017-01-03 to 2026-11-13)
$50,000 -> $114,273 (8.7% a year, worst fall -33.5%) vs QQQ $284,378 (19.3% a year, worst fall -35.6%).
614 trades, 68% winners, average +4.7% over 124 days; on average 70% of the money was invested.

## Universe + filters (2026-11-16, +20% / -30%, 30 positions)
| run | final | per year | worst fall |
|---|---|---|---|
| NDX, no filters | $114,273 | 8.7% | -33.5% |
| BOTH, no filters | $103,285 | 7.5% | -56.4% |
| BOTH, P/E <= 30, D/E <= 1 | $117,722 | 9.0% | -53.8% (Jan-Mar 2020) |
| BOTH, P/E <= 25, D/E <= 0.5 | $99,245 | 7.2% | -53.6% |
| BOTH, P/E <= 40, D/E <= 2 | $99,214 | 7.1% | -53.2% |
Independent check of the P/E <= 30, D/E <= 1 run: all 692 buys were index members that day and within both limits.

## Why the results are weak - diagnosis (2026-11-16, BOTH, P/E <= 30, D/E <= 2, +20% / -30%: $94,712, 6.6%/yr)
No calculation error: cash, buys and sells were re-checked independently. The causes are the rules:
1. The entry has little edge. 2017-2025, a NASDAQ-100/S&P 500 member bought on a day its low touched the buy price
   returned 11.2% on average over the next year, vs 9.5% for any member on any day - and lost more than 30%
   more often (11.7% vs 8.3% of the time). Stocks reach the buy price because they are falling.
2. +20% / -30% needs more than 60% winners to break even; the run had 65%, so each trade makes little
   (+3.6% on average): 432 targets earned $176k, 214 stops lost $130k.
3. A stock stopped out is usually still below its buy price, so it is bought again the next day
   (119 re-buys within 30 days of a stop, 44 stopped again).
4. QQQ (19.3%/yr) is a hard benchmark: it is weighted to the largest tech stocks. The average index member
   made about 9.5%/yr, which is close to what the strategy earns.
5. With both indexes the portfolio is nearly fully invested, so in Feb-Mar 2020 it fell 55%.
Changing the target/stop alone does not fix it (range 3.5% - 6.9%/yr). Optional extras added:
"wait N days after a stop" (90 days: $122,203, 9.4%/yr, worst -48%) and "no buys while QQQ < 200-day
average" (lower drawdown, lower return).
