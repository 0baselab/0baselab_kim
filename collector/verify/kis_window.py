"""KIS 종목별 투자자매매동향(FHPTJ04160001) 조회 가능 시간 실측 (verify kis-window).

005930 한 종목으로 3번만 호출한다(30일 전, 1년 전, 5년 전의 직전 거래일). DB에 쓰지 않고, 키·토큰은 기록하지 않는다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..http import ApiError
from ..kis import INVESTOR_BY_STOCK, investor_params, investor_window_ok, is_ok
from ..secrets import redact
from ..timeutil import stamp

CODE = "005930"
OFFSETS = (("30일 전", 30), ("1년 전", 365), ("5년 전", 365 * 5))
WEEKDAYS = "월화수목금토일"
BLOCK_CODE = "OPSQ2001"


def _cal(db_path: Path | None):
    """거래일 달력(읽기 전용). 없으면 None."""
    if db_path is None or not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        con.execute("SELECT 1 FROM trading_calendar LIMIT 1")
        return con
    except sqlite3.Error:
        return None


def _on_or_before(cal, day: str) -> str | None:
    r = cal.execute("SELECT MAX(date) FROM trading_calendar WHERE is_open=1 AND date <= ?", (day,)).fetchone()
    return r[0] if r else None


def conclude(results: list[dict]) -> str:
    if any(r.get("msg_cd") == BLOCK_CODE for r in results):
        return f"차단됨({BLOCK_CODE})"
    if results and all(r.get("ok") for r in results):
        return "지금 조회 가능"
    return "기타 오류"


def run_kis_window(kis, now: datetime, out_dir: Path, cal_db: Path | None = None) -> tuple[Path, str, list[str]]:
    cal = _cal(cal_db)
    today = now.strftime("%Y%m%d")
    try:
        if cal is None:
            today_open = "확인 불가 (거래일 달력 없음)"
        else:
            r = cal.execute("SELECT is_open FROM trading_calendar WHERE date=?", (today,)).fetchone()
            today_open = "확인 불가 (달력에 오늘 없음)" if r is None else ("거래일" if r[0] else "휴장일")
        results = []
        for label, days in OFFSETS:
            raw = (now - timedelta(days=days)).strftime("%Y%m%d")
            d = _on_or_before(cal, raw) if cal is not None else None
            note = "" if d else " (달력 없음: 계산한 날짜 그대로)"
            d = d or raw
            r = {"label": label, "date": d, "note": note}
            try:
                body = kis.get(INVESTOR_BY_STOCK, investor_params(CODE, d)).body
                rows = [x for x in (body.get("output2") or []) if x.get("stck_bsop_date")]
                ds = sorted(x["stck_bsop_date"] for x in rows)
                r.update(rt_cd=body.get("rt_cd"), msg_cd=body.get("msg_cd"), msg1=body.get("msg1"), rows=len(rows),
                         first=ds[0] if ds else None, last=ds[-1] if ds else None, ok=is_ok(body))
            except ApiError as exc:
                r.update(rt_cd=None, msg_cd=None, msg1=redact(str(exc))[:200], rows=0, first=None, last=None, ok=False)
            results.append(r)
    finally:
        if cal is not None:
            cal.close()
    verdict = conclude(results)
    L = [f"# KIS 수급 조회 가능 시간 실측 {now.strftime('%Y-%m-%d %H:%M')}", "",
         f"- 실행 시각: {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)",
         f"- 요일: {WEEKDAYS[now.weekday()]}요일",
         f"- 오늘 거래일 여부(거래일 달력): {today_open}",
         f"- 프로그램의 기존 시간 제한(15:40~23:59) 안인가: {'예' if investor_window_ok(now) else '아니오'}",
         f"- API: 종목별 투자자매매동향 {INVESTOR_BY_STOCK[0]}, 종목 {CODE}, 시장 J", "",
         "| 조회 기준 | FID_INPUT_DATE_1 | rt_cd | msg_cd | msg1 | 응답 행 수 | 첫 날짜 | 끝 날짜 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r['label']} | {r['date']}{r['note']} | {r['rt_cd'] or ''} | {r['msg_cd'] or ''} | {r['msg1'] or ''} | "
                 f"{r['rows']} | {r['first'] or ''} | {r['last'] or ''} |")
    L += ["", f"**결론: {verdict}**"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kis_window_{stamp(now)}.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path, verdict, L
