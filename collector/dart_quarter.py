"""DART 분기 단독 값 환산 (명세서 9.3, 14.4).

| 표 | 분기 단독 값 | 4분기 |
| 손익(IS·CIS, 같은 계정이면 IS) | thstrm_amount | 사업보고서 thstrm_amount - 3분기 thstrm_add_amount |
| 현금흐름(CF) | 이번 thstrm_amount - 직전 보고서 thstrm_amount (1분기는 그대로) | 사업보고서 - 3분기 누적 |
| 재무상태(BS) | 그 시점 잔액 thstrm_amount 그대로 | 같음 |
계정은 표준 account_id로만 연결한다. 연결(CFS)·별도(OFS)는 섞지 않는다.
"""
from __future__ import annotations

from . import db
from .krx_collect import to_int

REPORTS = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
IS_ACCOUNTS = {"revenue": "ifrs-full_Revenue", "op_income": "dart_OperatingIncomeLoss", "net_income": "ifrs-full_ProfitLoss"}
CF_ACCOUNTS = {"ocf": "ifrs-full_CashFlowsFromUsedInOperatingActivities"}
# 재무상태표 표준 ID: 실측 보고서에는 손익·현금흐름만 있었음. 응답에 없으면 빈 칸 + calc_note (확인 필요)
BS_ACCOUNTS = {"assets": "ifrs-full_Assets", "liabilities": "ifrs-full_Liabilities", "equity": "ifrs-full_Equity"}
COLS = ["revenue_krw", "op_income_krw", "net_income_krw", "assets_krw", "liabilities_krw", "equity_krw", "ocf_krw",
        "revenue_cum_krw", "op_income_cum_krw", "net_income_cum_krw", "ocf_cum_krw"]


def _report(con, corp, year, q, fs_div):
    r = con.execute("SELECT value FROM meta WHERE key=?", (f"dart_fs_src|{corp}|{year}|{REPORTS[q]}|{fs_div}",)).fetchone()
    if not r:
        return None
    rcept_dt, rcept_no = r[0].split("|", 1)
    rows = con.execute("SELECT * FROM dart_fs_raw WHERE rcept_no=? AND fs_div=? ORDER BY ord", (rcept_no, fs_div)).fetchall()
    return {"rcept_no": rcept_no, "rcept_dt": rcept_dt or None, "rows": rows}


def _pick(rep, sj_divs, account_id, field):
    """sj_divs 순서대로(IS 우선) 찾은 첫 값. (값, 통화) 또는 (None, None)."""
    if rep is None:
        return None, None
    for sj in sj_divs:
        for r in rep["rows"]:
            if r["sj_div"] == sj and r["account_id"] == account_id:
                return to_int(r[field]), r["currency"]
    return None, None


def compute(con, corp: str, year: str, fs_div: str) -> dict[int, dict]:
    reps = {q: _report(con, corp, year, q, fs_div) for q in REPORTS}
    out: dict[int, dict] = {}
    for q in REPORTS:
        rep = reps[q]
        if rep is None:
            continue
        row = {c: None for c in COLS}
        notes = []
        for name, acc in IS_ACCOUNTS.items():
            cur, ccy = _pick(rep, ("IS", "CIS"), acc, "thstrm_amount")
            if q < 4:
                cum = _pick(rep, ("IS", "CIS"), acc, "thstrm_add_amount")[0] if q > 1 else cur
                row[f"{name}_krw"], row[f"{name}_cum_krw"] = cur, cum
            else:
                q3_cum = _pick(reps[3], ("IS", "CIS"), acc, "thstrm_add_amount")[0]
                row[f"{name}_cum_krw"] = cur
                row[f"{name}_krw"] = cur - q3_cum if cur is not None and q3_cum is not None else None
                if cur is not None and q3_cum is None:
                    notes.append(f"{name}: 3분기 누적 없음")
            if cur is None:
                notes.append(f"{name}: 표준 계정 {acc} 없음" + (" (금융업 구조일 수 있음)" if name != "net_income" else ""))
            if ccy and ccy != "KRW":
                row[f"{name}_krw"] = row[f"{name}_cum_krw"] = None
                notes.append(f"{name}: 통화 {ccy}")
        for name, acc in CF_ACCOUNTS.items():
            cum, ccy = _pick(rep, ("CF",), acc, "thstrm_amount")  # 현금흐름표 thstrm_amount = 누적 (실측)
            row[f"{name}_cum_krw"] = cum
            if q == 1:
                row[f"{name}_krw"] = cum
            else:
                prev = _pick(reps[q - 1], ("CF",), acc, "thstrm_amount")[0]
                row[f"{name}_krw"] = cum - prev if cum is not None and prev is not None else None
                if cum is not None and prev is None:
                    notes.append(f"{name}: 직전 보고서 누적 없음")
            if cum is None:
                notes.append(f"{name}: 표준 계정 {acc} 없음")
            if ccy and ccy != "KRW":
                row[f"{name}_krw"] = row[f"{name}_cum_krw"] = None
                notes.append(f"{name}: 통화 {ccy}")
        for name, acc in BS_ACCOUNTS.items():
            v, ccy = _pick(rep, ("BS",), acc, "thstrm_amount")
            row[f"{name}_krw"] = v if not (ccy and ccy != "KRW") else None
            if v is None:
                notes.append(f"{name}: 표준 계정 {acc} 없음")
        row["rcept_no"], row["rcept_dt"] = rep["rcept_no"], rep["rcept_dt"]
        row["calc_note"] = "; ".join(notes) or None
        out[q] = row
    return out


def recompute(con, corp: str, year: str, ts: str) -> int:
    n = 0
    for fs_div in ("CFS", "OFS"):
        for q, row in compute(con, corp, year, fs_div).items():
            old = con.execute("SELECT * FROM dart_fs_quarter WHERE corp_code=? AND fiscal_year=? AND quarter=? AND fs_div=?",
                              (corp, year, q, fs_div)).fetchone()
            if old is not None:
                for c in COLS:
                    if old[c] != row[c]:
                        db.record_change(con, "dart_fs_quarter", f"{corp}|{year}|Q{q}|{fs_div}", c, old[c], row[c], ts,
                                         f"보고서 {old['rcept_no']} -> {row['rcept_no']}")
            con.execute(
                "INSERT OR REPLACE INTO dart_fs_quarter(corp_code, fiscal_year, quarter, fs_div, " + ", ".join(COLS) +
                ", rcept_no, rcept_dt, calc_note, updated_at) VALUES(" + ",".join("?" * (len(COLS) + 8)) + ")",
                (corp, year, q, fs_div, *[row[c] for c in COLS], row["rcept_no"], row["rcept_dt"], row["calc_note"], ts))
            n += 1
    return n
