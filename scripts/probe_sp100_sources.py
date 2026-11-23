"""
One-off check: which sources can give the S&P 100's member history? (read-only; saves nothing in the database)

    python scripts/probe_sp100_sources.py

Tries:
  1. iShares S&P 100 ETF (OEF) holdings file - today
  2. the same file for past dates (asOfDate) - month-ends back to 2015
  3. Wikipedia S&P 100 page - today's list and any change table
  4. SEC: OEF's quarterly holdings (Form N-PORT) - series id and how far back
Prints what worked; attach the output.
"""
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from src.collect import membership_sources as ms  # noqa: E402
from src.collect.sec_nport import _headers  # noqa: E402

OEF = ["https://www.ishares.com/us/products/239723/ishares-sp-100-etf/latest-holdings.csv",
       "https://www.ishares.com/us/products/239723/ishares-sp-100-etf/1467271812596.ajax?fileType=csv&fileName=OEF_holdings&dataType=fund"]


def holdings(url):
    text = ms._ishares_text(url, requests.get)
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.replace('"', "").strip().lower().startswith("ticker,"))
    asof = next((ln for ln in lines[:start] if "as of" in ln.lower()), "").replace('"', "").strip()
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip")
    df = df[df.get("Asset Class", "Equity").astype(str).str.strip().eq("Equity")]
    return sorted(df["Ticker"].astype(str).str.strip()), asof


print("1. OEF holdings today")
for u in OEF:
    try:
        t, asof = holdings(u)
        print(f"   OK  {len(t)} stocks ({asof}) from {u[:70]}...")
        break
    except Exception as e:
        print(f"   no  {u[:70]}...: {type(e).__name__}: {str(e)[:100]}")

print("2. OEF holdings on past dates (asOfDate)")
for d in ["20151130", "20161230", "20171229", "20181231", "20190628", "20201231", "20221230", "20250630"]:
    ok = False
    for u in OEF:
        try:
            t, asof = holdings(u + ("&" if "?" in u else "?") + f"asOfDate={d}")
            print(f"   {d}: OK {len(t)} stocks, file says '{asof}', first: {', '.join(t[:5])}")
            ok = True
            break
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:80]}"
    if not ok:
        print(f"   {d}: no ({err})")
    time.sleep(1)

print("3. Wikipedia S&P 100")
comps, changes, notes = ms.wiki_tables(["https://en.wikipedia.org/wiki/S%26P_100"], "sp100_wikipedia")
print(f"   components: {None if comps is None else len(comps)}; change table: "
      f"{'none' if changes is None else f'{len(changes)} rows from {changes.date.min()}'}; {'; '.join(notes)}")

print("4. SEC N-PORT for OEF")
try:
    r = requests.get("https://www.sec.gov/files/company_tickers_mf.json", headers=_headers(), timeout=30)
    r.raise_for_status()
    j = r.json()
    rows = [dict(zip(j["fields"], x)) for x in j["data"]]
    oef = [x for x in rows if x.get("symbol") == "OEF"]
    print(f"   ticker file: {oef}")
    if oef:
        sid = oef[0]["seriesId"]
        url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sid}&type=NPORT-P"
               f"&dateb=&owner=include&count=100&output=atom")
        r = requests.get(url, headers=_headers(), timeout=30)
        print(f"   series {sid} N-PORT list: HTTP {r.status_code}, filings listed: {r.text.count('<entry>')}")
        dates = sorted(set(pd.Series(pd.Series(r.text.split('<filing-date>')[1:]).str[:10])))
        print(f"   filing dates: {dates[:2]} ... {dates[-2:]}" if dates else "   no dates found")
        n = requests.get(f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={sid}&type=N-Q"
                         f"&dateb=&owner=include&count=100&output=atom", headers=_headers(), timeout=30)
        print(f"   older quarterly lists (Form N-Q): {n.text.count('<entry>')} filings")
except Exception as e:
    print(f"   failed: {type(e).__name__}: {str(e)[:150]}")
