"""
Price corrections. A wrong stored price is never edited by hand in the database.

  manual corrections : config/price_corrections.toml (you add them after a review)
  automatic          : added by the audit's tie-break rule (a bad tick in our source)

Both are kept in the price_corrections table and re-applied after every download,
so a re-download can't undo them. The original values stay in the raw files.
"""
import datetime as dt
import tomllib

from src.common import ROOT, run

CORRECTIONS_FILE = ROOT / "config" / "price_corrections.toml"
FIELDS = ("open", "high", "low", "close", "volume")

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_corrections (
    ticker    VARCHAR NOT NULL,
    date      DATE    NOT NULL,
    field     VARCHAR NOT NULL,
    value     DOUBLE  NOT NULL,
    origin    VARCHAR NOT NULL,     -- 'manual' or 'auto'
    source    VARCHAR,              -- where the correct value came from
    note      VARCHAR,
    added_at  TIMESTAMP,
    PRIMARY KEY (ticker, date, field)
);
"""


def ensure(con):
    con.execute(SCHEMA).fetchall()


def sync_manual(con):
    """Reload the manual corrections from config/price_corrections.toml."""
    ensure(con)
    items = []
    if CORRECTIONS_FILE.exists():
        with open(CORRECTIONS_FILE, "rb") as f:
            items = tomllib.load(f).get("correction", [])
    run(con, "DELETE FROM price_corrections WHERE origin = 'manual'")
    for c in items:
        field = str(c["field"]).lower()
        if field not in FIELDS:
            raise ValueError(f"price_corrections.toml: field must be one of {FIELDS}, got {field!r}")
        run(con, """INSERT OR REPLACE INTO price_corrections
                    VALUES (?, ?, ?, ?, 'manual', ?, ?, current_timestamp)""",
            [c["ticker"].upper(), dt.date.fromisoformat(str(c["date"])), field, float(c["value"]),
             c.get("source", ""), c.get("note", "")])
    return len(items)


def add_auto(con, ticker, date, field, value, source, note):
    ensure(con)
    run(con, """INSERT OR REPLACE INTO price_corrections
                VALUES (?, ?, ?, ?, 'auto', ?, ?, current_timestamp)""",
        [ticker, date, field, float(value), source, note])


def apply(con, ticker):
    """Write every correction for this ticker into prices_daily. Returns how many were applied."""
    ensure(con)
    rows = run(con, "SELECT date, field, value FROM price_corrections WHERE ticker = ?", [ticker])
    for d, field, value in rows:
        if field in FIELDS:
            run(con, f"UPDATE prices_daily SET {field} = ? WHERE ticker = ? AND date = ?", [value, ticker, d])
    return len(rows)


def for_ticker(con, ticker):
    """{(date, field): (value, origin, note)}"""
    ensure(con)
    return {(d, f): (v, o, n) for d, f, v, o, n in
            run(con, "SELECT date, field, value, origin, note FROM price_corrections WHERE ticker = ?", [ticker])}
