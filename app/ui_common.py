"""Shared pieces of the Streamlit app: read-only database connections and small helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from src.common import FUND_DB, TECH_DB, DATA_DIR, connect, run  # noqa: E402
from src.strategy import engine as en  # noqa: E402
from src import membership as mb  # noqa: E402


def _ensure_databases():
    """Ensure databases exist. On Streamlit Cloud, download from GitHub Release if missing."""
    if TECH_DB.exists() and FUND_DB.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import json, urllib.request, zipfile
    
    local_bundle = ROOT / "data_bundle.zip"
    if local_bundle.exists():
        with st.spinner("Extracting local database bundle..."):
            with zipfile.ZipFile(local_bundle, "r") as z:
                z.extractall(DATA_DIR)
        return

    token = st.secrets.get("GITHUB_TOKEN", "")
    release_url = "https://api.github.com/repos/msgmanju/StockResearch/releases/tags/v1.0.0"
    with st.spinner("Downloading database bundle from GitHub (first launch only, ~15-20s)..."):
        try:
            req = urllib.request.Request(release_url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            req.add_header("User-Agent", "Streamlit-App")
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
            asset_url = None
            for a in data.get("assets", []):
                if a.get("name") == "data_bundle.zip":
                    asset_url = a.get("url")
                    break
            if not asset_url:
                st.error("data_bundle.zip asset not found in GitHub release v1.0.0. Please upload it to your repo release.")
                st.stop()
            down_req = urllib.request.Request(asset_url)
            if token:
                down_req.add_header("Authorization", f"Bearer {token}")
            down_req.add_header("Accept", "application/octet-stream")
            down_req.add_header("User-Agent", "Streamlit-App")
            target_zip = DATA_DIR / "temp_bundle.zip"
            with urllib.request.urlopen(down_req) as resp, open(target_zip, "wb") as out_f:
                out_f.write(resp.read())
            with zipfile.ZipFile(target_zip, "r") as z:
                z.extractall(DATA_DIR)
            target_zip.unlink(missing_ok=True)
        except Exception as e:
            st.error(f"Error downloading database: {e}")
            st.stop()


def check_index_built(universe):
    """Warn (and return False) when an index in the chosen universe has no membership history yet."""
    _ensure_databases()
    names = mb.index_names(universe)
    have = {r[0] for r in run(tech(), "SELECT DISTINCT index_name FROM index_membership")}
    missing = [n for n in names if n not in have]
    if missing:
        st.warning(f"No membership history for {', '.join(missing)} yet - run "
                   f"`python scripts/build_membership.py {' '.join(missing)}` first.")
    return not missing


@st.cache_resource
def _tech_base():
    _ensure_databases()
    return connect(TECH_DB, read_only=True)


@st.cache_resource
def _fund_base():
    _ensure_databases()
    return connect(FUND_DB, read_only=True)


def tech():
    """A cursor of the (read-only) price database - one per use, so page reruns don't share state."""
    return _tech_base().cursor()


def fund():
    return _fund_base().cursor()


@st.cache_data
def last_session():
    return run(tech(), "SELECT max(date) FROM prices_daily WHERE ticker = 'QQQ'")[0][0]


@st.cache_data
def first_session():
    return run(tech(), "SELECT min(date) FROM technicals_daily WHERE low_52w IS NOT NULL")[0][0]


@st.cache_data
def all_tickers():
    return [r[0] for r in run(tech(), "SELECT DISTINCT ticker FROM prices_daily WHERE ticker <> 'QQQ' ORDER BY 1")]


@st.cache_data
def strategies():
    import tomllib
    with open(en.STRATEGY_FILE, "rb") as f:
        return {k: v["name"] for k, v in tomllib.load(f).items()}


def pct(v, digits=1):
    return "-" if v is None or v != v else f"{v:+.{digits}f}%"


def regime_banner(reg):
    if not reg:
        st.warning("No QQQ price that day - market regime unknown.")
        return
    text = (f"QQQ is {reg['qqq_drawdown'] * 100:.1f}% below this year's peak - "
            f"{'BEAR' if reg['bear'] else 'normal'} year (rule: {reg['method']}) -> stop -{reg['stop_pct']}%")
    (st.error if reg["bear"] else st.success)(text)


# ------------------------------------------------------------------ live prices (not stored)
@st.cache_data(ttl=60, show_spinner="Getting today's prices from Yahoo ...")
def live_quotes(tickers):
    from src.strategy import live
    return live.quotes(list(tickers))


def live_day():
    from src.strategy import live
    return live.live_day(tech())


@st.cache_data(show_spinner="Working out today's support and resistance for every member (once a day) ...")
def next_levels(tickers, as_of):
    from src.strategy import live
    return live.next_levels(tech(), list(tickers))
