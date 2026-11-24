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
    import zipfile, requests

    local_bundle = ROOT / "data_bundle.zip"
    if local_bundle.exists():
        with st.spinner("Extracting local database bundle..."):
            with zipfile.ZipFile(local_bundle, "r") as z:
                z.extractall(DATA_DIR)
        return

    direct_url = "https://github.com/msgmanju/StockResearch/releases/download/v1.0.0/data_bundle.zip"
    target_zip = DATA_DIR / "temp_bundle.zip"

    # 1. Try direct public download (timeout 120s)
    try:
        with st.spinner("📥 Downloading historical database (~112 MB, one-time setup)..."):
            r = requests.get(direct_url, stream=True, timeout=120)
            if r.status_code == 200:
                with open(target_zip, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                with zipfile.ZipFile(target_zip, "r") as z:
                    z.extractall(DATA_DIR)
                target_zip.unlink(missing_ok=True)
                if TECH_DB.exists() and FUND_DB.exists():
                    return                                 # done (no st.rerun here: it is swallowed by the except)
    except Exception:
        pass

    # 2. If private repo, require GITHUB_TOKEN from secrets
    token = ""
    try:
        token = st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        pass

    if not token:
        st.error(
            "### 🔑 GitHub Token Required\n\n"
            "Your repository is private, so Streamlit Cloud needs permission to download the database bundle.\n\n"
            "**How to fix in 30 seconds:**\n"
            "1. Click the **`⋮` (three dots)** menu in the top-right corner of this page.\n"
            "2. Click **Settings** ➔ **Secrets**.\n"
            "3. Paste the following and click **Save**:\n\n"
            "```toml\n"
            "GITHUB_TOKEN = \"ghp_your_token_from_earlier\"\n"
            "```\n\n"
            "*(Alternatively, go to your GitHub repo settings and change visibility to Public)*."
        )
        st.stop()

    token = str(token).strip().strip("'\"")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Streamlit-App"
    }
    
    release_api = "https://api.github.com/repos/msgmanju/StockResearch/releases/tags/v1.0.0"
    try:
        res = requests.get(release_api, headers=headers, timeout=20)
    except Exception as e:
        st.error(f"Network error connecting to GitHub: {e}")
        st.stop()

    if res.status_code != 200:
        msg = res.json().get("message", res.text) if res.headers.get("content-type", "").startswith("application/json") else res.text
        st.error(f"GitHub API Error ({res.status_code}): {msg}. "
                 "Please ensure your GITHUB_TOKEN has 'repo' scope and release 'v1.0.0' exists.")
        st.stop()

    assets = res.json().get("assets", [])
    asset_url = None
    for a in assets:
        if a.get("name") == "data_bundle.zip":
            asset_url = a.get("url")
            break

    if not asset_url:
        st.error("Release 'v1.0.0' was found, but `data_bundle.zip` is not attached to it yet!\n\n"
                 "Please go to: https://github.com/msgmanju/StockResearch/releases/tag/v1.0.0 and drag `data_bundle.zip` into the release.")
        st.stop()

    down_headers = {
        "Authorization": f"token {token}",
        "Accept": "application/octet-stream",
        "User-Agent": "Streamlit-App"
    }
    try:
        down_res = requests.get(asset_url, headers=down_headers, stream=True, timeout=60)
    except Exception as e:
        st.error(f"Error downloading asset: {e}")
        st.stop()

    if down_res.status_code != 200:
        st.error(f"Asset Download Error ({down_res.status_code}).")
        st.stop()

    with open(target_zip, "wb") as f:
        for chunk in down_res.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    with zipfile.ZipFile(target_zip, "r") as z:
        z.extractall(DATA_DIR)
    target_zip.unlink(missing_ok=True)


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
