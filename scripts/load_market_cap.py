"""
Market cap for the screener (display and sorting only) - see src/collect/market_cap.py.

    python scripts/load_market_cap.py            # rebuild the table from the Yahoo files already saved
    python scripts/load_market_cap.py --fetch    # download today's values for every current member first

The daily update (./start.sh) does the --fetch version once a day.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import TECH_DB, connect  # noqa: E402
from src import update_status as us  # noqa: E402
from src.collect import market_cap as mcap  # noqa: E402


def main(args=None):
    args = sys.argv[1:] if args is None else list(args)
    con = connect(TECH_DB, read_only=True)
    members = us.current_members(con)
    con.close()
    if "--fetch" in args:
        n, failed = mcap.fetch(members)
        print(f"downloaded {n} ticker(s) from Yahoo; {len(failed)} without a usable market cap")
        for f in failed[:30]:
            print("  ", f)
    con = connect(TECH_DB)
    rows = mcap.load_table(con)
    missing = sorted(set(members) - {r[0] for r in con.execute("SELECT ticker FROM market_cap").fetchall()})
    off = mcap.check_prices(con, set(members))
    con.close()
    print(f"market_cap table: {rows} tickers; current members without one: {len(missing)}"
          + (f" ({', '.join(missing[:20])})" if missing else ""))
    for t, ours, y in off:
        print(f"  [INFO] {t}: Yahoo price {y:.2f} vs our close {ours:.2f} at download - check if it persists")
    return missing, off


if __name__ == "__main__":
    main()
