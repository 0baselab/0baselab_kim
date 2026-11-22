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
    common_check TEXT NOT NULL DEFAULT 'confirmed',
    -- confirmed: universe 또는 종목정보로 보통주 확인 / unknown_code0, unknown_other: 종목정보 없음(판정 불가),
    -- 코드 끝자리 0 여부는 표시용. 분석 단계에서 WHERE common_check='confirmed'로 걸러낼 수 있다.
    PRIMARY KEY (date, code));
CREATE TABLE IF NOT EXISTS fail_log (
    code TEXT NOT NULL, date TEXT NOT NULL,
    fail_count INTEGER NOT NULL,   -- 연속 실패 횟수 (실행 1회당 최대 1). 성공하면 행 삭제
    last_error TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS theme_snapshot (
    date TEXT NOT NULL, code TEXT NOT NULL, theme TEXT NOT NULL,
    added_date TEXT, basis TEXT, note TEXT, file_hash TEXT NOT NULL,
    PRIMARY KEY (date, theme, code));
CREATE TABLE IF NOT EXISTS job_days (
    date TEXT PRIMARY KEY,
    method TEXT NOT NULL,     -- universe / past_universe / price_common
    basis_date TEXT,          -- past_universe일 때 빌려 쓴 과거 날짜
    note TEXT,                -- price_common일 때 종목정보 출처, 정보 없어 뺀 종목 수 등
    n_codes INTEGER NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "common_check" not in cols:  # 이전 버전 DB
        con.execute("ALTER TABLE jobs ADD COLUMN common_check TEXT NOT NULL DEFAULT 'confirmed'")
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
        con.execute("DELETE FROM fail_log WHERE code=? AND date=?", (res.code, res.date))


def is_done(con, code: str, date: str) -> bool:
    return con.execute("SELECT 1 FROM done WHERE code=? AND date=?", (code, date)).fetchone() is not None


def pending_jobs(con, max_fail: int = 3) -> list[tuple[str, str]]:
    """(date, code). 가장 오래된 날짜부터. 연속 max_fail회 실패한 건은 제외 (0이면 제외 안 함)."""
    return con.execute(
        "SELECT j.date, j.code FROM jobs j LEFT JOIN done d ON d.code=j.code AND d.date=j.date"
        " LEFT JOIN fail_log f ON f.code=j.code AND f.date=j.date"
        " WHERE d.code IS NULL AND (? = 0 OR f.fail_count IS NULL OR f.fail_count < ?)"
        " ORDER BY j.date, j.code", (max_fail, max_fail)).fetchall()


def record_fail(con, code: str, date: str, error: str, now: str) -> int:
    with con:
        con.execute("INSERT INTO fail_log(code,date,fail_count,last_error,updated_at) VALUES (?,?,1,?,?)"
                    " ON CONFLICT(code,date) DO UPDATE SET fail_count=fail_count+1, last_error=excluded.last_error,"
                    " updated_at=excluded.updated_at", (code, date, error[:300], now))
    return con.execute("SELECT fail_count FROM fail_log WHERE code=? AND date=?", (code, date)).fetchone()[0]


def given_up(con, max_fail: int = 3) -> list[tuple]:
    """재시도를 멈춘 (date, code, fail_count, last_error)."""
    return con.execute("SELECT date, code, fail_count, last_error FROM fail_log WHERE fail_count >= ?"
                       " ORDER BY date, code", (max_fail,)).fetchall()


def planned_dates(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT date FROM job_days")}


def add_jobs(con, rows: list[tuple], day_rows: list[tuple]) -> None:
    """기존 작업을 지우지 않고 새 날짜의 작업만 추가 (daily용)."""
    with con:
        con.executemany("INSERT OR IGNORE INTO jobs(date, code, reason, common_check) VALUES (?,?,?,?)", rows)
        con.executemany("INSERT OR REPLACE INTO job_days(date, method, basis_date, note, n_codes)"
                        " VALUES (?,?,?,?,?)", day_rows)


def bar_date_range(con) -> tuple[str | None, str | None]:
    return con.execute("SELECT MIN(date), MAX(date) FROM done WHERE n_bars > 0").fetchone()


def save_theme_snapshot(con, day: str, rows: list[tuple], file_hash: str) -> bool:
    """rows: (code, theme, added_date, basis, note). 같은 날짜가 이미 있으면 건너뛰고 False."""
    if con.execute("SELECT 1 FROM theme_snapshot WHERE date=? LIMIT 1", (day,)).fetchone():
        return False
    with con:
        con.executemany("INSERT OR IGNORE INTO theme_snapshot(date,code,theme,added_date,basis,note,file_hash)"
                        " VALUES (?,?,?,?,?,?,?)", [(day, *r, file_hash) for r in rows])
    return True


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
