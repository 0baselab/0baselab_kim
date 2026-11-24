"""매매 기록 DB (trade.sqlite). 모드(replay/paper/live)별로 구분해 저장하고, 누적 손실 한도는 live 기록만 센다."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
    date_from TEXT, date_to TEXT, settings_json TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS watch (
    run_id TEXT NOT NULL, date TEXT NOT NULL, code TEXT NOT NULL, rank INTEGER, vr REAL, r1 REAL,
    new_high20 INTEGER, prev_close REAL, value20 REAL, PRIMARY KEY (run_id, date, code));
CREATE TABLE IF NOT EXISTS fill (
    run_id TEXT NOT NULL, date TEXT NOT NULL, seq INTEGER NOT NULL, code TEXT NOT NULL, side TEXT NOT NULL,
    t INTEGER NOT NULL, price INTEGER NOT NULL, qty INTEGER NOT NULL, cost_krw REAL NOT NULL, reason TEXT,
    PRIMARY KEY (run_id, date, seq));
CREATE TABLE IF NOT EXISTS trade (
    run_id TEXT NOT NULL, date TEXT NOT NULL, code TEXT NOT NULL, entry_t INTEGER, entry_px INTEGER,
    exit_t INTEGER, exit_px INTEGER, qty INTEGER, gross_krw REAL, cost_krw REAL, net_krw REAL, net_pct REAL,
    exit_reason TEXT);
CREATE TABLE IF NOT EXISTS day (
    run_id TEXT NOT NULL, date TEXT NOT NULL, n_watch INTEGER, n_trades INTEGER, net_krw REAL,
    cum_krw REAL, halted TEXT, rejected_json TEXT, note TEXT, PRIMARY KEY (run_id, date));
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def start_run(con, run_id: str, mode: str, started: str, d_from: str, d_to: str, settings: dict, note: str = ""):
    with con:
        con.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, mode, started, None, d_from, d_to, json.dumps(settings, ensure_ascii=False), note))


def live_cum_realized(con) -> float:
    r = con.execute("SELECT COALESCE(SUM(t.net_krw), 0) FROM trade t JOIN run r USING(run_id) WHERE r.mode='live'").fetchone()
    return float(r[0])
