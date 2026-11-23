"""Table definitions for technicals.duckdb (step 1: prices only)."""

TECHNICALS_SCHEMA = """
-- One row per stock per trading day, exactly as the source reported it.
-- open/high/low/close are split-adjusted (Yahoo's standard); adj_close is
-- also adjusted for dividends.
CREATE TABLE IF NOT EXISTS prices_daily (
    ticker     VARCHAR   NOT NULL,
    date       DATE      NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    adj_close  DOUBLE,
    volume     BIGINT,
    source     VARCHAR   NOT NULL,
    loaded_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Splits and dividends reported by the source (value = split ratio or
-- dividend per share).
CREATE TABLE IF NOT EXISTS corporate_actions (
    ticker     VARCHAR   NOT NULL,
    date       DATE      NOT NULL,
    action     VARCHAR   NOT NULL,      -- 'split' or 'dividend'
    value      DOUBLE,
    source     VARCHAR,
    loaded_at  TIMESTAMP,
    PRIMARY KEY (ticker, date, action)
);

-- Every download attempt, successful or not.
CREATE TABLE IF NOT EXISTS load_log (
    run_id          VARCHAR,
    run_at          TIMESTAMP,
    ticker          VARCHAR,
    dataset         VARCHAR,            -- 'prices'
    source          VARCHAR,
    mode            VARCHAR,            -- 'backfill', 'incremental', 'full reload'
    requested_from  DATE,
    first_date      DATE,
    last_date       DATE,
    rows            INTEGER,
    status          VARCHAR,            -- 'OK', 'EMPTY', 'ERROR'
    message         VARCHAR,
    raw_file        VARCHAR
);
"""


def create_technicals(con):
    con.execute(TECHNICALS_SCHEMA).fetchall()
