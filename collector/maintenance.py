"""로그 정리(11.3)와 DB 스냅샷(11.4)."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .timeutil import KST, stamp

LOG_RE = re.compile(r"^run_(\d{8})-\d{6}\.log$")


def cleanup_logs(log_dir: Path, now: datetime, keep_days: int) -> int:
    """파일명의 생성 날짜로 오래된 로그를 지운다 (수집 여부 판단이 아니라 보관 기간 관리용)."""
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y%m%d")
    removed = 0
    for p in log_dir.glob("run_*.log"):
        m = LOG_RE.match(p.name)
        if m and m.group(1) < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def backup_db(con: sqlite3.Connection, backup_dir: Path, now: datetime, keep: int) -> Path:
    """SQLite 백업 API로 쓰기 중에도 안전하게 복사하고, 최근 keep개만 남긴다."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"market_{stamp(now)}.sqlite"
    dst = sqlite3.connect(str(path))
    try:
        con.backup(dst)
    finally:
        dst.close()
    snaps = sorted(backup_dir.glob("market_*.sqlite"))
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return path
