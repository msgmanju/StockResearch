# StockResearch - progress notes (last updated 2026-11-16)

> **Strategy names (Nov 22, 2026):** `dip_buyer` = **Dip Buyer** (Version 1, was `mr_v3`), `support_buyer` = **Support Buyer** (was `sr_v1`, "strategy 2"), `bigcap_dip_buyer` = **Big-Cap Dip Buyer** (was `mr_top30`, "strategy 3"). The old keys still work in commands.

Paste or point Claude at this file to resume. Detailed rules live in the other docs:
MEMBERSHIP_PROCEDURE.md, AUDIT_PROCEDURE.md, FUNDAMENTALS.md, STRATEGY.md.

## Project goal
Local research + backtest system for S&P 500 and NASDAQ-100 members, ~10 years, DuckDB + Streamlit.
Strategy features (do NOT change the original strategy):
- Screener by strategy, date and +/- tolerance around the buy price.
- Trade check: buy at that day's close; stop -18% (-30% in a QQQ bear year); custom take-profit;
  1-year time exit; report max return and its date.
Later: daily update agent, Excel exports, fundamentals from SEC EDGAR.
Working rules: raw-data accuracy first; step by step with audits; keep raw files untouched.

## Steps
| # | Step | Status |
|---|------|--------|
| 1 | Setup | done |
| 2 | Price loading + audit procedure | done |
| 3 | Technicals | done |
| 4 | NASDAQ-100 membership | done (VERIFIED vs QQQ) |
| 5 | S&P 500 membership | done (VERIFIED vs SPY) |
| 6 | Prices for all 741 members + audit | done - 613 VERIFIED, 114 single source (accepted), VNT reviewed; 12 old stocks + WRK have no price data |
| 7 | Technicals / support-resistance for all members | done - 729 PASS, 0 FAIL (`calc_technicals.py --members`) |
| 8 | Fundamentals (SEC EDGAR) | done - 0 blocked, 34 left to look at (30 Yahoo definition differences) |
| 9 | Strategy engine (screener + trade check) | done - user's run matched (2026-11-16) |
| 10 | Streamlit UI | built - `streamlit run app/Home.py`; waiting for the user's first look |
| 11 | Daily agent (launchd) + Excel exports | |

## Step 9 - strategy engine (2026-11-16)
Rules in config/strategies.toml (mr_v3 = NASDAQ-100 Mean-Reversion v3). Bear-year rule chosen by the
user: fixed list of years (2018, 2020, 2022, 2025, 2026) -> -30% stop for any buy in those years.
Trade check buys at the close; stop/target filled at the level or at the open on a gap; same-day
stop+target -> 52w low/avg < 0.55 rule; time exit = first session on/after +365 days.
Verified against an independent calculation (8 trades, 101 buy prices).
User's run matched (10 stocks, +22.0% avg, COST +15.91%). Excel fix: newer pandas needs sheet_name=.

## Step 9b - portfolio backtest (2026-11-16)
User's rules: buy when the low touches the v3 buy price, +20% target, -30% stop always (adjustable),
1-year exit, $50k from 2017, position = total capital / 30. App page Backtest + scripts/backtest.py,
docs/BACKTEST.md. Verified independently (accounting, 643 buys, 614 sells).

## Step 10 - Streamlit app (2026-11-16)
app/Home.py + pages Screener, Trade check, Stock; app/ui_common.py (read-only DB cursors).
Tested with streamlit AppTest on the user's databases (no errors). NEXT: user opens it, then step 11.

## Step 11 - daily update (2026-11-17)
- scripts/daily_update.py + ./start.sh (update, then the app); src/update_status.py (last completed session,
  members behind); start page shows whether the data is current and the last update's result.
- Tested offline with simulated downloads: big move and a missing day flagged, Tiingo-source stock audited
  after its full reload, SEC daily index -> only the filer is refreshed, second run skips everything.
- D/E fix (fundamentals v10) done and verified: 502 of 518 current members have D/E.

## S&P 100 tag (2026-11-17)
- New index `SP100` (src/sp100_build.py, src/sp100_history.py, src/collect/sp100_sources.py): history from
  OEF's SEC quarter-end holdings (2019+), Wikipedia page versions (2015-2019) and today's iShares OEF list;
  change dates by rule; 12 audit checks (docs/MEMBERSHIP_PROCEDURE.md section 8).
- Universe choices everywhere: NDX, SP500, SP100, BOTH (NDX+SP500), NDX+SP100.
- DONE 2026-11-16: user's run VERIFIED - 138 periods, 73 changes, 100-102 members every day, all 44 fund
  lists reproduced, 0 days outside the S&P 500; 46 Wikipedia differences all Wikipedia edit delays.
  Lists: OEF N-PORT 2019+, OEF N-Q/N-CSR/N-CSRS 2015-2019; mid-quarter dates from S&P press releases
  in config/sp100_changes.toml. No prices: DOW-OLD, MON (members until Aug 2017 / Jun 2018).

## Step 8 - where we stopped
Part 1 (SEC company matching) done: 734 OK, 14 annual-only foreign filers, 24 low coverage,
5 not found (LILA, LILAK, MBC, VIP, WRK), FRC/SBNY no SEC data (filed with FDIC).
Overrides in config/cik_overrides.toml are confirmed correct (SEC names checked).

Latest code pushed (2026-11-15, after the first full audit) fixes:
- build crash at ICE (empty-table case) - every stock from ICE on had not been built
- IFRS filers (AZN, CCEP, FER, GFS); bank net revenue first; 0 revenue = not reported (FLS)
- growth compares the same revenue tag; duplicate values -> more precise one
- audit: derived-Q4 checked as known at the time; EPS-sign info only; Yahoo compared only for the
  same latest quarter; bank revenue and operating margin vs Yahoo info only

### NEXT ACTION
```
python scripts/load_fundamentals.py      # rebuilds all 772 stocks (calculation version 8), ~35 min
python scripts/audit_fundamentals.py     # then attach the output
```
Version 6 (2026-11-16): standard annual + year-to-date TTM, largest revenue tag, REIT lease income,
company tagging errors removed, JPM 2015 fixed, 2x faster build, config/fundamentals_reviewed.toml
(JCI 2016 Tyco merger, KKR 2020/2021 accepted). Test on 32 problem stocks: all verified.
Version 7 (2026-11-16): bank revenue = net interest + non-interest income; partial revenue tags left
out (lease income etc.); excise-tax gross revenue left out; spin-off restatements; L3Harris annual fix;
TGT and CDW CIK corrected (were Target Group / WESCO); 24 real events + 6 Yahoo definition differences
in fundamentals_reviewed.toml. Version 8 run (2026-11-16): 34 NEEDS REVIEW - 30 are Yahoo differences (definitions; left visible in
exports/fundamentals/fundamentals_audit.xlsx for the user), MUR/LLL/LM/MAA then accepted.
STEP 8 DONE. Next: step 9 strategy engine.
Version 7 run: 58 NEEDS REVIEW (TGT/CDW fixed). Version 8: two safeguards on the spin-off
restatement (fixed false dips at CHRW, BBWI, HES, MUR, EBAY); 25 more accepted items. Version 6 run: 99 NEEDS REVIEW. Version 3 run had 98 NEEDS REVIEW. Then: review what remains, add accepted items to
fundamentals_reviewed.toml, then move to step 9.

## Price-audit decisions already made (step 6)
- Single-source verification is acceptable.
- Tie rule when Yahoo and Tiingo disagree (no third source): settings.toml [audit] tie_minor_pct=1,
  tie_max_items=5 - keep ours if minor, else the less extreme value; close gaps > 5% stay open.
- XRX and DD prices from Tiingo (Yahoo spin-off factors wrong); AMCR prices start 2019-06-11.
- Bank-failure penny trading (FRC, SBNY, SIVB) and HONA/MNK/SNDK/VNT moves marked real.

## Environment notes
- Mac: ~/Documents/StockResearch with .venv; scripts run there (Yahoo, Tiingo, SEC not reachable
  from Claude's workspace).
- Tiingo paid plan; key in config/secrets.toml (private). SEC contact email in settings.toml [edgar].
- Databases: data/technicals.duckdb (prices, membership, technicals, audits),
  data/fundamentals.duckdb (cik_map, cik_status, fund_facts, fund_snapshots, fund_audit).
- Fundamentals: snapshot used from the day AFTER its filing date; EPS restated across splits;
  use eps_today_basis()/pe_ratio() with split-adjusted prices.
