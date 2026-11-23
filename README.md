# StockResearch

Local stock database and backtest tool (S&P 500 + NASDAQ-100, with an S&P 100 tag).

## Every time you open Terminal
    cd ~/Documents/StockResearch
    source .venv/bin/activate

## Daily: update everything, then open the app (step 11)
    ./start.sh             # update to the last completed trading day, then the app
    ./start.sh --quick     # prices + technicals only (skips index lists, SEC fundamentals, market cap)
    python scripts/daily_update.py      # the update alone

- Last completed trading day = today after 6 pm New York time, otherwise the session before.
- Steps: index lists -> prices of every current member (+ QQQ) -> checks on the new days -> technicals
  and support/resistance -> new SEC reports (SEC daily filing index; only companies that filed) ->
  today's screen in exports/screens/. Only what is out of date is done; a second run takes seconds.
- Market cap (Screener size column and sort): Yahoo's market cap of every current member, downloaded once a
  day (raw files in data/raw/yahoo/info/). Rebuild alone: `python scripts/load_market_cap.py --fetch`.
  Display only - no strategy or backtest uses it.
- Result: summary at the end, logs/update/update_<date_time>.txt, and on the app's start page
  (green = up to date; yellow = run ./start.sh).
- Close the app before updating (the update writes to the databases; the app keeps them open).

## Live screen during the trading day
On a trading day the Screener page shows a **Live** switch (on by default while the market is open or until
the evening update): today's prices from Yahoo right now against the buy price from the 252 stored sessions
before today - the same rule as the backtest. **Refresh prices** gets new quotes (they are kept 1 minute).
Nothing live is saved; the database stores today's final prices with the next `./start.sh` after 6 pm New York time.

## Step 1: prices
    python scripts/update_prices.py      # download / update prices for tickers.txt
    python scripts/export_excel.py       # write exports/prices/<TICKER>.xlsx

- Settings: `config/settings.toml`
- Database: `data/technicals.duckdb`
- Original downloads: `data/raw/yahoo/prices/<TICKER>/`
- Excel files in `exports/` are rebuilt on every run; don't keep your own edits there.

## Step 2: price accuracy audit (procedure: docs/AUDIT_PROCEDURE.md)
    pip install -r requirements.txt     # once (adds the NYSE trading calendar)
    python scripts/audit_prices.py      # audit every ticker in tickers.txt, set its status

Normal order: update_prices.py -> audit_prices.py -> calc_technicals.py
- Stock status (VERIFIED / NEEDS REVIEW / BLOCKED ...): `stock_status` table and the summary at the end
- Confirm a reviewed item: `config/audit_reviewed.toml`; fix a wrong price: `config/price_corrections.toml`

- Readable report: `logs/audit/<TICKER>_prices_<date_time>.txt`
- Excel: `exports/audit/<TICKER>_price_audit.xlsx` (summary, issues, side-by-side vs Nasdaq/Stooq)
- Thresholds and sources: `[audit]` section of `config/settings.toml`
- API keys (Tiingo, later GuruFocus): `config/secrets.toml` - keep this file private
- Tiingo free plan: 50 requests/hour, 1,000/day, 500 tickers/month; the audit waits automatically at the hourly limit

## Steps 3-4: 52-week values, buy price, support/resistance
    python scripts/calc_technicals.py   # rebuild technicals for tickers.txt

- Table `technicals_daily` in `data/technicals.duckdb`: 52W low/high/avg close and 6M low for every day,
  using only the trading days BEFORE that day (like thinkorswim `low[1]`).
- Tables `swing_points` and `sr_daily`: support/resistance for every day, same method as the stock screener
  (settings in `[support_resistance]` of `config/settings.toml`)
- Excel: `exports/technicals/<TICKER>.xlsx` (v3 buy price, % from buy, H/L ratio, support, resistance)

## Step 5: index membership (procedure: docs/MEMBERSHIP_PROCEDURE.md)
    python scripts/build_membership.py          # NASDAQ-100, S&P 500 and S&P 100; run daily - saves today's official lists
    python scripts/build_membership.py SP500    # one index only (NDX, SP500 or SP100)
    python scripts/build_membership.py SP100    # S&P 100 (after the S&P 500; see docs/MEMBERSHIP_PROCEDURE.md section 8)
    python scripts/fetch_qqq_filing_sample.py   # one-time: save a QQQ SEC filing (needs [edgar] contact_email)

- Tables `index_membership` (one row per membership period), `index_changes`, `index_snapshots` (daily official lists)
- Report: `logs/audit/membership_<INDEX>_<date_time>.txt`; Excel: `exports/membership/<INDEX>_membership.xlsx`
- Tickers needing prices: `exports/membership/<INDEX>_members_since_backtest_start.txt` and `all_members_since_backtest_start.txt`
- Ticker renames (FB -> META): `config/ticker_changes.toml`
- Official lists: Nasdaq.com (NASDAQ-100), iShares IVV holdings (S&P 500), iShares OEF (S&P 100); history: Wikipedia
  (S&P 100: OEF's SEC quarter-end holdings + Wikipedia page versions)
- Independent check: QQQ's / SPY's quarterly SEC holdings (Form N-PORT, from Sep 2019) - membership on every quarter-end,
  plus each holding's quarter-end price vs our stored close. CUSIPs are turned into tickers with OpenFIGI (cached in
  `cusip_map`); fixes go in `config/cusip_overrides.toml`. Holdings are kept in the `etf_holdings` table.

## Step 9: strategy engine (rules: config/strategies.toml, docs/STRATEGY.md)
    python scripts/screen.py 2022-06-16 --tol 3 --check     # stocks near the buy price + how each trade went
    python scripts/trade_check.py COST 2022-06-16 --tp 40   # one trade, bought at that day's close

## Step 10: the app
    streamlit run app/Home.py           # opens in your browser; Ctrl+C in Terminal to stop

Pages: Screener, Trade check, Stock (chart + fundamentals as known each day), Backtest. Read-only.

    python scripts/backtest.py --tp 20 --stop 30 --capital 50000   # same backtest from Terminal (docs/BACKTEST.md)
