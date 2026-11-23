"""
S&P 100 sources (docs/MEMBERSHIP_PROCEDURE.md, section 'S&P 100').

Wikipedia's S&P 100 page has today's list but no change history, so the history comes from what
the iShares S&P 100 ETF (OEF, SEC series S000004306) actually held:
  * today            iShares OEF holdings file (the same file your screener uses)
  * Sep 2019 onwards OEF's quarterly SEC filings, Form N-PORT (holdings with CUSIPs)
  * 2015 - 2019      OEF's SEC reports: N-Q (Jun 30, Dec 31), annual N-CSR (Mar 31), half-year N-CSRS (Sep 30);
                     holdings by company name
  * fill-in / check  Wikipedia's S&P 100 page as it was at each quarter-end (old page versions)
Every download is kept under data/raw/membership/ and never downloaded twice.
"""
import html as html_lib
import io
import json
import re
import time

import pandas as pd
import requests

from src.common import RAW_DIR
from src.collect import membership_sources as ms
from src.collect.sec_nport import _headers, parse as parse_nport

OEF_CSV = ["https://www.ishares.com/us/products/239723/ishares-sp-100-etf/latest-holdings.csv",
           ("https://www.ishares.com/us/products/239723/ishares-sp-100-etf/"
            "1467271812596.ajax?fileType=csv&fileName=OEF_holdings&dataType=fund")]
SP100_WIKI = ["https://en.wikipedia.org/wiki/S%26P_100"]
WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_TITLE = "S&P 100"
SERIES = "S000004306"          # iShares S&P 100 ETF
TRUST_CIK = 1100663            # iShares Trust
FOLDER = RAW_DIR / "membership"


def oef_current(http=requests.get):
    """Today's S&P 100 from the iShares OEF holdings file: (DataFrame ticker, company), raw path."""
    errors = []
    for url in OEF_CSV:
        try:
            text = ms._ishares_text(url, http)
            lines = text.splitlines()
            start = next((i for i, ln in enumerate(lines)
                          if ln.replace('"', "").replace("\xa0", "").strip().lower().startswith("ticker,")), None)
            if start is None:
                raise ValueError(f"no 'Ticker' header (got: {text[:80]!r})")
            break
        except Exception as e:
            errors.append(f"{url.split('/')[-1][:40]}: {e}")
    else:
        raise ValueError("iShares OEF file not available - " + " | ".join(errors))
    raw = ms._save("sp100_current_ishares", text, "csv")
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df.columns = [str(c).strip() for c in df.columns]
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].astype(str).str.strip().eq("Equity")]
    if "Type" in df.columns:
        df = df[df["Type"].astype(str).str.strip().str.upper().eq("EQUITY")]
    df = df[df["Ticker"].astype(str).str.strip().str.match(r"^[A-Za-z][A-Za-z0-9.\-/ ]*$")]
    out = pd.DataFrame({"ticker": [ms.norm_ticker(t) for t in df["Ticker"]],
                        "company": [ms._clean(x) for x in df.get("Name", "")]})
    return out.drop_duplicates("ticker").reset_index(drop=True), raw


# ------------------------------------------------------------------ SEC filings of the OEF series
def _get(url, http, binary=False):
    r = http(url, headers=_headers(), timeout=60)
    r.raise_for_status()
    time.sleep(0.15)                       # SEC: at most 10 requests a second
    return r.content if binary else r.text


def series_filings(form, http=requests.get):
    """[(filing date, accession)] of the OEF series for one form type (newest first)."""
    url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={SERIES}&type={form}"
           f"&dateb=&owner=include&count=100&output=atom")
    text = _get(url, http)
    out = []
    for entry in text.split("<entry>")[1:]:
        acc = re.search(r"<accession-nu[mn]ber>([\d-]+)</accession-nu[mn]ber>", entry)
        fd = re.search(r"<filing-date>([\d-]+)</filing-date>", entry)
        typ = re.search(r"<filing-type>([^<]+)</filing-type>", entry)
        if acc and fd and (typ is None or typ.group(1).strip().upper().startswith(form.upper())):
            out.append((fd.group(1), acc.group(1)))
    return out


def _cached(name, fetch, valid=lambda data: len(data) > 500):
    """Saved copy if it exists and is valid, else download; an invalid download is not saved."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    f = FOLDER / name
    if f.exists() and valid(f.read_bytes()):
        return f
    data = fetch()
    raw = data if isinstance(data, bytes) else data.encode()
    if not valid(raw):
        raise ValueError(f"unexpected reply for {name}: {raw[:120]!r}")
    f.write_bytes(raw)
    return f


def nport_holdings(http=requests.get):
    """OEF holdings at every quarter-end with an N-PORT filing (the latest filing per period wins)."""
    frames = []
    for fdate, acc in series_filings("NPORT-P", http):
        f = _cached(f"oef_nport_{acc}.xml", lambda a=acc: _get(
            f"https://www.sec.gov/Archives/edgar/data/{TRUST_CIK}/{a.replace('-', '')}/primary_doc.xml", http))
        h = parse_nport(f)
        if len(h):
            frames.append(h.assign(filed=fdate, accession=acc))
    if not frames:
        return pd.DataFrame()
    h = pd.concat(frames, ignore_index=True)
    latest = h.groupby("period")["filed"].transform("max")
    return h[h["filed"] == latest].reset_index(drop=True)


DATE_RX = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)"
                     r"\s+(\d{1,2}),\s+(\d{4})")


def _text(html):
    t = re.sub(r"<[^>]+>", " ", html)
    return " ".join(html_lib.unescape(t).replace("\xa0", " ").replace("®", "").split())


REPORT_FORMS = ("N-Q", "N-CSR", "N-CSRS")       # holdings in Jun/Dec (N-Q), Mar (annual), Sep (half-year)


def report_documents(http=requests.get, first="2015-06-01", last="2019-12-31"):
    """[(form, filing date, accession, path of the main document)] for OEF's shareholder reports and N-Q filings."""
    out = []
    for form in REPORT_FORMS:
        for fdate, acc in series_filings(form, http):
            if not first <= fdate <= last:
                continue
            base = f"https://www.sec.gov/Archives/edgar/data/{TRUST_CIK}/{acc.replace('-', '')}"
            idx = json.loads(_cached(f"oef_nq_{acc}_index.json", lambda b=base: _get(f"{b}/index.json", http),
                                     valid=_is_json).read_text())
            docs = [i for i in idx.get("directory", {}).get("item", [])
                    if i.get("name", "").lower().endswith((".htm", ".html", ".txt"))
                    and "index" not in i.get("name", "").lower()]
            if not docs:
                continue
            main = max(docs, key=lambda i: int(i.get("size") or 0))
            path = _cached(f"oef_nq_{acc}_{main['name']}", lambda b=base, n=main["name"]: _get(f"{b}/{n}", http))
            out.append((form, fdate, acc, path))
    return out


def _is_json(raw):
    try:
        json.loads(raw)
        return True
    except ValueError:
        return False


FUND_RX = re.compile(r"(?i)ishares\s[^.;:]{0,80}?(etf|index fund)")


def parse_nq(path):
    """(period, [holding names]) - see parse_report."""
    period, rows = parse_report(path)
    return period, [n for n, _ in rows]


def parse_report(path):
    """(period date, [(holding name, market value $)]) from the OEF schedule in an SEC report; (None, []) if not found.
    The document holds several iShares funds; a schedule belongs to OEF when the last fund name written
    just before 'Schedule of Investments' is the S&P 100 ETF (continuation pages included)."""
    raw = path.read_text(errors="replace")
    sp = r"(?:\s|&nbsp;|&#160;|\xa0)+"
    parts = re.split(rf"(?i)schedule{sp}of{sp}investments", raw)
    period, names = None, []
    for k in range(1, len(parts)):
        # older reports: 'Schedule of Investments (Unaudited) iSHARES S&P 100 ETF December 31, 2015';
        # newer: 'iShares S&P 100 ETF Schedule of Investments (unaudited) June 30, 2018'
        # or 'Schedule of Investments (unaudited) (continued) December 31, 2018 iShares S&P 100 ETF'
        head = re.sub(r"(?i)^(\s*\((unaudited|continued)\))*", "", _text(parts[k][:1500]))
        head = DATE_RX.sub("", head[:60], count=1) + head[60:] if DATE_RX.match(head.strip()) else head
        head = head[:120]
        m = FUND_RX.match(head.strip())
        funds = [m.group(0)] if m else [x.group(0) for x in FUND_RX.finditer(_text(parts[k - 1][-2500:]))]
        if not funds:
            continue
        fund = funds[-1]
        if not re.search(r"(?i)s\s*&\s*p\s*100\b", fund) or re.search(r"(?i)ex[- ]s\s*&", fund):
            continue
        if period is None:
            m = DATE_RX.search(_text(parts[k][:3000]))
            period = pd.Timestamp(f"{m.group(1)} {m.group(2)} {m.group(3)}").date() if m else None
        try:                                   # lxml only: a part without a table must not fall back to html5lib
            tables = pd.read_html(io.StringIO(parts[k]), flavor="lxml")
        except (ValueError, ImportError):
            tables = []
        for t in tables:
            for row in t.astype(str).itertuples(index=False):
                cells = [c for c in row if isinstance(c, str) and c and c != "nan"]
                if len(cells) < 2:
                    continue
                name = cells[0].strip()
                nums = [c for c in cells[1:] if re.fullmatch(r"[\$\s]*[\d,]+(\.\d+)?[a-z()]*", c.strip())]
                if (len(nums) >= 2 and len(re.findall(r"[A-Za-z]", name)) >= 2 and not re.match(r"[\d.,\s]+%", name)
                        and not re.search(
                        r"(?i)^(total|common stocks|short-term|money market|net assets|other assets|investments|"
                        r"blackrock cash|cash|less:|liabilities|long contracts|short contracts)", name)
                        and not re.search(r"(?i)e-?\s?mini|futures", name)):
                    value = float(re.sub(r"[^\d.]", "", nums[-1]) or 0)          # last number = market value
                    names.append((re.sub(r"\s*\([a-z,]+\)\s*$", "", name).strip(), value))
    out = {}
    for n, v in names:                                  # one row per name (largest value)
        out[n] = max(v, out.get(n, 0))
    return period, list(out.items())


# ------------------------------------------------------------------ Wikipedia page versions (independent check)
_last_wiki = [0.0]


def _wiki_api(params, http):
    """One Wikipedia API call: at most one a second, retried after a pause when Wikipedia says 'too many requests'."""
    for wait in (0, 10, 30, 60):
        time.sleep(max(wait, 1.0 - (time.time() - _last_wiki[0])))
        _last_wiki[0] = time.time()
        r = http(WIKI_API, params=params, headers=ms.WIKI_HEADERS, timeout=30)
        if getattr(r, "status_code", 200) == 429 or not _is_json(r.text.encode()):
            continue
        return r.text
    raise ValueError("Wikipedia API: too many requests - try again later")


def wiki_list_on(day, http=requests.get):
    """Wikipedia's S&P 100 page as it was at the end of `day`: (revision date, {ticker: company}) or (None, None)."""
    q = _cached(f"sp100_wiki_rev_{day}.json", lambda: _wiki_api({
        "action": "query", "prop": "revisions", "titles": WIKI_TITLE, "rvlimit": 1, "rvdir": "older",
        "rvstart": f"{day}T23:59:59Z", "rvprop": "ids|timestamp", "format": "json", "formatversion": 2}, http),
        valid=lambda raw: _is_json(raw) and b'"query"' in raw)
    pages = json.loads(q.read_text()).get("query", {}).get("pages", [])
    revs = pages[0].get("revisions") if pages else None
    if not revs:
        return None, None
    rid, ts = revs[0]["revid"], revs[0]["timestamp"][:10]
    p = _cached(f"sp100_wiki_{rid}.json", lambda: _wiki_api({
        "action": "parse", "oldid": rid, "prop": "text", "format": "json", "formatversion": 2}, http),
        valid=lambda raw: _is_json(raw) and b'"parse"' in raw)
    html = json.loads(p.read_text()).get("parse", {}).get("text", "")
    try:
        comps = ms.parse_components(html)
    except ValueError:
        return ts, None
    return ts, dict(zip(comps["ticker"], comps["company"]))
