"""단타 전용 새 DB (SQLite). 원본 JSON은 저장하지 않는다."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .fetch import DayResult, minute_values

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bar (
    code TEXT NOT NULL, date TEXT NOT NULL, time TEXT NOT NULL,
    open INTEGER, high INTEGER, low INTEGER, close INTEGER,
    volume INTEGER,
    acml_value_krw INTEGER,  -- KIS acml_tr_pbmn (누적 거래대금, 원 단위는 확인 필요)
    value_krw INTEGER,       -- 1분 거래대금 = 누적의 앞 봉 대비 차이 (ACML_IS_CUMULATIVE=Y일 때만 계산)
    PRIMARY KEY (code, date, time));
CREATE TABLE IF NOT EXISTS done (
    code TEXT NOT NULL, date TEXT NOT NULL,
    n_bars INTEGER NOT NULL, n_calls INTEGER NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS jobs (
    date TEXT NOT NULL, code TEXT NOT NULL, reason TEXT,
    PRIMARY KEY (date, code));
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def save_day(con: sqlite3.Connection, res: DayResult, fetched_at: str, compute_value: bool) -> None:
    """분봉과 완료 기록을 한 트랜잭션으로 저장 (중간에 꺼져도 반쪽 기록이 남지 않음)."""
    vals = minute_values(res.bars) if compute_value else [None] * len(res.bars)
    with con:
        con.execute("DELETE FROM minute_bar WHERE code=? AND date=?", (res.code, res.date))
        con.executemany(
            "INSERT INTO minute_bar(code,date,time,open,high,low,close,volume,acml_value_krw,value_krw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(res.code, res.date, b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"],
              b["acml_value_krw"], v) for b, v in zip(res.bars, vals)])
        con.execute("INSERT OR REPLACE INTO done(code,date,n_bars,n_calls,fetched_at) VALUES (?,?,?,?,?)",
                    (res.code, res.date, len(res.bars), res.calls, fetched_at))


def is_done(con, code: str, date: str) -> bool:
    return con.execute("SELECT 1 FROM done WHERE code=? AND date=?", (code, date)).fetchone() is not None


def pending_jobs(con) -> list[tuple[str, str]]:
    """(date, code). 가장 오래된 날짜부터."""
    return con.execute(
        "SELECT j.date, j.code FROM jobs j LEFT JOIN done d ON d.code=j.code AND d.date=j.date"
        " WHERE d.code IS NULL ORDER BY j.date, j.code").fetchall()


def recalc_values(con) -> int:
    """이미 저장한 분봉의 value_krw를 누적 거래대금으로 다시 계산한다. 갱신한 (종목,날짜) 수."""
    keys = con.execute("SELECT DISTINCT code, date FROM minute_bar").fetchall()
    with con:
        for code, date in keys:
            rows = con.execute("SELECT time, acml_value_krw FROM minute_bar WHERE code=? AND date=? ORDER BY time",
                               (code, date)).fetchall()
            vals = minute_values([{"acml_value_krw": a} for _, a in rows])
            con.executemany("UPDATE minute_bar SET value_krw=? WHERE code=? AND date=? AND time=?",
                            [(v, code, date, t) for (t, _), v in zip(rows, vals)])
    return len(keys)
