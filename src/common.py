"""Shared helpers: settings, paths, database connections."""
import datetime as dt
import tomllib
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]

with open(ROOT / "config" / "settings.toml", "rb") as _f:
    SETTINGS = tomllib.load(_f)


_SECRETS_FILE = ROOT / "config" / "secrets.toml"
if _SECRETS_FILE.exists():
    with open(_SECRETS_FILE, "rb") as _f:
        SECRETS = tomllib.load(_f)
else:
    SECRETS = {}

# private values live in secrets.toml (never uploaded) and override settings.toml - e.g. [edgar] contact_email
_edgar = SECRETS.get("edgar") or {}
if _edgar.get("contact_email"):
    SETTINGS.setdefault("edgar", {})["contact_email"] = _edgar["contact_email"]


def path(key: str) -> Path:
    return ROOT / SETTINGS["paths"][key]


DATA_DIR = path("data_dir")
RAW_DIR = path("raw_dir")
LOGS_DIR = path("logs_dir")
EXPORTS_DIR = path("exports_dir")
TECH_DB = DATA_DIR / "technicals.duckdb"
FUND_DB = DATA_DIR / "fundamentals.duckdb"

START_DATE = dt.date.fromisoformat(SETTINGS["history"]["start_date"])
OVERLAP_DAYS = int(SETTINGS["history"]["overlap_days"])


def connect(db_path: Path = TECH_DB, read_only: bool = False):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def run(con, sql: str, params=None):
    """Execute one statement and read its whole result (required by DuckDB 2.x)."""
    return con.execute(sql, params or []).fetchall()


def read_tickers(args=None) -> list[str]:
    """Tickers from the command line, otherwise from tickers.txt (# starts a comment)."""
    if args:
        return [a.strip().upper() for a in args if a.strip()]
    lines = path("tickers_file").read_text().splitlines()
    out = []
    for line in lines:
        t = line.split("#")[0].strip().upper()
        if t and t not in out:
            out.append(t)
    return out
