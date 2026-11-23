"""
Write one Excel file per stock to exports/prices/<TICKER>.xlsx (newest day first).
Files are rebuilt on every run, so don't keep your own edits in them.

    python scripts/export_excel.py            # every stock in the database
    python scripts/export_excel.py AAPL       # just these
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from src.common import EXPORTS_DIR, TECH_DB, connect, run  # noqa: E402


def export_prices(tickers=None, db_path=TECH_DB, folder=EXPORTS_DIR / "prices"):
    folder.mkdir(parents=True, exist_ok=True)
    con = connect(db_path, read_only=True)
    if not tickers:
        tickers = [r[0] for r in run(con, "SELECT DISTINCT ticker FROM prices_daily ORDER BY 1")]
    for t in tickers:
        df = con.execute("""SELECT date, open, high, low, close, adj_close, volume, source
                            FROM prices_daily WHERE ticker = ? ORDER BY date DESC""", [t]).df()
        acts = con.execute("""SELECT date, action, value FROM corporate_actions
                              WHERE ticker = ? ORDER BY date DESC""", [t]).df()
        with pd.ExcelWriter(folder / f"{t}.xlsx", engine="openpyxl",
                            date_format="YYYY-MM-DD", datetime_format="YYYY-MM-DD") as xw:
            for name, frame in (("prices", df), ("splits_dividends", acts)):
                frame.to_excel(xw, sheet_name=name, index=False)
                ws = xw.sheets[name]
                ws.freeze_panes = "A2"                       # keep the header row visible
                for col in ws.columns:
                    letter = col[0].column_letter
                    ws.column_dimensions[letter].width = 12 if letter == "A" else 13
                    if col[0].value == "date":
                        for cell in col[1:]:
                            cell.number_format = "yyyy-mm-dd"
                    elif col[0].value in ("open", "high", "low", "close", "adj_close", "value"):
                        for cell in col[1:]:
                            cell.number_format = "0.00"
                    elif col[0].value == "volume":
                        for cell in col[1:]:
                            cell.number_format = "#,##0"
    con.close()
    print(f"Excel files written: {len(tickers)} in {folder}")


if __name__ == "__main__":
    export_prices([a.upper() for a in sys.argv[1:]])
