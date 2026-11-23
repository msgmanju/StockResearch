"""
Downloads for index membership. Every response is saved untouched under
data/raw/membership/ before it is parsed.

  NASDAQ-100 today      : Nasdaq.com API (same feed your stock screener uses)
  NASDAQ-100 today (2nd): Wikipedia "Nasdaq-100" components table
  NASDAQ-100 changes    : Wikipedia "Nasdaq-100" change table (Date | Added | Removed | Reason)
  S&P 500 today         : iShares IVV holdings file
  S&P 500 today (2nd)   : Wikipedia "List of S&P 500 companies"
  S&P 500 changes       : Wikipedia change table (Effective Date | Added | Removed | Reason)
"""
import datetime as dt
import io
import re

import pandas as pd
import requests

from src.common import RAW_DIR

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
WIKI_HEADERS = {"User-Agent": "StockResearch/1.0 (personal research; python-requests)"}
NDX_API = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
# Tried in order. Wikipedia moved the tables to a separate list page in 2026; the main
# article is kept as a fallback, and any "List of ..." page it links to is followed too.
NDX_WIKI = ["https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",          # components
            "https://en.wikipedia.org/wiki/Historical_components_of_the_Nasdaq-100",  # change history
            "https://en.wikipedia.org/wiki/Nasdaq-100"]
WIKI_BASE = "https://en.wikipedia.org/wiki/"

# S&P 500: today's list from the iShares Core S&P 500 ETF (IVV) holdings file (same provider your
# screener uses for the S&P 100); Wikipedia for a second list and the change history.
IVV_CSV = [  # tried in order; iShares moved the download link in 2026
    "https://www.ishares.com/us/products/239726/ishares-core-s-p-500-etf/latest-holdings.csv",
    ("https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
     "1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"),
]
SP500_WIKI = ["https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
              "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"]


def _save(name, text, ext):
    folder = RAW_DIR / "membership"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{name}_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.{ext}"
    f.write_text(text)
    return f


def norm_ticker(t) -> str:
    """'BRK.B' / 'BRK/B' -> 'BRK-B' (Yahoo style); strips footnote marks and spaces."""
    s = re.sub(r"\[.*?\]", "", str(t)).strip().upper()
    return re.sub(r"\s+", "-", s.replace(".", "-").replace("/", "-"))     # 'BRK B' -> 'BRK-B' 


def _clean(x) -> str:
    s = "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)
    return re.sub(r"\[.*?\]", "", s).strip()


# ---------------------------------------------------------------- Nasdaq.com
def ndx_current_nasdaq(http=requests.get):
    headers = dict(BROWSER, Origin="https://www.nasdaq.com", Referer="https://www.nasdaq.com/")
    r = http(NDX_API, headers=headers, timeout=30)
    r.raise_for_status()
    raw = _save("ndx_current_nasdaq", r.text, "json")
    data = (r.json() or {}).get("data") or {}
    rows = (data.get("data") or {}).get("rows") or data.get("rows") or []
    out = pd.DataFrame([{"ticker": norm_ticker(x["symbol"]), "company": _clean(x.get("companyName"))}
                        for x in rows if x.get("symbol")])
    return out, raw


# ---------------------------------------------------------------- iShares IVV (S&P 500)
def _ishares_text(url, http):
    r = http(url, headers=dict(BROWSER, Accept="text/csv,*/*"), timeout=30)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig", errors="replace") if hasattr(r, "content") else r.text
    if text.lstrip()[:200].lower().startswith(("<!doctype", "<html")) or "<html" in text[:500].lower():
        raise ValueError("got a web page, not the holdings file")
    return text


def sp500_current_ishares(http=requests.get):
    errors = []
    for url in IVV_CSV:
        try:
            text = _ishares_text(url, http)
            lines = text.splitlines()
            start = next((i for i, ln in enumerate(lines)
                          if ln.replace('"', "").replace("\xa0", "").strip().lower().startswith("ticker,")), None)
            if start is None:
                raise ValueError(f"no 'Ticker' header (got: {text[:80]!r})")
            break
        except Exception as e:
            errors.append(f"{url.split('/')[-1][:40]}: {e}")
    else:
        raise ValueError("iShares IVV file not available - " + " | ".join(errors))
    raw = _save("sp500_current_ishares", text, "csv")
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df.columns = [str(c).strip() for c in df.columns]
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip().eq("Equity")]
    if "Type" in df.columns:                       # drops warrants ('SKYD WS'), rights, etc.
        df = df[df["Type"].astype(str).str.strip().str.upper().eq("EQUITY")]
    if "Exchange" in df.columns:                   # leftover claims after a buyout ('HOLX', unlisted at $0.01)
        df = df[~df["Exchange"].astype(str).str.upper().str.startswith("NO MARKET")]
    df = df[df["Ticker"].astype(str).str.strip().str.match(r"^[A-Za-z][A-Za-z0-9.\-/ ]*$")]
    out = pd.DataFrame({"ticker": [norm_ticker(t) for t in df["Ticker"]],
                        "company": [_clean(x) for x in df.get("Name", "")]})
    return out.drop_duplicates("ticker").reset_index(drop=True), raw


SPY_XLSX = ("https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-spy.xlsx")


def sp500_current_ssga(http=requests.get):
    """Backup official list: State Street's daily SPY holdings (Excel)."""
    r = http(SPY_XLSX, headers=dict(BROWSER, Accept="application/vnd.openxmlformats-officedocument."
                                                     "spreadsheetml.sheet,*/*"), timeout=30)
    r.raise_for_status()
    data = r.content
    if data[:2] != b"PK":
        raise ValueError("got a web page, not the SPY holdings file")
    folder = RAW_DIR / "membership"
    folder.mkdir(parents=True, exist_ok=True)
    raw = folder / f"sp500_current_ssga_{dt.datetime.now():%Y%m%d_%H%M%S_%f}.xlsx"
    raw.write_bytes(data)
    grid = pd.read_excel(io.BytesIO(data), header=None)
    hdr = next(i for i in range(min(len(grid), 30))
               if "ticker" in [str(x).strip().lower() for x in grid.iloc[i]])
    df = pd.read_excel(io.BytesIO(data), header=hdr)
    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["Ticker"].astype(str).str.strip().str.match(r"^[A-Za-z][A-Za-z0-9.\-/ ]*$")]
    df = df[~df["Ticker"].astype(str).str.upper().isin(["CASH_USD", "USD", "-"])]
    df = df[~df.get("Name", pd.Series("", index=df.index)).astype(str).str.upper().str.contains("WARRANT|RIGHTS")]
    out = pd.DataFrame({"ticker": [norm_ticker(t) for t in df["Ticker"]],
                        "company": [_clean(x) for x in df.get("Name", "")]})
    return out.drop_duplicates("ticker").reset_index(drop=True), raw


def sp500_current(http=requests.get):
    """Today's S&P 500: iShares IVV file, or State Street's SPY file if iShares is unavailable."""
    try:
        return sp500_current_ishares(http=http)
    except Exception as e1:
        try:
            return sp500_current_ssga(http=http)
        except Exception as e2:
            raise ValueError(f"{e1} || SSGA SPY: {e2}")


def fix_share_class(tickers, reference):
    """Files often drop the share-class dash ('BRKB', 'BFB'); use the reference list's form ('BRK-B')."""
    ref = set(reference)
    out = []
    for t in tickers:
        if t not in ref and len(t) > 2 and f"{t[:-1]}-{t[-1]}" in ref:
            t = f"{t[:-1]}-{t[-1]}"
        out.append(t)
    return out


# ---------------------------------------------------------------- Wikipedia
def wiki_page(url, name, http=requests.get):
    r = http(url, headers=WIKI_HEADERS, timeout=30)
    r.raise_for_status()
    page = re.sub(r"[^A-Za-z0-9_-]", "", url.rsplit("/", 1)[-1])[:60]
    raw = _save(f"{name}_{page}", r.text, "html")
    return r.text, raw


def _flat(cols):
    out = []
    for c in cols:
        parts = [str(p) for p in (c if isinstance(c, tuple) else (c,))]
        parts = [p for i, p in enumerate(parts) if not p.startswith("Unnamed") and (i == 0 or p != parts[i - 1])]
        out.append(" ".join(parts).strip())
    return out


def _cols(t):
    return [c.lower() for c in _flat(t.columns)]


def parse_components(html):
    """The current-components table: a Ticker/Symbol column and roughly 90-520 rows (largest such table)."""
    best = None
    for t in pd.read_html(io.StringIO(html)):
        cols = _cols(t)
        tick = next((i for i, c in enumerate(cols) if c in ("ticker", "symbol", "ticker symbol")), None)
        if tick is None or not (90 <= len(t) <= 520):
            continue
        if best is None or len(t) > len(best[0]):
            best = (t, tick, cols)
    if best is None:
        raise ValueError("no components table found on the Wikipedia page")
    t, tick, cols = best
    comp = next((i for i, c in enumerate(cols) if c in ("company", "security", "company name", "name")), None)
    return pd.DataFrame({"ticker": [norm_ticker(x) for x in t.iloc[:, tick]],
                         "company": [_clean(x) for x in t.iloc[:, comp]] if comp is not None else ""})


def parse_changes(html):
    """
    Change tables: Date | Added (Ticker, Security) | Removed (Ticker, Security) | Reason.
    All matching tables on the page are combined (some pages split them by year).
    Returns one row per added or removed ticker: date, action ('add'/'remove'), ticker, company, reason.
    """
    rows = []
    for t in pd.read_html(io.StringIO(html)):
        cols = _cols(t)
        if not (any("date" in c for c in cols) and any(c.startswith("added") for c in cols)
                and any(c.startswith("removed") for c in cols)):
            continue

        def col(prefix, kinds=None):
            for i, c in enumerate(cols):
                if c.startswith(prefix) and (kinds is None or any(k in c for k in kinds)):
                    return i
            return None
        i_date = next(i for i, c in enumerate(cols) if "date" in c)
        tick_words, name_words = ("ticker", "symbol"), ("security", "company", "name")
        i_at, i_as = col("added", tick_words), col("added", name_words)
        i_rt, i_rs = col("removed", tick_words), col("removed", name_words)
        i_reason = next((i for i, c in enumerate(cols) if "reason" in c), None)
        for _, r in t.iterrows():
            d = pd.to_datetime(_clean(r.iloc[i_date]), errors="coerce")
            if pd.isna(d):
                continue
            reason = _clean(r.iloc[i_reason]) if i_reason is not None else ""
            for action, it, isec in (("add", i_at, i_as), ("remove", i_rt, i_rs)):
                tick = _clean(r.iloc[it]) if it is not None else ""
                if tick and tick.lower() != "nan":
                    rows.append({"date": d.date(), "action": action, "ticker": norm_ticker(tick),
                                 "company": _clean(r.iloc[isec]) if isec is not None else "",
                                 "reason": reason})
    if not rows:
        raise ValueError("no change table (Date / Added / Removed) found on the Wikipedia page")
    return pd.DataFrame(rows).drop_duplicates().sort_values(["date", "action", "ticker"]).reset_index(drop=True)


def _list_links(html):
    """Pages this page points to for components or history: its 'For ..., see X' notes and
    'List of ...' / 'Historical components of ...' links about an index."""
    found = re.findall(r'"2":\{"wt":"([^"]+)"\}', html)
    found += [x for x in re.findall(r'href="(?:https://en\.wikipedia\.org)?/wiki/((?:List_of_|Historical_components_of_)'
                                    r'[^"#?]+)"', html) if re.search(r"nasdaq|s%26p|s&p", x, re.I)]
    out = []
    for x in found:
        if not re.search(r"nasdaq|s%26p|s&p|list of|historical components", x, re.I):
            continue
        url = WIKI_BASE + x.replace(" ", "_")
        if url not in out:
            out.append(url)
    return out


def wiki_tables(urls, name, http=requests.get):
    """
    Try each page (and any 'List of ...' page it links to) until both the components
    table and the change table are found. Each is taken from the first page that has it.
    Returns (components or None, changes or None, notes).
    """
    queue, seen = list(urls), set()
    comps = changes = None
    notes = []
    while queue and (comps is None or changes is None) and len(seen) < 6:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            html, _ = wiki_page(url, name, http=http)
        except Exception as e:
            notes.append(f"{url}: download failed ({type(e).__name__})")
            continue
        page = url.rsplit("/", 1)[-1]
        if comps is None:
            try:
                comps = parse_components(html)
                notes.append(f"components from {page}")
            except ValueError:
                pass
        if changes is None:
            try:
                changes = parse_changes(html)
                notes.append(f"changes from {page} ({len(changes)} rows, from {changes['date'].min()})")
            except ValueError:
                pass
        queue += [u for u in _list_links(html) if u not in seen]
    return comps, changes, notes
