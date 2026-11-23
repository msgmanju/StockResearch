# Price Data Audit Procedure

**Status: APPROVED and built** (full audit). The daily audit (B) and monthly re-check (D) will be scheduled with the daily update agent.
**Scope:** daily prices for every stock in the database (S&P 500 + NASDAQ-100, past and present members).
**Goal:** no stock's prices are used in the screener or a backtest until they have passed this audit.

---

## 1. Sources

| Role | Source | Coverage | Limits |
|---|---|---|---|
| Primary (stored) | Yahoo Finance | ~11 years, current tickers | Often has no data for delisted stocks |
| Independent check | Tiingo | Full history, incl. delisted | Free: 50 requests/hour, 1,000/day, 500 tickers/month |
| Recent check | Nasdaq.com | Last ~1 year | Stocks only (not ETFs) |
| Calendar | NYSE official calendar | All trading days and holidays | None |

Yahoo is the stored source. Tiingo and Nasdaq are only used to check it.

---

## 2. When an audit runs

| Event | Audit type | Second source used |
|---|---|---|
| A. Stock downloaded for the first time | **Full audit**: every day, every check | Tiingo (full history) + Nasdaq |
| B. Daily update (new days only) | **Daily audit**: the new days only | Nasdaq (no Tiingo, to save the monthly limit) |
| C. Full reload after a new split or dividend | **Full audit** again | Tiingo + Nasdaq |
| D. Monthly re-check | **Full audit** for half the stocks each month (all stocks every 2 months) | Tiingo + Nasdaq |
| E. A correction is added (section 6) | **Full audit** of that stock | Tiingo + Nasdaq |

---

## 3. The checks and how serious each one is

### Tier 1 – Blocking (the stock cannot be used until fixed)
| Check | Rule |
|---|---|
| Data present | At least one year of prices stored |
| Database = original download | Every stored value equals the saved raw file |
| Price sanity | Low <= open/close <= high, prices > 0 |
| Trading days | Every NYSE trading day present, no extra days |
| Repeated days | No day identical to the day before |
| Split days | No jump on a split day (prices really are split-adjusted) |
| Splits match Tiingo | Same split dates and ratios |
| Close matches second source | Within 0.5% on every day compared (always our **close**, never adj_close, so a wrong close can't hide behind a correct adjusted price) |

### Tier 2 – Checked automatically; sent for review only if the rule fails
| Check | Automatic rule | If the rule fails |
|---|---|---|
| Big one-day move (>15%) | Confirmed if Tiingo shows the same closes on both days. Single-source stocks: confirmed if the move persists the next day (not a one-day spike) | Review |
| High/low differs from Tiingo (>0.5%) | Accepted if it does **not** change any 52-week high/low or 6-month low on any day | Review, using the tie-break rule below |

**Tie-break rule for a high/low dispute that does matter:** a daily high/low far outside that day's own open and close (more than 5% beyond them) is treated as a bad tick from that source.
- Only the other source has the outlier → our value is kept.
- Only our value is the outlier → it is replaced with the other source's value as an **automatic correction** (section 6), recorded with origin `auto`.
- Neither or both look odd → manual review.

### Tier 3 – Information only (recorded, never blocks)
| Check | Why it doesn't block |
|---|---|
| Opening price differs | Providers define the open differently; the strategy doesn't use it |
| Volume differs | Providers count volume differently; the strategy doesn't use it |
| Dividend amount/date differs | Only affects adj_close; the strategy uses close/high/low (split-adjusted) |
| adj_close vs recalculation | Same reason |

---

## 4. Result for each stock

Each audit gives the stock one status, stored in a `stock_status` table:

| Status | Meaning | Used in screener/backtests? |
|---|---|---|
| **VERIFIED** | All Tier 1 pass; all Tier 2 resolved | Yes |
| **VERIFIED – SINGLE SOURCE** | Passed, but only one price source exists (e.g. delisted stock priced from Tiingo only) | Yes, flagged |
| **NEEDS REVIEW** | Tier 1 pass, but a Tier 2 item could not be resolved automatically | No, until reviewed |
| **BLOCKED** | A Tier 1 check failed | No |
| **NO DATA** | No source has prices | No |

The screen and backtest results will show how many index members were excluded and why.

---

## 5. Manual review (only for NEEDS REVIEW items)

1. Open `exports/audit/<TICKER>_price_audit.xlsx`, **issues** sheet.
2. For each item, look up the date in a third place: the Yahoo Finance website, Nasdaq.com historical data, or a thinkorswim daily chart.
3. Decide:
   - **Our value is right** → add it to `config/audit_reviewed.toml` with a short note.
   - **Our value is wrong** → add a correction to `config/price_corrections.toml` (section 6).
4. Re-run the audit for that stock. Status should become VERIFIED.

---

## 6. Corrections

If a stored price is wrong, it is never edited in the database by hand. Instead:

- Manual corrections go in `config/price_corrections.toml`: ticker, date, field, correct value, where the correct value came from, and a note.
- Automatic corrections (tie-break rule) are stored in the `price_corrections` table with origin `auto`.
- Corrections are applied automatically after every download, so a re-download can't undo them.
- The original Yahoo value stays in the raw files, and every correction is logged.

---

## 7. Delisted stocks

- If Yahoo has no data, prices come from **Tiingo** instead.
- The audit then has only one full-history source, so the stock can reach at most **VERIFIED – SINGLE SOURCE**.
- Tier 1 checks that don't need a second source (sanity, trading days, splits, repeated days) still apply.

### Coverage of index days (all past and current members)
A ticker can be reused by a different company after the original one is delisted, and the index
sometimes keeps one ticker through a merger (e.g. WestRock -> Smurfit Westrock SW). So for every
member, `scripts/load_all_prices.py` checks the stored prices against the trading days the stock was
in either index (from the backtest start):

| Result | Rule | Effect |
|---|---|---|
| OK | prices on at least 99% of member days | goes on to the audit |
| PARTIAL | some member days missing | REVIEW - fixed per case (e.g. the predecessor company's prices) |
| NO DATA | no source covers the member days | stored rows removed (they would be another company's), never used |

If Yahoo covers fewer member days than needed, Tiingo is tried and the source covering more is kept.
Other symbols to try (e.g. the OTC symbol after a bankruptcy) are listed in `config/price_symbols.toml`;
each is kept only if it covers the index days. A stock that stopped trading up to 10 trading days
before the index removed it (a takeover), or a short membership missing 1-2 days, still counts as OK.

**Backtest window:** prices can only affect a backtest from 1 year before a stock first joined either
index (52-week values) to 1 year after it last left (longest hold). Audit items dated outside that
window become information only - e.g. a bankrupt stock trading for pennies years after leaving the
index. The raw-download and data-present checks always count, whatever the date.

**Recognised automatically (information only), added after the first full audit (Nov 2026):**
- *Tiingo filler rows*: after a takeover closes or during a halt, Tiingo repeats the last price with
  zero volume. These are not trades and are removed when prices are loaded.
- *Spin-off adjustments*: a "split" only one source records, whose ratio is not an ordinary split ratio
  (whole numbers, halves, thirds, quarters), with our prices continuous that day - e.g. Yahoo records
  Aptiv's Delphi spin-off as a 1.193 split. Close differences equal to such a factor are not price errors.
- *New listings* with under a year of prices; *missing half-day sessions*; *when-issued gaps* in a
  stock's first weeks before it joined the index.
- *Market-wide days*: a big move on a day when at least a quarter of all stocks moved over 8% the same way.
- One or two missing days (often a trading halt) are REVIEW, not FAIL; missing days around the stock's
  index exit (halted, taken over, delisted) are information only.
- *Copied-forward bars*: a day whose bar repeats the day before with (almost) no volume. In our data
  it is replaced by the second source's bar (an automatic correction, recorded); if the second source
  repeats the previous close instead, ours is kept. Runs of 3+ such days (placeholder history before a
  US listing, e.g. Ferrovial before May 2024) are removed when prices are loaded.
- A bar where only the *open* is outside the day's low-high is information only (the open is not used).

**Free independent check, every stock:** the SPY and QQQ SEC filings give each holding's quarter-end
price. Every stored close is compared with them (16,883 comparisons, Nov 2026). A difference that is
the same factor on every quarter before some date is information only (the source scaled older prices
for a spin-off); any other difference over 0.5% goes to REVIEW.

"Late start" (under a year of prices before joining) is noted only: 52-week values are not available
for those first months, so the stock can't be picked then.

---

## 8. Records kept

| Record | Where |
|---|---|
| Every check result | `audit_runs` table |
| Every issue found | `audit_issues` table |
| Current status per stock | `stock_status` table |
| Readable report per run | `logs/audit/` |
| Excel per stock | `exports/audit/` |
| Confirmed items | `config/audit_reviewed.toml` |
| Corrections | `config/price_corrections.toml` (manual) and `price_corrections` table (all) |

---

## 9. Running it

    python scripts/load_all_prices.py   # 0. first load of all members + coverage check (resumable)
    python scripts/update_prices.py     # 1. download (corrections re-applied automatically)
    python scripts/audit_prices.py      # 2. audit, set each stock's status
    python scripts/calc_technicals.py   # 3. 52-week values, buy price, support/resistance

Always in this order: an automatic correction made by the audit is picked up by step 3.

## 10. Rollout

1. Apply this procedure to the 11 test stocks and confirm the statuses look right.
2. Expand to 50 stocks; review how many items reach manual review.
3. Expand to all S&P 500 + NASDAQ-100 members, current and past, over 2 months (Tiingo's 500-tickers-a-month limit).


## Third source for Yahoo-vs-Tiingo disagreements

`python scripts/third_source_check.py` (add `--dry-run` to only look) takes every open close or
high/low disagreement from the latest audit and asks a third source (Nasdaq.com, then Stooq) for
that day. It compares numbers that don't depend on split/dividend adjustment: the day's return
for a close, high/close or low/close for a high or low (agreement = within 0.25 points).
- Third source agrees with ours: the item is added to `config/audit_reviewed.toml`.
- Third source agrees with Tiingo: our value is corrected (automatic correction; raw files untouched).
- Neither, or no third-source data: left open for review.
Then re-audit the stocks it lists.

## Tie rule (no third source available)
Free third sources only reach back about a year (Nasdaq.com) or block downloads (Stooq), so
older Yahoo-vs-Tiingo disagreements are settled by a fixed rule (settings.toml [audit]),
applied only when a stock has at most `tie_max_items` (5) such disagreements - more than that
points to a systematic problem and stays open:
- high/low: ours is kept if Tiingo's value would move the 52-week high/low or 6-month low by at
  most `tie_minor_pct` (1%). Otherwise the LESS extreme of the two values is kept (the one closer
  to the day's open/close); if that is Tiingo's, ours is corrected automatically.
- close: ours is kept if within 1% of Tiingo. Otherwise the close nearer the previous day's close
  (the smaller move) is kept. Gaps above `bad_tick_pct` (5%) are never settled by the rule.
Every decision is listed in the audit report as "tie rule: ...".

## Per-stock price rules (config/price_symbols.toml)
- `source = "tiingo"`: always take the stock's prices from Tiingo, when Yahoo's history is wrong
  (XRX and DD: Yahoo records spin-offs as odd "splits", which puts a false jump in the history).
- `first_date = ...`: drop prices before this date (AMCR: thin over-the-counter prints before
  its NYSE listing on Jun 11 2019).

## Reviewed date ranges (config/audit_reviewed.toml)
An entry may use `date_from` / `date_to` and a list of checks instead of one `date`; it then covers
every item of those checks in the range (used for the 2023 bank failures FRC, SBNY, SIVB).
