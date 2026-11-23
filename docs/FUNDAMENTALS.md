# Fundamentals (step 8) - procedure

Source: SEC EDGAR "company facts" (every figure a company tagged in its 10-K / 10-Q / 20-F / 40-F,
with the date the report was filed). Free and official. Files are kept untouched in `data/raw/sec/`.

## Run order
```
python scripts/load_fundamentals.py      # 1) match stocks to SEC company numbers, download, 2) build tables
python scripts/audit_fundamentals.py     # 3) audit (with the Yahoo cross-check; --offline to skip it)
```
Both resume after Ctrl+C. `load_fundamentals.py --rebuild` recalculates every stock.

## 1. Matching a stock to its SEC company number (CIK)
Candidates, each checked rather than trusted:
1. `config/cik_overrides.toml` (manual - always wins; several entries for one ticker = a chain)
2. the SEC's current ticker list
3. the SEC's list of every company name ever registered (delisted / renamed companies)

A candidate is judged by the periodic reports it actually **filed** between 1 year before the stock
joined the index (or its first price, if later) and the day it left. Counting report periods instead
would be wrong: a new holding company's first 10-K repeats years it never filed.

When one CIK doesn't cover the whole stretch (holding-company reorganization, merger) the predecessor
or successor is chained, each used only for reports filed in its own date range
(e.g. Disney: CIK 1001039 until May 2019, then 1744489).

Status: OK, ANNUAL ONLY (foreign filers with yearly reports only), LOW COVERAGE, NOT FOUND,
NO XBRL (company never filed machine-readable data), NO SEC DATA (e.g. banks that filed with the FDIC).

## 2. Point-in-time tables (data/fundamentals.duckdb)
* `fund_facts` - every version of every tracked figure with its filing date.
* `fund_snapshots` - one row per filing date: the fundamentals as known right after that filing.
  **A snapshot is used from the day after its filing date** (reports often come out after the close).

Standard definitions (src/calculate/fundamentals.py):

| field | definition |
|---|---|
| revenue | net revenue after interest expense (banks) if reported; other banks: net interest income + non-interest income. Otherwise each revenue tag is worked out on its own and the LARGEST TTM is used, after leaving out (a) tags that cover only PART of revenue - less than half of another tag for the same year in the same annual report (lease income, a segment), (b) revenue including excise taxes when the company also reports it excluding them (Philip Morris, Altria), (c) values below operating income. A value reported as 0 or below counts as not reported; two values for one period in one report: the larger. The tag used is in `revenue_tag`, the method in `revenue_ttm_how`. Quarterly growth always compares the SAME tag(s) in both years |
| op_income | `OperatingIncomeLoss` (banks/insurers usually don't report it - left empty) |
| net_income | `NetIncomeLoss` (attributable to the company's shareholders) |
| eps_diluted | `EarningsPerShareDiluted`, else basic; if neither: TTM net income / diluted shares (marked in `eps_source`) |
| equity | `StockholdersEquity` |
| total_debt | long-term debt incl. current part + short-term borrowings / commercial paper; operating leases excluded |

* Quarterly values are the 3-month figures as reported. A missing quarter (usually Q4, which is only
  in the annual report) = annual minus 9 months (or 6 minus 3 months ...). Quarter lengths 77-119 days
  (12/16-week retail quarters included).
* TTM (calculation version 8) = the standard way: when a fiscal year has just ended, the annual figure;
  otherwise last annual + this year-to-date - the same year-to-date a year earlier (both year-to-date
  figures come from the same report, so restatements can't create negative quarters). Only if that is
  impossible: sum of the last 4 quarters, rejected if one quarter is more than half the total.
  Yearly filers (20-F/40-F) use the latest annual figure.
* Spin-offs and sales: when last year's year-to-date figure was later restated DOWN (business moved to
  discontinued operations), last year's annual figure is restated by the same proportion
  ("annual (restated) + year-to-date"), so the TTM is on today's basis (DuPont 2019, J&J/Kenvue 2023).
  Not done when another tag in the same report still gives the old total (a segment tagged as revenue),
  or when the annual report itself already restated that year (Bath & Body Works 2021).
* Company tagging errors removed before calculating: a full-year value in a 10-Q; a "quarter" equal to
  the annual value in the same report (NiSource, Comfort Systems); a 10-K "fourth quarter" that is
  really the whole year with no year given (L3Harris 2025) is used as the year.
* IFRS filers (e.g. AstraZeneca, Ferrovial) are read from the `ifrs-full` tags with the same definitions.
* Values in the company's reporting currency (`currency`); ratios are currency-free, PE is only
  meaningful for USD reporters.
* **Splits:** EPS in a snapshot is in the share count of the snapshot date (quarters before and after a
  split are restated before being added up). To compare with today's split-adjusted prices use
  `eps_today_basis()` / `pe_ratio()` - they divide by every split after the snapshot date. Only real
  splits are used (not the odd spin-off "splits" a price source records).

Known limits (left empty rather than guessed):
* Berkshire Hathaway stopped tagging EPS and share counts in the standard taxonomy after 2015 - no PE.
* Alphabet 2015-2018 tagged EPS per share class only - those snapshots have no EPS.
* SEC's company-facts files for some companies stop before their latest reports (Nov 2026: AEP,
  Duke, Southern, NextEra, Dominion, Con Ed and other utilities, Citigroup, Prologis); those stocks'
  fundamentals end there - the coverage check lists the months without a recent report.
* KKR tags EPS and share counts only in its own (custom) tags, which the SEC company-facts file leaves out - no PE.
* Debt is tagged in many ways; debt/equity is information only. Version 9 (2026-11-16): many companies tag
  debt only in the annual report (Caterpillar, GM) or with convertible-note tags (Datadog, DexCom, DoorDash,
  Moderna, Palo Alto). When the latest balance sheet has no debt tag, the latest earlier one (up to 400 days)
  is used and noted; debt is 0 when the company reports none (never tagged, or last figure 0) and has no
  material interest expense (Arista, Intuitive Surgical, Palantir, Copart ...); otherwise D/E stays empty with
  the reason in `debt_note` (Berkshire and PACCAR tag their debt only in company-specific tags).

## 3. Audit (src/checks/fundamentals_audit.py)
| check | rule |
|---|---|
| raw_vs_db | stored figures = the SEC files read again, compared value by value (a filing may tag two values for one period, e.g. 33,866,000 and a rounded 33,900,000 - the more precise one is used) |
| coverage | share of month-ends in the window with a recent report (period end within 200 days; yearly filers 470); below 90% = REVIEW |
| derived_q4 | on reports whose latest quarter was derived (annual minus 9 months): that quarter > 0 and at most 3x the average of the other three |
| eps_sign | information only: TTM EPS and net income of opposite sign (quarterly EPS summed across a big share-count change) |
| jumps | TTM revenue x2 or x0.5 between two reports = REVIEW (merger/spin-off) |
| yahoo | current members, only when Yahoo's latest quarter equals ours (Yahoo updates on the earnings release, days before the SEC report): TTM revenue within 3% (banks/insurers information only), TTM EPS within 5% (or 5 cents), quarterly revenue growth within 3 points; operating margin and debt/equity information only |

Accepted items go in `config/fundamentals_reviewed.toml` (ticker, check, optional filed dates, note);
they then show as REVIEWED (information) and no longer make the stock NEEDS REVIEW.

Stock status: VERIFIED, VERIFIED - NO CROSS-CHECK (former members), VERIFIED - ANNUAL ONLY,
NEEDS REVIEW, BLOCKED, NO DATA.
