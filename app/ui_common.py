"""Shared pieces of the Streamlit app: read-only database connections and small helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402
from src.common import FUND_DB, TECH_DB, connect, run  # noqa: E402
from src.strategy import engine as en  # noqa: E402
from src import membership as mb  # noqa: E402


def check_index_built(universe):
    """Warn (and return False) when an index in the chosen universe has no membership history yet."""
    names = mb.index_names(universe)
    have = {r[0] for r in run(tech(), "SELECT DISTINCT index_name FROM index_membership")}
    missing = [n for n in names if n not in have]
    if missing:
        st.warning(f"No membership history for {', '.join(missing)} yet - run "
                   f"`python scripts/build_membership.py {' '.join(missing)}` first.")
    return not missing


@st.cache_resource
def _tech_base():
    return connect(TECH_DB, read_only=True)


@st.cache_resource
def _fund_base():
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
