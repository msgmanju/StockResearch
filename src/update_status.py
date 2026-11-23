"""
How fresh the databases are (used by scripts/daily_update.py and the app's start page).

The last COMPLETED trading day: today if the NYSE session has ended and the day's prices are out
(after 6 pm New York time), otherwise the session before.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from src import membership as mb
from src.common import run

NY = ZoneInfo("America/New_York")
READY_HOUR = 18            # Yahoo has the day's final prices by then


def expected_session(now=None):
    now = now or dt.datetime.now(NY)
    today = now.astimezone(NY).date()
    days = mb.sessions(today - dt.timedelta(days=12), today)
    if days and days[-1] == today and now.astimezone(NY).hour < READY_HOUR:
        days = days[:-1]
    return days[-1]


def current_members(con):
    return [r[0] for r in run(con, "SELECT DISTINCT ticker FROM index_membership WHERE end_date IS NULL ORDER BY 1")]


def price_status(con, target=None):
    """(target session, newest price date of the current members, list of members behind the target)."""
    target = target or expected_session()
    rows = run(con, """SELECT m.ticker, max(p.date) FROM (SELECT DISTINCT ticker FROM index_membership
                       WHERE end_date IS NULL) m LEFT JOIN prices_daily p USING (ticker) GROUP BY 1""")
    behind = sorted(t for t, d in rows if d is None or d < target)
    newest = max((d for _, d in rows if d is not None), default=None)
    return target, newest, behind


def last_update(con):
    has = run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = 'update_log'")[0][0]
    if not has:
        return None
    r = run(con, """SELECT run_at, target_session, status, summary FROM update_log ORDER BY run_at DESC LIMIT 1""")
    return r[0] if r else None
