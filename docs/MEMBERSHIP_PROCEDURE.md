# Index Membership Procedure (S&P 500 and NASDAQ-100)

**Status: APPROVED.** Being built in stages: NASDAQ-100 first, then S&P 500.
**Goal:** know exactly which stocks were in each index on every trading day since Nov 2015, so that
1. a backtest only picks stocks that were really in the index on that day (no survivorship bias), and
2. we know the full list of current **and past** members whose prices we need.

---

## 1. What gets stored

`index_membership` table in `technicals.duckdb`, one row per membership period:

| Field | Example |
|---|---|
| index | `SP500` or `NDX` |
| ticker | `META` (today's ticker, the one prices are stored under) |
| ticker_at_time | `FB` (the ticker it had while it was in the index, if different) |
| company | Meta Platforms |
| start_date | first trading day in the index |
| end_date | last trading day in the index (blank = still a member) |
| source, note | where the dates came from |

A stock can have several rows (removed and later re-added).
**Date rule:** index changes take effect before the market opens on the effective date, so the stock counts as a member from that date. On its removal date it is no longer a member.

A small `ticker_changes` table links old and new tickers (FB → META, BK → BNY) so history isn't split in two.

---

## 2. Sources

| Role | S&P 500 | NASDAQ-100 |
|---|---|---|
| Today's official list | iShares IVV holdings file (same provider your screener uses for OEF); State Street SPY daily holdings file as backup | Nasdaq.com API (already used by your screener) |
| Change history (main source) | Wikipedia "List of S&P 500 companies" change table | Wikipedia "Nasdaq-100" yearly change lists + Nasdaq announcements |
| **Independent check** | Quarterly holdings of **SPY** (SPDR S&P 500 ETF Trust) filed with the SEC (Form N-PORT) | Quarterly holdings of **QQQ** filed with the SEC (Form N-PORT, Invesco QQQ Trust) |

**Method:** start from today's official list and walk the change history backwards, day by day, to Nov 2015. Every quarter-end is then checked against what the ETF actually held that day.

**Matching holdings to tickers:** the SEC filings identify holdings by CUSIP and name, not ticker. CUSIPs are converted with OpenFIGI (free, run by Bloomberg; results cached), then by company name; anything still unmatched is listed for review and fixed in `config/cusip_overrides.toml`.
Foreign-registered companies (Medtronic, Chubb, Aon, Linde…) are filed with CUSIP `000000000`; for those the ISIN is used instead. Only a clean OpenFIGI answer is cached: a rate-limit or error reply is asked again on the next run. A position under 0.0001% of the fund (e.g. 1 share kept after a stock left the index) is a leftover and is ignored (listed as INFO).
**Coverage:** QQQ's filings in this format start Sep 2019. Quarter-ends between Nov 2016 and Sep 2019 rely on the Wikipedia history plus the daily member-count check.
**Bonus price check:** each holding's value ÷ shares is its closing price on the quarter-end; it must match our stored close (adjusted back for later splits) within 0.5%.

---

## 3. Checks

### Blocking (must pass before the membership data is used)
| Check | Rule |
|---|---|
| Today matches official list | Rebuilt list for today = official list, exactly |
| Quarter-end matches ETF holdings | Every quarter-end since 2019 matches the ETF's SEC filing (allowed: a change dated within 5 trading days of quarter end; each one listed) |
| Member count | S&P 500: 500–506 tickers every day. NASDAQ-100: 100–107 every day (more than 100 when a company has two share classes, e.g. GOOG/GOOGL). A count that is back in range within 3 trading days is allowed (INFO): S&P adds a spin-off a day before the stock it replaces leaves (Fox 2019, Carrier/Otis 2020) |
| No gaps or overlaps | A ticker's periods in one index never overlap; every addition has a matching removal on the same date (one in, one out) unless the record says otherwise |

### Review
| Check | Rule |
|---|---|
| Prices exist | Every member has stored prices on its member days. Missing → listed so we download it (or mark NO DATA if no source has it) |
| Ticker reused | A ticker that belonged to a different company before (an old delisted ticker reused by a new company) → flagged so the right price history is used |

Results go to the same kind of report as the price audit: `logs/audit/membership_<date>.txt` and `exports/audit/membership_audit.xlsx`.

---

## 4. Keeping it current (future-proof)

Membership is **date-based**: every stock has a start and end date in each index, so "members on date D" is always answered for that exact day. It changes every year (NASDAQ-100: December reconstitution plus changes during the year; S&P 500: quarterly plus changes during the year).

To stay correct going forward without depending on any single source:

| Situation | What happens |
|---|---|
| **Every run** (daily, by the daily agent) | Today's official list is saved as a **snapshot** (`index_snapshots`). From now on, our own snapshots are the primary record of changes; Wikipedia is needed only for the past. |
| Official list changed, Wikipedia already shows it | Wikipedia's exact effective date is used. |
| Official list changed, Wikipedia doesn't show it (late/wrong) | The change is recorded from the snapshots with an **approximate date** (first trading day after the previous snapshot) and flagged for review. Once Wikipedia shows it, the exact date replaces the approximate one automatically. |
| Wikipedia shows a change the official lists don't | Flagged for review. |
| Change announced for a future date | Kept as **pending**; applied automatically on its date once the official list shows it. |
| Ticker rename (a company changes symbol) | Flagged as a **possible rename** when the names match; once added to `config/ticker_changes.toml`, history stays under one ticker. |
| A source is down | The run uses the other source and says so; if both are down, nothing is changed. |
| New index (S&P 500, or any other later) | Plugs into the same code: one entry in the index list in `scripts/build_membership.py`. |
| Each quarter | The ETF holdings cross-check (SEC filings) re-checks the latest quarter-end. |

## 5. Outputs

| Output | Use |
|---|---|
| `index_membership` table | Backtests and screener: "members of S&P 500 / NASDAQ-100 / either on date D" |
| `exports/membership/membership_timeline.xlsx` | Every stock with its start and end dates in each index |
| Download list | All current and past members -> fed into the price download and audit |

---

## 6. Decisions (approved)

1. **Two share classes of one company** (GOOG + GOOGL, etc.): **keep both**, as the index does.
2. **A stock removed from the index while held in a backtest:** **keep holding** until one of the strategy's exits (stop, take profit, 1 year). Removal never forces a sale.
3. **Universe:** both indexes are stored. In the web page you choose **S&P 500, NASDAQ-100, or both**.
4. **Renamed companies keep one continuous history under today's ticker** (as Wikipedia does): e.g. FleetCor FLT → Corpay CPAY, Everest Re RE → EG, Under Armour UA/UA.C → UAA/UA (Dec 7 2016), EchoStar SATS → ECHO, WestRock → Smurfit Westrock SW, CBS/ViacomCBS/Paramount → SKYD. Each rename in `config/ticker_changes.toml` and each old CUSIP in `config/cusip_overrides.toml` carries its source.
5. **One ticker, two companies** (the change history doesn't show it): `config/membership_splits.toml` gives the earlier part of the period to the earlier company under its own price ticker - NASDAQ-100 FOXA/FOX before Mar 19 2019 = 21st Century Fox (TFCFA/TFCF); S&P 500 DOW before Sep 2017 = Dow Chemical (DOW-OLD); SW before Jul 8 2024 = WestRock (WRK). A rename is also applied to a removal Wikipedia lists under the old ticker up to 14 days after the rename date.
6. **Spin-offs held by the ETF for a day** but sent by S&P to another index (GRAIL 2024, Ralliant 2025) are marked reviewed in `config/audit_reviewed.toml`, not added to the history.

## 7. Rollout

1. Build NASDAQ-100 first (smaller, ~100 stocks):
   a. Membership from Nasdaq's current list + Wikipedia's change table, with the checks that need no SEC data.
   b. Then the QQQ quarter-end cross-check (SEC filings).
2. Then S&P 500, checked against the S&P 500 ETF filings.
3. Produce the full download list; download and audit prices for all past and current members (Tiingo's 500-tickers-a-month limit means the audit runs over about 2 months).

## 8. S&P 100 (added 2026-11-17, revised after the first real run)

S&P publishes no free change list for the S&P 100 and Wikipedia's page has no change table, so the
history is rebuilt from **lists of members on known days** and every change is dated by rule.
Run after the S&P 500: `python scripts/build_membership.py SP100` (it is checked against the S&P 500).

**Dated lists - the fund's own holdings** (OEF fully replicates the index)

| Period | Source | Kept under data/raw/membership/ |
|---|---|---|
| today | iShares OEF holdings file (each run saves it as a snapshot) | `sp100_current_ishares_*.csv` |
| Sep 2019 onwards, every quarter-end | OEF's SEC Form N-PORT (series S000004306, iShares Trust CIK 1100663); CUSIPs -> tickers | `oef_nport_<accession>.xml` |
| Sep 2015 - Jun 2019, every quarter-end | OEF's SEC reports: N-Q (Jun/Dec), N-CSR (Mar), N-CSRS (Sep); company names -> tickers | `oef_nq_<accession>_*.htm` |

**Wikipedia** (the S&P 100 page as it was at each quarter-end) is the **independent check**, not a source:
the first real run showed it was often edited months late (e.g. Dec 2015 still listed Apache, Baxter, eBay).
It is used as a list only for the build start if no fund report covers it. Page versions are fetched at most
one a second and retried when Wikipedia says "too many requests"; an error reply is never saved.

**Names and tickers.** A list entry is kept only if it is an S&P 500 member that day in our S&P 500 history:
by ticker (following renames, `ticker_at_time`, `membership_splits.toml`, and `[[ticker_alias]]` in
`config/sp100_changes.toml` - UTX -> RTX, PCLN -> BKNG), else by company name (names learnt from the S&P 500
history, OEF's N-PORT holdings and Wikipedia; footnote letters such as 'Inc.a' removed; two share classes
by class letter). Positions under 0.05% of the fund are leftovers, not index stocks (e.g. California Resources, spun off by
Occidental, Mar 2016), and are ignored. A fund report with any other unmatched name is used only as a check
(REVIEW) - add a `[[name_alias]]` to fix it.

**Dating a change between two lists A and B** (`src/sp100_history.py`)
1. **override** - `config/sp100_changes.toml` `[[change]]`: dates confirmed from S&P Dow Jones Indices press
   releases (KHC 2016, NFLX/NVDA 2018, ADBE/DOW/HAL 2019, AMT/CRM 2020 are filled in).
2. **exact** - the stock left the S&P 500 inside (A, B]: it left the S&P 100 the same day. A stock that joined the
   S&P 500 after the quarter's rebalance, or on the day another member left (merger/spin-off: DowDuPont 2017,
   DuPont 2019), joined the S&P 100 that day.
3. **paired** - the single replacement for a stock that left mid-quarter joins the same day (only when there is
   exactly one candidate; checked against S&P's release for NVIDIA/Time Warner, Jun 20 2018); a parent replaced
   by its spin-off (Honeywell -> Honeywell Aerospace 2026) leaves the day the spin-off joins.
4. **rebalance** - otherwise the quarterly rebalance inside (A, B] (first session after the third Friday of
   Mar/Jun/Sep/Dec), S&P's usual S&P 100 change day.
5. **snapshot** - no rebalance inside (A, B]: date B, flagged REVIEW.

When a stock left mid-quarter and its replacement is not certain, the days the count is one short are
reported under `sp100_dates` (REVIEW, with the candidates) instead of failing `member_count`.

**Audit checks** (report `logs/audit/membership_SP100_*.txt`, Excel `exports/membership/SP100_membership.xlsx`)

| Check | Blocks? | What it proves |
|---|---|---|
| official_list | FAIL | today's OEF file was read (100-102 stocks) |
| sources_today | REVIEW | OEF today = Wikipedia today |
| oef_filings / oef_reports | REVIEW | the fund's quarter-end holdings were read and every name matched |
| dated_lists | REVIEW | lists exist with no gap over 100 days; a gap passes when Wikipedia inside it agrees with our history (OEF filed no holdings for Jun 2019: N-Q was retired before N-PORT went public); the months after OEF's latest filing are INFO |
| history_consistent | FAIL | the change walk is consistent |
| change_dates | REVIEW if any 'snapshot' date | how every date was found |
| lists_reproduced | FAIL | the history gives exactly every dated list on its date |
| subset_of_sp500 | FAIL | every S&P 100 member is in the S&P 500 on every day |
| member_count | FAIL | 100-102 members every day (`sp100_min/max` in settings.toml) |
| one_in_one_out | INFO | days where additions and removals differ |
| wikipedia_check | REVIEW | independent: Wikipedia's page at each fund quarter-end and inside every gap between fund lists. A difference is Wikipedia's delay (INFO) when a later page version agrees with us while our history for that stock did not change, or the change was within 10 sessions / after the page version was saved |
| source_mapping | REVIEW | list entries not matched to an S&P 500 member, Wikipedia versions not read |
| prices_exist | INFO | prices are stored for every member since the backtest start |

Confirmed items go in `config/audit_reviewed.toml` as for the other indexes.

In the screener and backtest choose **S&P 100 only** or **NASDAQ-100 + S&P 100** (CLI: `--universe SP100`, `NDX+SP100`).
