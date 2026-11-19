"""대상 종목 필터(14.3)와 신규 상장 인덱스(5.2). 날짜별로 universe_daily에 기록한다."""
from __future__ import annotations

from . import trading_calendar as cal

WINDOWS = (5, 20, 60, 120, 250)
NEW_LISTING_DAYS = 60
SECT_NOTES = ("관리종목(소속부없음)", "투자주의환기종목(소속부없음)", "외국기업(소속부없음)")
SPAC_SECT = "SPAC(소속부없음)"


def exclude_reason(secugrp, kind, sect, name) -> str | None:
    """14.3: 세 조건을 모두 만족해야 분석 대상. 위에서부터 순서대로 사유를 붙인다."""
    if secugrp != "주권":
        return "reits" if secugrp and "부동산" in secugrp else "not_stock"
    if kind != "보통주":
        return "not_common"
    if sect == SPAC_SECT or "스팩" in (name or ""):
        return "spac"
    return None


def compute(con, day: str, ts: str) -> int:
    """day의 KRX 종목기본정보로 universe_daily를 다시 계산한다. 기록한 행 수를 돌려준다."""
    rows = con.execute("SELECT * FROM krx_base_daily WHERE date=?", (day,)).fetchall()
    if not rows:
        return 0
    open_list = [r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date <= ? ORDER BY date", (day,))]
    cal_start = open_list[0] if open_list else None
    index_of = {d: i for i, d in enumerate(open_list)}
    # KRX 매매정보를 받은 거래일 수 (day 포함, 이전)
    data_days = con.execute(
        "SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND dataset LIKE 'trade_%' AND status='success'"
        " AND unit_key <= ? AND dataset = ?", (day, "trade_KOSPI")).fetchone()[0]
    con.execute("DELETE FROM universe_daily WHERE date=?", (day,))
    for r in rows:
        reason = exclude_reason(r["secugrp"], r["kind"], r["sect_tp"], r["name"])
        listed = r["listed_date"]
        days, note = None, None
        if listed and cal_start and listed >= cal_start:
            # 상장일 = 1. 상장일이 휴장일로 잡혀 있으면 그 다음 거래일부터 센다.
            first = next((d for d in open_list if d >= listed), None)
            days = (index_of[day] - index_of[first] + 1) if first and day in index_of else None
        elif listed and cal_start:
            note = f"상장일이 거래일 달력 시작({cal_start}) 이전"
        if days is not None:
            is_new = 1 if days < NEW_LISTING_DAYS else 0
        else:
            is_new = 0 if len(open_list) >= NEW_LISTING_DAYS and listed and cal_start and listed < cal_start else None
        secured = min(days, data_days) if days is not None else data_days
        fit = [w for w in WINDOWS if w <= secured]
        max_window = str(fit[-1]) if fit else "none"
        if days is not None and max_window != "250":
            nxt = next((w for w in WINDOWS if w > secured), None)
            note = f"상장 {days}거래일째" + (f", {nxt}일 지표 미산정" if nxt else "")
        sect_note = r["sect_tp"] if r["sect_tp"] in SECT_NOTES else None
        con.execute(
            "INSERT INTO universe_daily(date, code, in_universe, exclude_reason, is_admin, is_halted, sect_note,"
            " days_since_listing, is_new_listing, max_window, listing_note, source, fetched_at, raw_file)"
            " VALUES(?,?,?,?,NULL,NULL,?,?,?,?,?,'KRX',?,?)",
            (day, r["code"], 0 if reason else 1, reason, sect_note, days, is_new, max_window, note, ts, r["raw_file"]))
    return len(rows)


def universe_codes(con, day: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT code FROM universe_daily WHERE date=? AND in_universe=1 ORDER BY code", (day,))]
