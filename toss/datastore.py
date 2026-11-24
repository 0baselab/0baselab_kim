"""수집 데이터 저장소 (toss_data/).

- daily.sqlite: 종목 목록(universe), 거래일(trading_day), 일봉(daily_bar), 수급(investor_daily), 진행 기록
- minute/YYYY/minute_YYYY-MM-DD.sqlite: 날짜별 분봉 파일. 파일 안의 code_done(종목별 완료)과
  file_meta.complete로 수집 여부를 판단한다(파일명만으로 판단하지 않음). 나중에 동기화 폴더로 옮기기 쉽게
  한 번 완성된 파일은 다시 고치지 않는다.

가격은 원 단위 정수(소수가 있으면 실수), 분봉 시각 t는 봉 '시작' 시각의 0시 기준 분(예: 09:00 → 540).
거래대금은 API가 주지 않아 추정값(value_est_krw)만 둔다.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

DAILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    code TEXT PRIMARY KEY, name TEXT, market TEXT, security_type TEXT, is_common INTEGER,
    list_date TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trading_day (date TEXT PRIMARY KEY, source TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily_bar (
    code TEXT NOT NULL, date TEXT NOT NULL,
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume INTEGER,
    value_est_krw INTEGER,      -- 추정 거래대금. 분봉 (종가×거래량) 합 또는 일봉 종가×거래량
    value_est_src TEXT,         -- minute / daily_close
    PRIMARY KEY (code, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS investor_daily (
    code TEXT NOT NULL, date TEXT NOT NULL,
    frgn_net_vol INTEGER, orgn_net_vol INTEGER, prsn_net_vol INTEGER, other_net_vol INTEGER, updated_at TEXT,
    PRIMARY KEY (code, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS progress (
    kind TEXT NOT NULL, code TEXT NOT NULL, last_date TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MINUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar (
    code TEXT NOT NULL, t INTEGER NOT NULL,
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume INTEGER NOT NULL,
    PRIMARY KEY (code, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS code_done (
    code TEXT PRIMARY KEY, n_bars INTEGER NOT NULL, n_calls INTEGER NOT NULL, fetched_at TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS code_fail (
    code TEXT PRIMARY KEY, fail_count INTEGER NOT NULL, last_error TEXT, updated_at TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS file_meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


def num(v):
    """'192000' → 192000, '12.5' → 12.5, 빈 값 → None"""
    if v is None or v == "":
        return None
    d = Decimal(str(v))
    return int(d) if d == d.to_integral_value() else float(d)


def connect_daily(store_dir: Path) -> sqlite3.Connection:
    store_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(store_dir / "daily.sqlite"))
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(DAILY_SCHEMA)
    return con


def minute_path(store_dir: Path, day: str) -> Path:
    """day: YYYY-MM-DD"""
    return store_dir / "minute" / day[:4] / f"minute_{day}.sqlite"


def connect_minute(store_dir: Path, day: str) -> sqlite3.Connection:
    p = minute_path(store_dir, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.executescript(MINUTE_SCHEMA)
    con.execute("INSERT OR IGNORE INTO file_meta VALUES ('date', ?)", (day,))
    con.commit()
    return con


def minute_status(store_dir: Path, day: str) -> str:
    """complete / partial / missing (파일 안의 기록으로 판단)"""
    p = minute_path(store_dir, day)
    if not p.exists():
        return "missing"
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT value FROM file_meta WHERE key='complete'").fetchone()
    except sqlite3.OperationalError:
        r = None
    finally:
        con.close()
    return "complete" if r and r[0] == "1" else "partial"
