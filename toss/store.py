"""측정 결과 DB (toss_probe.sqlite)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ratelimit_log (
    at TEXT NOT NULL, grp TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL,
    limit_hdr TEXT, remaining_hdr TEXT, reset_hdr TEXT);
CREATE TABLE IF NOT EXISTS results (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candle (
    symbol TEXT NOT NULL, interval TEXT NOT NULL, adjusted INTEGER NOT NULL, ts TEXT NOT NULL,
    open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT,
    PRIMARY KEY (symbol, interval, adjusted, ts));
CREATE TABLE IF NOT EXISTS ranking_snap (
    at TEXT NOT NULL, type TEXT NOT NULL, duration TEXT NOT NULL, ranked_at TEXT,
    rank INTEGER, symbol TEXT, last_price TEXT, change_rate TEXT, trading_amount TEXT, trading_volume TEXT);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def put_result(con, key: str, value, now: str) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO results(key, value_json, updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False, default=str), now))


def get_result(con, key: str):
    r = con.execute("SELECT value_json, updated_at FROM results WHERE key=?", (key,)).fetchone()
    return (json.loads(r[0]), r[1]) if r else (None, None)


def log_rate(con, at: str, rec) -> None:
    con.execute("INSERT INTO ratelimit_log VALUES (?,?,?,?,?,?,?)",
                (at, rec.group, rec.path, rec.status, rec.limit, rec.remaining, rec.reset))


def save_candles(con, symbol: str, interval: str, adjusted: bool, bars: list[dict]) -> None:
    with con:
        con.executemany("INSERT OR REPLACE INTO candle VALUES (?,?,?,?,?,?,?,?,?)",
                        [(symbol, interval, int(adjusted), b["timestamp"], b.get("openPrice"), b.get("highPrice"),
                          b.get("lowPrice"), b.get("closePrice"), b.get("volume")) for b in bars])
