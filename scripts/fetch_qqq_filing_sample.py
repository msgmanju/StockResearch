"""
Save QQQ's latest quarterly holdings filing (SEC Form N-PORT) so its layout can be
checked before the QQQ cross-check is built. One-time helper.

    python scripts/fetch_qqq_filing_sample.py

Needs the SEC contact email in config/settings.toml -> [edgar] contact_email
(the SEC asks every automated request to identify itself).
Saves data/raw/membership/qqq_nport_<period>.xml and prints a short summary.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from src.common import RAW_DIR, SETTINGS  # noqa: E402

QQQ_CIK = "0001067839"   # Invesco QQQ Trust, Series 1


def main():
    email = (SETTINGS.get("edgar") or {}).get("contact_email", "").strip()
    if not email:
        print("Please put your email in config/settings.toml under [edgar] contact_email first.")
        return
    headers = {"User-Agent": f"StockResearch personal research {email}", "Accept-Encoding": "gzip, deflate"}
    sub = requests.get(f"https://data.sec.gov/submissions/CIK{QQQ_CIK}.json", headers=headers, timeout=30)
    sub.raise_for_status()
    recent = sub.json()["filings"]["recent"]
    rows = [(f, a, p) for f, a, p in zip(recent["form"], recent["accessionNumber"], recent["reportDate"])
            if f.startswith("NPORT-P")]
    print(f"QQQ N-PORT filings listed: {len(rows)}; periods {rows[-1][2] if rows else '-'} to {rows[0][2] if rows else '-'}")
    if not rows:
        return
    form, acc, period = rows[0]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(QQQ_CIK)}/{acc.replace('-', '')}/primary_doc.xml"
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    folder = RAW_DIR / "membership"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"qqq_nport_{period}.xml"
    out.write_text(r.text)
    print(f"saved {out.relative_to(RAW_DIR.parent.parent)} ({len(r.text):,} characters), period {period}")
    print(f"holdings entries: {r.text.count('<invstOrSec>')}, entries with a ticker: {r.text.count('<ticker ')}")


if __name__ == "__main__":
    main()
