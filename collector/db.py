"""SQLite 저장소 (명세서 7장, 14장).

- 금액 컬럼은 단위 접미사(_krw, _mil). 단위가 확인되지 않은 값은 _unconfirmed.
- 빈 값은 NULL. 0은 실제 0일 때만.
- 모든 테이블에 source, fetched_at, raw_file(데이터 폴더 기준 상대 경로).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS trading_calendar (
    date TEXT PRIMARY KEY, is_open INTEGER NOT NULL,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_file TEXT);

CREATE TABLE IF NOT EXISTS stock_master (
    code TEXT PRIMARY KEY, name TEXT, market TEXT, listed_date TEXT,
    krx_secugrp TEXT, krx_sect_tp TEXT, krx_kind TEXT,
    kis_scty_grp TEXT, kis_stck_kind TEXT, kis_checked_at TEXT,
    is_admin INTEGER, is_halted INTEGER,
    first_seen TEXT, last_seen TEXT,
    source TEXT, fetched_at TEXT, raw_file TEXT);

CREATE TABLE IF NOT EXISTS krx_base_daily (
    date TEXT NOT NULL, code TEXT NOT NULL, market TEXT, name TEXT, listed_date TEXT,
    secugrp TEXT, sect_tp TEXT, kind TEXT, list_shares INTEGER,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, code));

CREATE TABLE IF NOT EXISTS universe_daily (
    date TEXT NOT NULL, code TEXT NOT NULL,
    in_universe INTEGER NOT NULL, exclude_reason TEXT,
    is_admin INTEGER, is_halted INTEGER, sect_note TEXT,
    days_since_listing INTEGER, is_new_listing INTEGER, max_window TEXT, listing_note TEXT,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, code));

CREATE TABLE IF NOT EXISTS price_daily (
    date TEXT NOT NULL, code TEXT NOT NULL, market TEXT,
    open REAL, high REAL, low REAL, close REAL, change_rate_pct REAL,
    volume_sh INTEGER, value_krw INTEGER, mktcap_krw INTEGER, listed_shares INTEGER,
    is_provisional INTEGER NOT NULL DEFAULT 0,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, code));

CREATE TABLE IF NOT EXISTS index_daily (
    date TEXT NOT NULL, series TEXT NOT NULL, index_name TEXT NOT NULL, index_class TEXT,
    open REAL, high REAL, low REAL, close REAL, change_rate_pct REAL,
    volume_unconfirmed INTEGER, value_unconfirmed INTEGER, mktcap_unconfirmed INTEGER,
    is_provisional INTEGER NOT NULL DEFAULT 0,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, series, index_name));

CREATE TABLE IF NOT EXISTS investor_daily (
    date TEXT NOT NULL, code TEXT NOT NULL,
    frgn_net_qty INTEGER, orgn_net_qty INTEGER, prsn_net_qty INTEGER,
    frgn_net_amt_mil INTEGER, orgn_net_amt_mil INTEGER, prsn_net_amt_mil INTEGER,
    detail_json TEXT, is_provisional INTEGER NOT NULL DEFAULT 0,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, code));

CREATE TABLE IF NOT EXISTS collect_log (
    source TEXT NOT NULL, dataset TEXT NOT NULL, unit_key TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, empty_streak INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, raw_file TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (source, dataset, unit_key));

CREATE TABLE IF NOT EXISTS value_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tbl TEXT NOT NULL, key TEXT NOT NULL, col TEXT NOT NULL,
    old TEXT, new TEXT, changed_at TEXT NOT NULL, note TEXT);

CREATE TABLE IF NOT EXISTS run_log (
    run_id TEXT PRIMARY KEY, mode TEXT, version TEXT, started_at TEXT, ended_at TEXT,
    calls INTEGER, errors INTEGER, summary TEXT);
"""

# 버전별 업그레이드 (이전 버전 DB에 순서대로 적용)
UPGRADES = {
    2: """
CREATE TABLE IF NOT EXISTS market_investor_daily (
    date TEXT NOT NULL, market TEXT NOT NULL,
    frgn_net_qty_unconfirmed INTEGER, orgn_net_qty_unconfirmed INTEGER, prsn_net_qty_unconfirmed INTEGER,
    frgn_net_amt_unconfirmed INTEGER, orgn_net_amt_unconfirmed INTEGER, prsn_net_amt_unconfirmed INTEGER,
    index_close REAL, detail_json TEXT, is_provisional INTEGER NOT NULL DEFAULT 0,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (date, market));
""",
    3: """
CREATE TABLE IF NOT EXISTS theme_versions (
    version INTEGER PRIMARY KEY, loaded_at TEXT NOT NULL, file_hash TEXT NOT NULL, row_count INTEGER NOT NULL,
    history_file TEXT);
CREATE TABLE IF NOT EXISTS theme_membership (
    version INTEGER NOT NULL, theme TEXT NOT NULL, code TEXT NOT NULL,
    added_date TEXT, basis TEXT, note TEXT,
    PRIMARY KEY (version, theme, code));
CREATE TABLE IF NOT EXISTS briefing_unclassified (
    date TEXT NOT NULL, code TEXT NOT NULL, list_name TEXT NOT NULL, generated_at TEXT NOT NULL,
    PRIMARY KEY (date, code, list_name));
""",
    4: """
CREATE TABLE IF NOT EXISTS adj_price_daily (
    code TEXT NOT NULL, date TEXT NOT NULL, as_of TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    source TEXT, fetched_at TEXT, raw_file TEXT,
    PRIMARY KEY (code, date));
""",
}

STATUSES = ("success", "provisional", "empty", "failed", "holiday", "not_listed")


class DbError(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None)  # 트랜잭션은 직접 BEGIN/COMMIT
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db(con: sqlite3.Connection) -> int:
    """없으면 만들고, 스키마 버전을 확인·업그레이드하고, 무결성을 검사한다 (명세서 2.3)."""
    con.executescript(SCHEMA_V1)
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row else 0
    if current > SCHEMA_VERSION:
        raise DbError(f"DB 스키마 버전({current})이 프로그램({SCHEMA_VERSION})보다 높습니다. 새 버전 프로그램을 설치하세요.")
    for v in sorted(UPGRADES):
        if v > current:
            con.executescript(UPGRADES[v])
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        raise DbError(f"DB 무결성 검사 실패: {ok}. backup/의 스냅샷으로 복구가 필요합니다.")
    return SCHEMA_VERSION


class Tx:
    """수집 단위 하나 = 트랜잭션 하나 (명세서 6.3). 예외가 나면 그 단위만 버린다."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def __enter__(self):
        self.con.execute("BEGIN")
        return self.con

    def __exit__(self, exc_type, exc, tb):
        self.con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def get_status(con, source: str, dataset: str, unit_key: str) -> str | None:
    r = con.execute("SELECT status FROM collect_log WHERE source=? AND dataset=? AND unit_key=?",
                    (source, dataset, unit_key)).fetchone()
    return r["status"] if r else None


def set_status(con, source: str, dataset: str, unit_key: str, status: str, now: str,
               error: str | None = None, raw_file: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    prev = con.execute("SELECT attempts, empty_streak FROM collect_log WHERE source=? AND dataset=? AND unit_key=?",
                       (source, dataset, unit_key)).fetchone()
    attempts = (prev["attempts"] if prev else 0) + 1
    streak = ((prev["empty_streak"] if prev else 0) + 1) if status == "empty" else 0
    con.execute(
        "INSERT OR REPLACE INTO collect_log(source, dataset, unit_key, status, attempts, empty_streak, last_error, raw_file, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (source, dataset, unit_key, status, attempts, streak, error, raw_file, now))


def record_change(con, tbl: str, key: str, col: str, old, new, now: str, note: str | None = None) -> None:
    con.execute("INSERT INTO value_changes(tbl, key, col, old, new, changed_at, note) VALUES(?,?,?,?,?,?,?)",
                (tbl, key, col, None if old is None else str(old), None if new is None else str(new), now, note))
