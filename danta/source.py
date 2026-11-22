"""기존 수집기 DB 읽기 (읽기 전용).

sqlite URI의 mode=ro로 열어서 쓰기 자체가 불가능하다.
사용 테이블: trading_calendar(date, is_open), price_daily(date, code, value_krw, change_rate_pct),
universe_daily(date, code, in_universe),
krx_base_daily / stock_master(보통주 판정, universe_daily가 빠진 날짜에만).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import hashlib
import csv
import io

from collector.themes import CODE_RE, _read_text, fix_code
from collector.universe import exclude_reason


class SourceDbError(Exception):
    pass


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SourceDbError(f"기존 수집기 DB가 없습니다: {path} (설정 SOURCE_DB로 지정 가능)")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def open_days(con, start: str, end: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date BETWEEN ? AND ? ORDER BY date", (start, end))]


def last_open_before(con, day: str) -> str | None:
    r = con.execute("SELECT MAX(date) FROM trading_calendar WHERE is_open=1 AND date < ?", (day,)).fetchone()
    return r[0] if r else None


def has_universe(con, day: str) -> bool:
    return con.execute("SELECT 1 FROM universe_daily WHERE date=? AND in_universe=1 LIMIT 1", (day,)).fetchone() is not None


def universe_dates(con) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM universe_daily WHERE in_universe=1 ORDER BY date")]


def universe_set(con, day: str) -> set[str]:
    return {r[0] for r in con.execute("SELECT code FROM universe_daily WHERE date=? AND in_universe=1", (day,))}


def common_from_price(con, day: str) -> tuple[set[str], str, list[str]]:
    """그날 price_daily 종목 중 보통주만 (기존 수집기 universe.exclude_reason과 같은 기준).

    종목 정보는 그날 krx_base_daily 우선, 없으면 stock_master(최신 정보). 정보가 없는 종목은 뺀다.
    반환: (보통주 집합, 사용한 정보 출처, 정보가 없어 판정 못 한 종목 목록)
    """
    codes = [r[0] for r in con.execute("SELECT code FROM price_daily WHERE date=?", (day,))]
    info = {r[0]: r[1:] for r in con.execute(
        "SELECT code, secugrp, kind, sect_tp, name FROM krx_base_daily WHERE date=?", (day,))}
    src = "krx_base_daily"
    if not info:
        info = {r[0]: r[1:] for r in con.execute(
            "SELECT code, krx_secugrp, krx_kind, krx_sect_tp, name FROM stock_master")}
        src = "stock_master"
    out, unknown = set(), []
    for c in codes:
        if c not in info:
            unknown.append(c)
        elif exclude_reason(*info[c]) is None:
            out.add(c)
    return out, src, sorted(unknown)


def presumed_common(code: str) -> bool:
    """표시용: 종목코드 끝자리가 0이면 보통주일 가능성이 높다 (포함 여부에는 쓰지 않음)."""
    return code.endswith("0")


def select_codes(con, day: str, top_value: int, top_change: int, allowed: set[str]) -> dict[str, str]:
    """{code: reason}. allowed 안에서 그날 일봉 기준 거래대금 상위 + 등락률 상위, 중복 제거."""
    rows = [r for r in con.execute(
        "SELECT code, value_krw, change_rate_pct FROM price_daily WHERE date=?", (day,)) if r[0] in allowed]
    by_value = sorted((r for r in rows if r[1] is not None), key=lambda r: (-r[1], r[0]))[:top_value]
    by_change = sorted((r for r in rows if r[2] is not None), key=lambda r: (-r[2], r[0]))[:top_change]
    out: dict[str, str] = {}
    for r in by_value:
        out[r[0]] = "value"
    for r in by_change:
        out[r[0]] = "value+change" if r[0] in out else "change"
    return out


def value_extremes(con, day: str, n: int = 5) -> tuple[list[str], list[str]]:
    """그날 보통주 범위(universe_daily, 없으면 price_daily 전체) 중 거래대금 상위 n, 하위 n(0 초과)."""
    has_u = con.execute("SELECT 1 FROM universe_daily WHERE date=? LIMIT 1", (day,)).fetchone() is not None
    base = ("SELECT p.code FROM price_daily p JOIN universe_daily u ON u.date=p.date AND u.code=p.code"
            " AND u.in_universe=1 WHERE p.date=? AND p.value_krw > 0") if has_u else \
           "SELECT p.code FROM price_daily p WHERE p.date=? AND p.value_krw > 0"
    top = [r[0] for r in con.execute(base + " ORDER BY p.value_krw DESC, p.code LIMIT ?", (day, n))]
    low = [r[0] for r in con.execute(base + " ORDER BY p.value_krw ASC, p.code LIMIT ?", (day, n))]
    return top, [c for c in low if c not in top]


def daily_value(con, day: str, code: str) -> int | None:
    r = con.execute("SELECT value_krw FROM price_daily WHERE date=? AND code=?", (day, code)).fetchone()
    return r[0] if r else None


def calendar_status(con, day: str) -> int | None:
    """1=개장, 0=휴장, None=달력에 없음(모름)."""
    r = con.execute("SELECT is_open FROM trading_calendar WHERE date=?", (day,)).fetchone()
    return None if r is None else int(r[0])


def has_price(con, day: str) -> bool:
    return con.execute("SELECT 1 FROM price_daily WHERE date=? LIMIT 1", (day,)).fetchone() is not None


def read_theme_file(path: Path) -> tuple[list[tuple], str]:
    """기존 수집기 theme_master.csv 읽기 (읽기만). 코드 보정·형식 검사는 기존 수집기와 같은 규칙.

    반환: [(code, theme, added_date, basis, note)], 파일 sha256
    """
    raw = path.read_bytes()
    out, seen = [], set()
    for r in csv.DictReader(io.StringIO(_read_text(path))):
        t, c = (r.get("theme") or "").strip(), fix_code((r.get("code") or "").strip())
        if t and CODE_RE.match(c) and (t, c) not in seen:
            seen.add((t, c))
            out.append((c, t, (r.get("added_date") or "").strip() or None, (r.get("basis") or "").strip() or None,
                        (r.get("note") or "").strip() or None))
    return out, hashlib.sha256(raw).hexdigest()
