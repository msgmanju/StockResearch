"""
Company fundamentals from SEC EDGAR (free, official): the XBRL "company facts" file of each company,
i.e. every number it reported in its 10-K / 10-Q (or 20-F / 40-F) filings with the date it was filed.

Files kept untouched under data/raw/sec/:
    company_tickers_<time>.json        SEC's current ticker -> CIK list
    cik-lookup-data_<time>.txt         every company name ever registered with the SEC -> CIK
    companyfacts/CIK##########_<time>.json.gz
SEC asks for at most 10 requests per second and a contact email (config/settings.toml [edgar]).
"""
import datetime as dt
import gzip
import json
import re
import time

import requests

from src.common import RAW_DIR
from src.collect.sec_nport import _headers

SEC_DIR = RAW_DIR / "sec"
FACTS_DIR = SEC_DIR / "companyfacts"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
NAMES_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
PAUSE = 0.15                      # seconds between requests (SEC limit: 10 per second)
FILING_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT", "6-K")
_last = [0.0]


def _get(url, http=requests.get):
    wait = PAUSE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    r = http(url, headers=_headers(), timeout=120)
    return r


def _stamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _latest(pattern, max_age_days=None):
    files = sorted(SEC_DIR.glob(pattern))
    if not files:
        return None
    f = files[-1]
    if max_age_days is not None and time.time() - f.stat().st_mtime > max_age_days * 86400:
        return None
    return f


# ------------------------------------------------------------------ ticker / name lists
def current_tickers(http=requests.get, max_age_days=7):
    """{TICKER: (cik, title)} from SEC's current list (BRK.B is written BRK-B)."""
    f = _latest("company_tickers_*.json", max_age_days)
    if f is None:
        r = _get(TICKERS_URL, http)
        r.raise_for_status()
        SEC_DIR.mkdir(parents=True, exist_ok=True)
        f = SEC_DIR / f"company_tickers_{_stamp()}.json"
        f.write_text(r.text)
    data = json.loads(f.read_text())
    out = {}
    for row in data.values():
        t = str(row["ticker"]).upper().replace(".", "-")
        out.setdefault(t, (int(row["cik_str"]), row["title"]))
    return out


_SUFFIX = re.compile(r"\b(the|and|inc|incorporated|corp|corporation|co|company|companies|ltd|limited|plc|llc|lp|"
                     r"sa|nv|ag|se|n v|s a|holdings?|group|class [a-c]|new|de|del|common stock|ordinary shares?|"
                     r"american depositary shares?|ads|adr|shares?)\b")


def norm_name(name):
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"/[a-z]{2,3}/?", " ", s)                  # EDGAR state tags like /DE/
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    for _ in range(2):
        s = _SUFFIX.sub(" ", " ".join(s.split()))
    s = re.sub(r"\b([a-z]) (?=[a-z]\b)", r"\1", " ".join(s.split()))   # "e i du pont" -> "ei du pont"
    return " ".join(s.split())


def name_candidates(names_index, member_name, limit=5):
    """CIKs whose SEC name equals the member name, else starts with it (or the reverse), normalized."""
    key = norm_name(member_name)
    if not key:
        return []
    exact = names_index.get(key)
    if exact:
        return sorted(exact)[:limit]
    first = key.split()[0]
    out = []
    for other, ciks in names_index.get(("__first__", first), {}).items():
        if (other.startswith(key + " ") or (key.startswith(other + " ") and len(other.split()) >= 2)):
            out += sorted(ciks)
        if len(out) >= limit:
            break
    return list(dict.fromkeys(out))[:limit]


def all_names(http=requests.get, max_age_days=90):
    """{normalized name: {cik, ...}} from SEC's full company-name list (large file, kept 90 days)."""
    f = _latest("cik-lookup-data_*.txt", max_age_days)
    if f is None:
        r = _get(NAMES_URL, http)
        r.raise_for_status()
        SEC_DIR.mkdir(parents=True, exist_ok=True)
        f = SEC_DIR / f"cik-lookup-data_{_stamp()}.txt"
        f.write_bytes(r.content)
    out = {}
    for line in f.read_bytes().decode("latin-1").splitlines():
        m = re.match(r"^(.*):(\d{1,10}):\s*$", line)
        if m:
            key = norm_name(m.group(1))
            if key:
                out.setdefault(key, set()).add(int(m.group(2)))
                out.setdefault(("__first__", key.split()[0]), {}).setdefault(key, set()).add(int(m.group(2)))
    return out


# ------------------------------------------------------------------ company facts
def facts_file(cik):
    files = sorted(FACTS_DIR.glob(f"CIK{cik:010d}_*.json.gz"))
    return files[-1] if files else None


def download_facts(cik, http=requests.get, max_age_days=None):
    """Download (or reuse) a company's facts file. Returns (path or None, note)."""
    f = facts_file(cik)
    if f is not None and (max_age_days is None or time.time() - f.stat().st_mtime < max_age_days * 86400):
        return f, "saved file"
    r = _get(FACTS_URL.format(cik=cik), http)
    if r.status_code == 404:
        return None, "SEC has no XBRL facts for this company"
    r.raise_for_status()
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    f = FACTS_DIR / f"CIK{cik:010d}_{_stamp()}.json.gz"
    with gzip.open(f, "wb") as g:
        g.write(r.content)
    return f, "downloaded"


def load_facts(path):
    with gzip.open(path, "rb") as g:
        return json.loads(g.read())


MAIN_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT")
ANNUAL_FORMS = ("20-F", "40-F")


def filings(facts):
    """{accession: (form, filed date ISO)} for the company's own periodic reports with XBRL data."""
    out = {}
    for tax in (facts.get("facts") or {}).values():
        for concept in tax.values():
            for unit_rows in concept.get("units", {}).values():
                for r in unit_rows:
                    form = str(r.get("form", "")).split("/")[0]
                    acc, filed = r.get("accn"), r.get("filed")
                    if form in MAIN_FORMS and acc and filed:
                        if acc not in out or filed < out[acc][1]:
                            out[acc] = (form, filed)
    return out


def filed_dates(facts, start=None, end=None):
    """Sorted (filed, form) of periodic reports filed in [start, end] (ISO strings; None = open)."""
    return sorted((f, form) for form, f in filings(facts).values()
                  if (start is None or f >= start) and (end is None or f <= end))
