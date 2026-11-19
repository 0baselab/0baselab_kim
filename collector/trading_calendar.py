"""거래일 달력 (명세서 7.1, 14장 결정).

- 오늘과 앞으로: KIS 국내휴장일조회(CTCA0903R) opnd_yn. 하루 1회 (collect_log로 판단).
- 과거: KIS 국내업종 일자별지수(FHPUP02120000, 코스피 0001) 응답 날짜. 1회 최대 100거래일.
  응답이 덮는 기간 안에서 응답에 없는 날은 휴장(is_open=0)으로 기록한다.
- 평일을 거래일로 가정하지 않는다. 달력에 없는 날은 '모름'이다.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import db
from .kis import HOLIDAY, INDEX_DAILY, KisClient, holiday_params, index_daily_params, is_ok
from .rawstore import save_raw

log = logging.getLogger("collector")

SRC_HOLIDAY = "KIS_HOLIDAY"
SRC_INDEX = "KIS_INDEX"
MAX_INDEX_PAGES = 6


def _days(start: str, end: str):
    d = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    while d <= e:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def _prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def _upsert(con, day: str, is_open: int, source: str, now: str, raw: str | None) -> None:
    con.execute("INSERT OR REPLACE INTO trading_calendar(date, is_open, source, fetched_at, raw_file) VALUES(?,?,?,?,?)",
                (day, is_open, source, now, raw))


def update_holidays(con, kis: KisClient, root, now: datetime) -> str:
    """하루 1회. 이미 오늘 받았으면 호출하지 않는다."""
    today = now.strftime("%Y%m%d")
    ts = now.isoformat(timespec="seconds")
    if db.get_status(con, "KIS", "holiday", today) == "success":
        return "오늘 이미 조회함 (호출 0회)"
    body = kis.get(HOLIDAY, holiday_params(today)).body
    raw = save_raw(root, "kis", "holiday", today, body, now)
    if not is_ok(body):
        with db.Tx(con):
            db.set_status(con, "KIS", "holiday", today, "failed", ts, f"{body.get('msg_cd')} {body.get('msg1')}", raw)
        return f"실패: {body.get('msg_cd')} {body.get('msg1')}"
    out = body.get("output") or []
    rows = out if isinstance(out, list) else [out]
    with db.Tx(con):
        for r in rows:
            d, yn = r.get("bass_dt"), r.get("opnd_yn")
            if d and yn in ("Y", "N"):
                _upsert(con, d, 1 if yn == "Y" else 0, SRC_HOLIDAY, ts, raw)
        db.set_status(con, "KIS", "holiday", today, "success", ts, raw_file=raw)
    return f"{len(rows)}일치 갱신"


def update_past(con, kis: KisClient, root, now: datetime, need_open_days: int) -> str:
    """과거 거래일을 KIS 업종 일자별지수(코스피) 날짜로 채운다. 하루 1회 + 부족하면 더 과거로."""
    today = now.strftime("%Y%m%d")
    ts = now.isoformat(timespec="seconds")
    if db.get_status(con, "KIS", "calendar_index", today) == "success" and \
            count_open_before(con, today) >= need_open_days:
        return "오늘 이미 갱신함 (호출 0회)"
    prev_latest = con.execute(
        "SELECT MAX(date) FROM trading_calendar WHERE date < ? AND source=?", (today, SRC_INDEX)).fetchone()[0]
    query, fetched, calls = today, set(), 0
    raws = []
    while calls < MAX_INDEX_PAGES:
        body = kis.get(INDEX_DAILY, index_daily_params("0001", query)).body
        calls += 1
        raw = save_raw(root, "kis", "calendar_index", query, body, now)
        raws.append(raw)
        if not is_ok(body):
            msg = f"{body.get('msg_cd')} {body.get('msg1')}"
            with db.Tx(con):
                db.set_status(con, "KIS", "calendar_index", today, "failed", ts, msg, raw)
            if fetched:
                break
            return f"실패: {msg}"
        dates = {r.get("stck_bsop_date") for r in (body.get("output2") or []) if r.get("stck_bsop_date")}
        dates = {d for d in dates if d < today}  # 오늘 개장 여부는 휴장일조회로 판단
        new = dates - fetched
        if not new:
            break
        fetched |= new
        oldest = min(fetched)
        enough = count_open_before(con, today, extra=fetched) >= need_open_days
        reached_old = prev_latest is not None and oldest <= prev_latest
        if enough and (prev_latest is None or reached_old):
            break
        query = _prev_day(oldest)
    if not fetched:
        return "응답에 날짜 없음"
    lo, hi = min(fetched), _prev_day(today)
    with db.Tx(con):
        for d in _days(lo, hi):
            _upsert(con, d, 1 if d in fetched else 0, SRC_INDEX, ts, raws[0])
        db.set_status(con, "KIS", "calendar_index", today, "success", ts, raw_file=raws[0])
    return f"{calls}회 호출, 거래일 {len(fetched)}일 ({lo}~{max(fetched)})"


def count_open_before(con, day: str, extra: set[str] | None = None) -> int:
    rows = {r[0] for r in con.execute("SELECT date FROM trading_calendar WHERE is_open=1 AND date < ?", (day,))}
    return len(rows | (extra or set()))


def is_open(con, day: str) -> bool | None:
    r = con.execute("SELECT is_open FROM trading_calendar WHERE date=?", (day,)).fetchone()
    return None if r is None else bool(r[0])


def open_days(con, start: str, end: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date BETWEEN ? AND ? ORDER BY date", (start, end))]


def last_open_days(con, before: str, n: int) -> list[str]:
    """before 이전(불포함) 최근 n개 거래일, 오래된 순."""
    rows = [r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date < ? ORDER BY date DESC LIMIT ?", (before, n))]
    return sorted(rows)


def prev_open(con, day: str) -> str | None:
    r = con.execute("SELECT MAX(date) FROM trading_calendar WHERE is_open=1 AND date < ?", (day,)).fetchone()
    return r[0]


def coverage(con) -> tuple[str | None, str | None, int]:
    r = con.execute("SELECT MIN(date), MAX(date), SUM(is_open) FROM trading_calendar").fetchone()
    return r[0], r[1], r[2] or 0
