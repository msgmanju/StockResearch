"""
ETF quarterly holdings from SEC Form N-PORT (public, free).
Each filing is downloaded once and kept under data/raw/membership/<etf>_nport_<period>.xml.
"""
import re
import time
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from src.common import RAW_DIR, SETTINGS

ETF_CIKS = {"QQQ": "0001067839",          # Invesco QQQ Trust, Series 1 (NASDAQ-100)
            "SPY": "0000884394"}          # SPDR S&P 500 ETF Trust (S&P 500)
NS = {"n": "http://www.sec.gov/edgar/nport"}


def _headers():
    email = (SETTINGS.get("edgar") or {}).get("contact_email", "").strip()
    if not email:
        raise ValueError("SEC contact email missing: config/settings.toml -> [edgar] contact_email")
    return {"User-Agent": f"StockResearch personal research {email}", "Accept-Encoding": "gzip, deflate"}


def list_filings(etf, http=requests.get):
    """[(period 'YYYY-MM-DD', accession)] for every NPORT-P filing, newest first."""
    cik = ETF_CIKS[etf]
    r = http(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=_headers(), timeout=30)
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    out, seen = [], set()
    for form, acc, period in zip(recent["form"], recent["accessionNumber"], recent["reportDate"]):
        if form.startswith("NPORT-P") and period not in seen:      # first listed = latest (incl. amendments)
            seen.add(period)
            out.append((period, acc))
    return out


def download(etf, period, acc, http=requests.get):
    """Path of the saved filing (downloaded only if not already saved)."""
    folder = RAW_DIR / "membership"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / f"{etf.lower()}_nport_{period}.xml"
    if f.exists() and f.stat().st_size > 1000:
        return f
    cik = int(ETF_CIKS[etf])
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/primary_doc.xml"
    r = http(url, headers=_headers(), timeout=60)
    r.raise_for_status()
    f.write_text(r.text)
    time.sleep(0.2)                       # SEC asks for at most 10 requests a second
    return f


def _txt(el, path):
    x = el.find(path, NS)
    return x.text.strip() if x is not None and x.text else ""


def parse(path):
    """Stock holdings of one filing: period, name, title, cusip, isin, shares, value_usd, price."""
    root = ET.parse(path).getroot()
    period = _txt(root, ".//n:genInfo/n:repPdDate")
    rows = []
    for h in root.iter(f"{{{NS['n']}}}invstOrSec"):
        if _txt(h, "n:assetCat") != "EC" or _txt(h, "n:units") != "NS":
            continue                       # only stocks held as shares (skip cash funds, futures)
        isin = h.find("n:identifiers/n:isin", NS)
        shares, value = float(_txt(h, "n:balance") or 0), float(_txt(h, "n:valUSD") or 0)
        rows.append({"period": pd.Timestamp(period).date(), "name": _txt(h, "n:name"),
                     "title": _txt(h, "n:title"), "cusip": _txt(h, "n:cusip"),
                     "isin": isin.get("value") if isin is not None else "",
                     "shares": shares, "value_usd": value, "price": value / shares if shares else None})
    return pd.DataFrame(rows)


def all_holdings(etf, http=requests.get):
    """Download (if needed) and parse every filing. Returns (holdings DataFrame, list of periods)."""
    frames = []
    filings = list_filings(etf, http=http)
    for period, acc in filings:
        frames.append(parse(download(etf, period, acc, http=http)))
    h = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return h, sorted(p for p, _ in filings)


def norm_cusip(c):
    return re.sub(r"[^0-9A-Z]", "", str(c).upper())


def security_id(cusip, isin):
    """The CUSIP, or the ISIN when the filing has no real CUSIP ('000000000', 'N/A' - foreign companies)."""
    c = norm_cusip(cusip)
    if c and c != "NA" and c.strip("0"):
        return c
    i = norm_cusip(isin)
    return i if i and i.strip("0") else c
