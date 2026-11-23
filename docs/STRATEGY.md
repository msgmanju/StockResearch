# Strategy engine (step 9)

> **Strategy names (Nov 22, 2026):** `dip_buyer` = **Dip Buyer** (Version 1, was `mr_v3`), `support_buyer` = **Support Buyer** (was `sr_v1`, "strategy 2"), `bigcap_dip_buyer` = **Big-Cap Dip Buyer** (was `mr_top30`, "strategy 3"). The old keys still work in commands.

Code: `src/strategy/engine.py`. Rules: `config/strategies.toml` (change a number there only if you
change the strategy itself). Scripts: `scripts/screen.py`, `scripts/trade_check.py`.

## Point in time - nothing from the future
* Technicals of day D use the 252 sessions BEFORE D (52-week low, high, average close).
* Fundamentals: the latest SEC report filed before D (used from the day after its filing date).
* Index membership on D (NASDAQ-100 for mr_v3).
* Prices are split-adjusted (not dividend-adjusted, like thinkorswim); dividends are reported separately.

## Screener (`python scripts/screen.py DATE [--tol 5] [--check] [--tp 100]`)
For every index member on DATE (the last trading day on or before it):
| column | rule |
|---|---|
| buy_price | 0.70 x 52-week low + 0.30 x 52-week average close |
| pct_from_buy | close / buy price - 1; **in range** when within +/- tol (default 5%) |
| low_touched_buy | the day's low was at or below the buy price (your entry trigger) |
| hl_ratio, passes_hl_filter | 52-week high / 52-week low; above 3.0 = excluded by the volatility filter |
| discount_score, discount_rank, top_n | 52-week low / buy price, ranked lowest first among stocks passing the filter; top 30 = selected |
| stop_if_bought, take_profit_if_bought | levels if bought at that day's close |
| support / resistance | from step 7 |
| pe_ttm, eps, revenue, growth, margin, debt/equity | fundamentals known on that day |
`--check` runs the trade check on every stock in range. Output: `exports/screens/screen_<strategy>_<date>.xlsx`.

## Trade check (`python scripts/trade_check.py TICKER DATE [--tp 100] [--stop 18] [--hold 365]`)
* Buy at the **close** of DATE.
* Stop: -18%, or -30% in a bear year. Bear years (your choice, Nov 2026): the backtest's list in
  `bear_years` = 2018, 2020, 2022, 2025, 2026 - the whole year, whatever the buy date. Add a year to the
  list when you decide it is a bear year. (Other rules available via `regime_method`: "ytd" = QQQ 12%
  below this year's peak on the buy date; "52w" = QQQ 12% below its 52-week high, your thinkorswim study.)
* Take-profit: +100% by default, or your own with `--tp`.
* From the next session, the first exit hit wins:
  - stop: day's low <= stop -> sold at the stop price (at the open if the stock opened below it)
  - take-profit: day's high >= target -> sold at the target (at the open if it opened above it)
  - both on the same day: stop first if 52-week low / 52-week average < 0.55, otherwise target first
  - time exit: close of the first session on or after DATE + 365 days
  - price data ends (acquired / delisted): last close
* Reports: exit date, price, reason, return, return with dividends, the highest point reached
  (max return = best intraday high, and best close) with its date, and the lowest point with its date.
* Output: `exports/trades/<TICKER>_<DATE>.xlsx` (summary + every day held).

## Verification (Nov 2026)
An independent re-calculation straight from prices_daily (no shared code) gave the same exit date and
price for 8 trades covering every exit type (time exit COST, stop with a gap NFLX/META/MRNA, take-profit
NVDA/TSLA, take-profit on a gap AAPL, delisted ATVI), and the same buy price and 52w high/low ratio for
all 101 NASDAQ-100 members screened on 2022-06-16.
