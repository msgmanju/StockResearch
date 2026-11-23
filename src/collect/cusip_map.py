"""
CUSIP (or ISIN, for foreign companies filed without a CUSIP) -> ticker, for matching ETF holdings (which list CUSIPs, not tickers) to our tickers.

Order used:
  1. config/cusip_overrides.toml   (your manual fixes; always wins)
  2. cusip_map table               (cache of earlier lookups)
  3. OpenFIGI                      (free official mapping service, run by Bloomberg;
                                    no key needed: 10 CUSIPs per request, 25 requests/minute)
Tickers come back in today's form (e.g. META for Facebook's CUSIP), which is how we store prices.
"""
import time
import tomllib

import pandas as pd
import requests

from src.common import ROOT, run
from src.collect.membership_sources import norm_ticker

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
SOURCE = "openfigi_us"          # lookups made before this rule ('openfigi') are refreshed automatically
MISS = "openfigi_us_none"       # OpenFIGI answered 'No identifier found' (retried after retry_missing_days)
# A reply with an error (rate limit, server problem) is never cached, so it is asked again next run.
# Misses cached as plain 'openfigi_us' (before Nov 16 2026) could hide such errors and are asked again once.
OVERRIDES_FILE = ROOT / "config" / "cusip_overrides.toml"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip      VARCHAR PRIMARY KEY,
    ticker     VARCHAR,          -- NULL = not found
    figi_name  VARCHAR,
    source     VARCHAR,          -- 'openfigi_us' or 'override'
    fetched_at TIMESTAMP
);
"""


def overrides():
    if not OVERRIDES_FILE.exists():
        return {}
    with open(OVERRIDES_FILE, "rb") as f:
        return {i["cusip"].upper(): norm_ticker(i["ticker"]) for i in tomllib.load(f).get("cusip", [])}


US_EXCH = {"US", "UN", "UW", "UQ", "UR", "UA", "UP", "UV", "UF"}   # US composite + US exchanges
CURRENCY = ("USD", "EUR", "CHF", "GBP", "CAD", "JPY", "MXN")


def _pick(data):
    """Best match from OpenFIGI's answer: a US listing, equity first. Non-US tickers are not used."""
    if not data:
        return None, None
    us = [d for d in data if d.get("exchCode") in US_EXCH and d.get("ticker")]
    if not us:
        return None, None
    eq = [d for d in us if d.get("marketSector") == "Equity"] or us
    t = norm_ticker(eq[0]["ticker"]).rstrip("*")
    for cur in CURRENCY:                                  # e.g. 'ATVIEUR' -> 'ATVI'
        if t.endswith(cur) and len(t) > len(cur) + 1:
            t = t[: -len(cur)]
    return t, eq[0].get("name")


def _id_type(c):
    """Foreign companies are listed with an ISIN (e.g. IE00BTN1Y115) instead of a CUSIP."""
    return "ID_ISIN" if len(c) == 12 and c[:2].isalpha() else "ID_CUSIP"


def _ask(http, batch, us_only):
    jobs = [dict({"idType": _id_type(c), "idValue": c}, **({"exchCode": "US"} if us_only else {})) for c in batch]
    r = http(OPENFIGI_URL, json=jobs, headers={"Content-Type": "application/json"}, timeout=60)
    if r.status_code == 429:
        time.sleep(60)
        r = http(OPENFIGI_URL, json=jobs, headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def lookup(con, cusips, http=requests.post, pause=2.5, retry_missing_days=30):
    """{cusip: ticker or None}. New CUSIPs are looked up on OpenFIGI and cached."""
    con.execute(SCHEMA).fetchall()
    cusips = sorted({c for c in cusips if c and c != "N/A" and c.strip("0")})
    ov = overrides()
    cached = {c: (t, src, at) for c, t, src, at in
              run(con, "SELECT cusip, ticker, source, fetched_at FROM cusip_map")}
    now = pd.Timestamp.now()
    def stale(c):
        t, src, at = cached[c]
        if t is not None:
            return src != SOURCE
        return src != MISS or (now - pd.Timestamp(at)).days >= retry_missing_days
    todo = [c for c in cusips if c not in ov and (c not in cached or stale(c))]
    n_err = 0
    for i in range(0, len(todo), 10):
        batch = todo[i:i + 10]
        found, answered = {}, set()
        for us_only in (True, False):                      # US listing first, then any (US exchanges only)
            ask = [c for c in batch if c not in found]
            if not ask:
                break
            try:
                answers = _ask(http, ask, us_only)
            except Exception:
                answers = [{"error": "request failed"}] * len(ask)
            for c, ans in zip(ask, answers):
                ans = ans if isinstance(ans, dict) else {"error": "bad reply"}
                if "error" in ans:
                    continue
                answered.add(c)
                t, name = _pick(ans.get("data"))
                if t:
                    found[c] = (t, name)
            time.sleep(pause)
        for c in batch:
            if c not in found and c not in answered:
                n_err += 1                                 # no clean answer: not cached, asked again next run
                cached.pop(c, None)
                continue
            t, name = found.get(c, (None, None))
            src = SOURCE if t else MISS
            run(con, "INSERT OR REPLACE INTO cusip_map VALUES (?, ?, ?, ?, current_timestamp)", [c, t, name, src])
            cached[c] = (t, src, now)
    if n_err:
        print(f"  OpenFIGI gave no clean answer for {n_err} CUSIP(s); they will be asked again next run")
    out = {}
    for c in cusips:
        out[c] = ov[c] if c in ov else (cached.get(c, (None,))[0])
    return out
