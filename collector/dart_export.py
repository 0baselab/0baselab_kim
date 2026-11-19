"""구글 시트용 DART CSV 내보내기 (명세서 9.4). 원 단위, 컬럼명 _krw. 기업·분기당 1행."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .timeutil import stamp

FIN_COLS = ["code", "name", "market", "fiscal_year", "quarter", "fs_div",
            "revenue_krw", "op_income_krw", "net_income_krw", "assets_krw", "liabilities_krw", "equity_krw", "ocf_krw",
            "revenue_ttm_krw", "op_income_ttm_krw", "net_income_ttm_krw", "ocf_ttm_krw", "rcept_dt", "calc_note"]
TTM_ITEMS = ("revenue", "op_income", "net_income", "ocf")


def _prev(y: str, q: int) -> tuple[str, int]:
    return (y, q - 1) if q > 1 else (str(int(y) - 1), 4)


def export(con, out_dir: Path, now: datetime) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = con.execute(
        "SELECT q.*, c.stock_code, s.name, s.market FROM dart_fs_quarter q JOIN dart_corp c ON c.corp_code=q.corp_code"
        " LEFT JOIN stock_master s ON s.code=c.stock_code ORDER BY c.stock_code, q.fiscal_year, q.quarter").fetchall()
    by_key = {(r["corp_code"], r["fiscal_year"], r["quarter"], r["fs_div"]): r for r in rows}
    chosen = {}
    for r in rows:  # 기업·분기당 1행: 연결 우선, 없으면 별도
        k = (r["corp_code"], r["fiscal_year"], r["quarter"])
        if k not in chosen or r["fs_div"] == "CFS":
            chosen[k] = r
    out_rows = []
    for (corp, y, q), r in sorted(chosen.items(), key=lambda kv: (kv[1]["stock_code"] or "", kv[0][1], kv[0][2])):
        row = {"code": r["stock_code"], "name": r["name"], "market": r["market"], "fiscal_year": y, "quarter": q,
               "fs_div": r["fs_div"], "rcept_dt": r["rcept_dt"], "calc_note": r["calc_note"]}
        for c in FIN_COLS[6:13]:
            row[c] = r[c]
        # TTM: 같은 구분(연결/별도)의 연속 4개 분기가 모두 있을 때만 (9.3)
        quarters, yy, qq = [], y, q
        for _ in range(4):
            quarters.append(by_key.get((corp, yy, qq, r["fs_div"])))
            yy, qq = _prev(yy, qq)
        for item in TTM_ITEMS:
            vals = [x[f"{item}_krw"] if x is not None else None for x in quarters]
            row[f"{item}_ttm_krw"] = sum(vals) if None not in vals else None
        out_rows.append(row)
    if out_rows:
        keys = sorted({(r["fiscal_year"], r["quarter"]) for r in out_rows})
        period = f"{keys[0][0]}Q{keys[0][1]}-{keys[-1][0]}Q{keys[-1][1]}"
    else:
        period = "none"
    fin = out_dir / f"dart_financials_{period}_{stamp(now)}.csv"
    with fin.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIN_COLS)
        w.writeheader()
        w.writerows(out_rows)
    filings = con.execute(
        "SELECT f.stock_code AS code, COALESCE(s.name, f.corp_name) AS name, f.rcept_dt, f.report_nm, f.rcept_no"
        " FROM dart_filings f LEFT JOIN stock_master s ON s.code=f.stock_code WHERE f.stock_code IS NOT NULL"
        " ORDER BY f.rcept_dt DESC, f.rcept_no DESC").fetchall()
    fp = (f"{filings[-1]['rcept_dt']}-{filings[0]['rcept_dt']}" if filings else "none")
    fil = out_dir / f"dart_filings_{fp}_{stamp(now)}.csv"
    with fil.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "rcept_dt", "report_nm", "rcept_no"])
        for r in filings:
            w.writerow(list(r))
    return [fin, fil]
