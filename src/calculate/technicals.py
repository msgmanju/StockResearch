"""
Daily 52-week and 6-month values, stored in technicals.duckdb -> technicals_daily.

Definition (same as your thinkorswim column and stock screener):
  the values for day D use the trading days BEFORE D only (D itself excluded),
  so they are what you could have known at the open of day D.
    low_52w       lowest daily LOW   over the prior 252 trading days
    high_52w      highest daily HIGH over the prior 252 trading days
    avg_close_52w average daily CLOSE over the prior 252 trading days
    low_6m        lowest daily LOW   over the prior 126 trading days
Prices are split-adjusted, not dividend-adjusted (as on thinkorswim charts).
Values stay blank until a full window of history exists (days_52w < 252).
"""

LOOKBACK_52W = 252
LOOKBACK_6M = 126
CALC_VERSION = "v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS technicals_daily (
    ticker         VARCHAR NOT NULL,
    date           DATE    NOT NULL,
    close          DOUBLE,
    low_52w        DOUBLE,
    high_52w       DOUBLE,
    avg_close_52w  DOUBLE,
    low_6m         DOUBLE,
    days_52w       INTEGER,     -- prior days available in the 52-week window (252 = complete)
    days_6m        INTEGER,
    calc_version   VARCHAR,
    calculated_at  TIMESTAMP,
    PRIMARY KEY (ticker, date)
);
"""


def calculate(con, ticker):
    """Rebuild the whole technicals history for one ticker from prices_daily."""
    con.execute(SCHEMA).fetchall()
    con.begin()
    try:
        con.execute("DELETE FROM technicals_daily WHERE ticker = ?", [ticker]).fetchall()
        con.execute(f"""
            INSERT INTO technicals_daily
            WITH w AS (
                SELECT ticker, date, close,
                    min(low)   OVER y AS lo,  max(high) OVER y AS hi,  avg(close) OVER y AS av,
                    count(*)   OVER y AS n52,
                    min(low)   OVER h AS lo6, count(*) OVER h AS n6
                FROM prices_daily
                WHERE ticker = ?
                WINDOW y AS (ORDER BY date ROWS BETWEEN {LOOKBACK_52W} PRECEDING AND 1 PRECEDING),
                       h AS (ORDER BY date ROWS BETWEEN {LOOKBACK_6M} PRECEDING AND 1 PRECEDING)
            )
            SELECT ticker, date, close,
                CASE WHEN n52 = {LOOKBACK_52W} THEN lo END,
                CASE WHEN n52 = {LOOKBACK_52W} THEN hi END,
                CASE WHEN n52 = {LOOKBACK_52W} THEN av END,
                CASE WHEN n6  = {LOOKBACK_6M}  THEN lo6 END,
                n52, n6, '{CALC_VERSION}', current_timestamp
            FROM w
        """, [ticker]).fetchall()
        con.commit()
    except Exception:
        con.rollback()
        raise
    return con.execute("""SELECT count(*), count(low_52w), min(date) FILTER (WHERE low_52w IS NOT NULL),
                                 max(date) FROM technicals_daily WHERE ticker = ?""", [ticker]).fetchall()[0]
