# Versions

> **Strategy names (Nov 22, 2026):** `dip_buyer` = **Dip Buyer** (Version 1, was `mr_v3`), `support_buyer` = **Support Buyer** (was `sr_v1`, "strategy 2"), `bigcap_dip_buyer` = **Big-Cap Dip Buyer** (was `mr_top30`, "strategy 3"). The old keys still work in commands.

## Version 1 - 2026-11-17 (frozen)
Archive: `versions/v1_2026-11-17.zip` (all code, config, docs, scripts, app; no data, no secrets.toml).
To look at or restore it: unzip into a new folder; never over the working project.

What version 1 is:
- Data: S&P 500 + NASDAQ-100 + S&P 100 membership history (all VERIFIED against SPY/QQQ/OEF SEC filings),
  daily prices Nov 2015 on (audited), technicals, support/resistance, SEC fundamentals (v10, D/E fix).
- Strategy `mr_v3` (config/strategies.toml) - NASDAQ-100 Mean-Reversion v3, volatility filter:
  buy = 0.70 x 52w low + 0.30 x 52w avg close (prior 252 sessions); H/L <= 3.0; top 30 by discount;
  TP +100%, stop -18% normal / -30% bear (bear years 2018, 2020, 2022, 2025, 2026); 1-year exit.
- Tools: screener (incl. live intraday), trade check, stock page, portfolio backtest (scripts + app),
  daily update `./start.sh` (step 11).

Rule for later versions: version 1's strategy section `[mr_v3]` and its code paths stay unchanged, so its
results can always be reproduced. A new strategy gets its own section in config/strategies.toml
(e.g. `[strategy2]`) and, where it needs new logic, its own code - selectable next to v1 in the app.

## Strategy 2 (experiment, started 2026-11-17)
`[sr_v1]` in config/strategies.toml - buy at support, sell at resistance. See docs/STRATEGY2.md.
Checked: version 1's screen and backtests (three settings) give exactly the same results as before.
