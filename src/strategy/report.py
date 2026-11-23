"""Excel output for the screener and the trade check (formatted like the other exports)."""
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill

PCT = {"eps_growth_1y", "eps_growth_3y", "pct_from_buy", "peak_after_pct", "revenue_growth_q_yoy", "revenue_growth_ttm_yoy", "op_margin_ttm", "qqq_drawdown"}
PCT100 = {"return_pct", "max_return_pct", "max_close_return_pct", "max_drawdown_pct", "return_with_dividends_pct",
          "stop_pct", "take_profit_pct"}
MONEY = {"peg_1y", "peg_3y", "peak_after", "open", "high", "low", "close", "buy_price", "low_52w", "high_52w", "avg_close_52w", "low_6m", "support",
         "resistance", "stop_if_bought", "take_profit_if_bought", "entry_price", "stop_price", "take_profit_price",
         "exit_price", "eps_diluted_ttm", "pe_ttm", "hl_ratio", "discount_score", "debt_to_equity",
         "dividends_per_share", "qqq_close", "qqq_peak"}
BIG = {"revenue_ttm", "market_cap"}
GREEN, RED, BLUE = "FFC6EFCE", "FFFFC7CE", "FFBDD7EE"


def _format(ws, df):
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = PatternFill("solid", fgColor="FF305496")
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B2"
    for i, col in enumerate(df.columns, start=1):
        letter = ws.cell(1, i).column_letter
        ws.column_dimensions[letter].width = max(10, min(30, len(str(col)) + 2))
        fmt = ("0.0%" if col in PCT else "0.00\\%" if col in PCT100 else "#,##0.00" if col in MONEY
               else "#,##0" if col in BIG else "yyyy-mm-dd" if "date" in col or col in ("fund_filed", "period_end",
                                                                                   "time_exit_on_or_after") else None)
        if fmt:
            for row in range(2, len(df) + 2):
                ws.cell(row, i).number_format = fmt


def _colour(ws, df, col, rule):
    if col not in df.columns:
        return
    i = list(df.columns).index(col) + 1
    for r, v in enumerate(df[col], start=2):
        fill = rule(v)
        if fill:
            ws.cell(r, i).fill = PatternFill("solid", fgColor=fill)


def write_screen(path, info, df, checks=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    reg = info.get("regime") or {}
    head = pd.DataFrame({"item": ["strategy", "date asked", "session used", "universe", "members on the day",
                                  "tolerance around buy price", "in range", "QQQ regime (stop)", "regime rule",
                                  "QQQ drawdown from this year's peak", "members without a price that day"],
                         "value": [info["strategy"], str(info["asked_date"]), str(info["date"]), info["universe"],
                                   info["members"], f"+/- {info['tolerance_pct']}%", int(df["in_range"].sum()) if len(df) else 0,
                                   (f"{'BEAR' if reg.get('bear') else 'normal'} (stop -{reg.get('stop_pct')}%)" if reg else "-"),
                                   reg.get("method", "-"),
                                   f"{reg['qqq_drawdown'] * 100:.1f}%" if reg else "-",
                                   ", ".join(info.get("no_price_that_day", [])) or "-"]})
    cols = ["ticker", "market_cap", "close", "buy_price", "pct_from_buy", "peak_after", "peak_after_pct", "peak_after_date", "low_touched_buy", "hl_ratio", "passes_hl_filter",
            "discount_score", "discount_rank", "top_n", "low_52w", "avg_close_52w", "high_52w", "low_6m",
            "support", "support_touches", "resistance", "resistance_touches", "stop_if_bought",
            "take_profit_if_bought", "pe_ttm", "peg_1y", "eps_growth_1y", "peg_3y", "eps_growth_3y", "eps_diluted_ttm", "revenue_ttm", "revenue_growth_q_yoy",
            "revenue_growth_ttm_yoy", "op_margin_ttm", "debt_to_equity", "debt_note", "fund_filed", "period_end", "note"]
    cols = [c for c in cols if c in df.columns]
    inr = df[df["in_range"]][cols] if len(df) else df
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        head.to_excel(xw, sheet_name="summary", index=False)
        xw.sheets["summary"].column_dimensions["A"].width = 36
        xw.sheets["summary"].column_dimensions["B"].width = 60
        sheets = [("in_range", inr), ("all_members", df[cols] if len(df) else df)]
        if checks is not None and len(checks):
            sheets.insert(1, ("trade_checks", checks))
        for name, frame in sheets:
            frame.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            _format(ws, frame)
            _colour(ws, frame, "pct_from_buy", lambda v: GREEN if pd.notna(v) and v <= 0 else None)
            _colour(ws, frame, "passes_hl_filter", lambda v: RED if v is False else None)
            _colour(ws, frame, "top_n", lambda v: BLUE if v is True else None)
            _colour(ws, frame, "return_pct", lambda v: GREEN if pd.notna(v) and v > 0 else RED if pd.notna(v) else None)


def write_range(path, info, rows, checks=None):
    """Screener over a date range: one row per stock-day (or the first day per stock), same columns as the
    single-day screen plus the date, the QQQ regime of that day and the number of days it qualified."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = pd.DataFrame({"item": ["strategy", "universe", "from", "to", "trading days screened",
                                  "tolerance around buy price", "filters", "rows", "stocks", "rows in the table"],
                         "value": [info["strategy"], info["universe"], str(info["asked_date"]), str(info["date"]),
                                   info.get("range_days"), f"+/- {info['tolerance_pct']}%", info.get("filters", "-"),
                                   info.get("rows", "-"), rows["ticker"].nunique() if len(rows) else 0, len(rows)]})
    cols = ["date", "ticker", "days_hit", "regime", "market_cap", "close", "buy_price", "pct_from_buy", "peak_after",
            "peak_after_pct", "peak_after_date", "low_touched_buy", "hl_ratio", "passes_hl_filter", "discount_score",
            "discount_rank", "top_n", "low_52w", "avg_close_52w", "high_52w", "low_6m", "support", "support_touches",
            "resistance", "resistance_touches", "near_52w_pct", "near_which", "stop_if_bought",
            "take_profit_if_bought", "pe_ttm", "peg_1y", "eps_growth_1y", "peg_3y", "eps_growth_3y", "eps_diluted_ttm", "revenue_ttm", "revenue_growth_q_yoy",
            "revenue_growth_ttm_yoy", "op_margin_ttm", "debt_to_equity", "debt_note", "fund_filed", "period_end",
            "note"]
    frame = rows[[c for c in cols if c in rows.columns]]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        head.to_excel(xw, sheet_name="summary", index=False)
        xw.sheets["summary"].column_dimensions["A"].width = 36
        xw.sheets["summary"].column_dimensions["B"].width = 60
        sheets = [("in_range", frame)]
        if checks is not None and len(checks):
            sheets.append(("trade_checks", checks))
        for name, f in sheets:
            f.to_excel(xw, sheet_name=name, index=False)
            _format(xw.sheets[name], f)
            _colour(xw.sheets[name], f, "pct_from_buy", lambda v: GREEN if pd.notna(v) and v <= 0 else None)
            _colour(xw.sheets[name], f, "return_pct",
                    lambda v: GREEN if pd.notna(v) and v > 0 else RED if pd.notna(v) else None)


def write_trade(path, res, held):
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame({"item": list(res.keys()), "value": [str(v) if not isinstance(v, float) else round(v, 4)
                                                              for v in res.values()]})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="summary", index=False)
        xw.sheets["summary"].column_dimensions["A"].width = 28
        xw.sheets["summary"].column_dimensions["B"].width = 60
        held.to_excel(xw, sheet_name="daily", index=False)
        _format(xw.sheets["daily"], held)
