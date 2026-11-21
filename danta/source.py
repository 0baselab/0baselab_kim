"""기존 수집기 DB 읽기 (읽기 전용).

sqlite URI의 mode=ro로 열어서 쓰기 자체가 불가능하다.
사용 테이블: trading_calendar(date, is_open), price_daily(date, code, value_krw, change_rate_pct),
universe_daily(date, code, in_universe).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


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


def _top(con, day: str, col: str, n: int) -> list[str]:
    # 그날 일봉 기준(그날 알 수 있던 값). 그날 보통주 범위(universe_daily.in_universe=1)만.
    return [r[0] for r in con.execute(
        f"SELECT p.code FROM price_daily p JOIN universe_daily u ON u.date=p.date AND u.code=p.code"
        f" WHERE p.date=? AND u.in_universe=1 AND p.{col} IS NOT NULL"
        f" ORDER BY p.{col} DESC, p.code LIMIT ?", (day, n))]


def select_codes(con, day: str, top_value: int, top_change: int) -> dict[str, str]:
    """{code: reason}. 거래대금 상위 + 등락률 상위, 중복 제거."""
    out: dict[str, str] = {}
    for c in _top(con, day, "value_krw", top_value):
        out[c] = "value"
    for c in _top(con, day, "change_rate_pct", top_change):
        out[c] = "value+change" if c in out else "change"
    return out


def daily_value(con, day: str, code: str) -> int | None:
    r = con.execute("SELECT value_krw FROM price_daily WHERE date=? AND code=?", (day, code)).fetchone()
    return r[0] if r else None
