"""최근 데이터 합치기(sync)와 업로드용 내보내기(export). 명세서 17장.

sync: market.sqlite(읽기 전용) -> backtest.sqlite. 확정값만(잠정값 제외), 이미 있는 (날짜, 종목)은 덮어쓰지 않음.
export: sync 후 SQLite 백업 API로 일관 복사 -> zip(DB 파일 하나만) -> 바탕화면 backtest_<마지막데이터일>.zip.
"""
from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from ..timeutil import stamp


def _cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def sync(bt_path: Path, market_path: Path, report_dir: Path, now: datetime) -> tuple[list[str], Path | None]:
    if not market_path.exists():
        return ["수집 DB(market.sqlite)가 없어 합치기를 건너뜁니다 (이 PC에서는 정기 수집을 하지 않음)."], None
    from .. import backfill as B
    src = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    dst = _open_dst(bt_path)
    dst.execute("PRAGMA busy_timeout=30000")
    lines, skipped_conv = [], []
    try:
        # 1. 일별 시세: KRX 확정값만 (KIS 잠정값 제외)
        last = dst.execute("SELECT MAX(date) FROM bt_daily").fetchone()[0] or ""
        rows = src.execute(
            "SELECT p.*, s.name AS sname FROM price_daily p LEFT JOIN stock_master s ON s.code=p.code"
            " WHERE p.date > ? AND p.is_provisional=0 AND p.source='KRX'", (last,)).fetchall()
        has_cmp = "cmpprevdd_prc" in rows[0].keys() if rows else True
        if not has_cmp:
            skipped_conv.append("price_daily에 cmpprevdd_prc 없음: 수정계수 계산에서 빠짐")
        n = _insert(dst, "bt_daily", rows, lambda r: (
            r["date"], r["code"], r["market"], r["sname"], None, r["open"], r["high"], r["low"], r["close"],
            r["cmpprevdd_prc"] if has_cmp else None, r["change_rate_pct"], r["volume_sh"], r["value_krw"],
            r["mktcap_krw"], r["listed_shares"], 1.0))
        lines.append(_line("일별 시세", rows, n, "date"))
        added_daily = n
        # 2. 지수: KRX 확정값만
        last = dst.execute("SELECT MAX(date) FROM bt_index").fetchone()[0] or ""
        rows = src.execute("SELECT * FROM index_daily WHERE date > ? AND is_provisional=0 AND source='KRX'", (last,)).fetchall()
        n = _insert(dst, "bt_index", rows, lambda r: (
            r["date"], r["series"], r["index_name"], r["index_class"], r["open"], r["high"], r["low"], r["close"],
            r["change_rate_pct"], r["volume_unconfirmed"], r["value_unconfirmed"], r["mktcap_unconfirmed"]))
        lines.append(_line("지수", rows, n, "date"))
        # 3. 종목기본정보 (보통주 판별용)
        last = dst.execute("SELECT MAX(date) FROM bt_base_info").fetchone()[0] or ""
        rows = src.execute("SELECT * FROM krx_base_daily WHERE date > ?", (last,)).fetchall()
        n = _insert(dst, "bt_base_info", rows, lambda r: (
            r["date"], r["code"], r["market"], r["name"], r["listed_date"], r["secugrp"], r["sect_tp"], r["kind"]))
        lines.append(_line("종목기본정보", rows, n, "date"))
        # 4. 종목별 수급: 확정값만
        last = dst.execute("SELECT MAX(date) FROM bt_investor").fetchone()[0] or ""
        rows = src.execute("SELECT * FROM investor_daily WHERE date > ? AND is_provisional=0", (last,)).fetchall()
        n = _insert(dst, "bt_investor", rows, lambda r: (
            r["date"], r["code"], r["frgn_net_qty"], r["orgn_net_qty"], r["prsn_net_qty"],
            r["frgn_net_amt_mil"], r["orgn_net_amt_mil"], r["prsn_net_amt_mil"]))
        lines.append(_line("종목별 수급", rows, n, "date"))
        # 5. DART 분기 재무 (+ 기업코드)
        corp = src.execute("SELECT * FROM dart_corp").fetchall()
        _insert(dst, "dart_corp", corp, lambda r: tuple(r[k] for k in r.keys()))
        qcols = _cols(src, "dart_fs_quarter")
        if qcols != _cols(dst, "dart_fs_quarter"):
            skipped_conv.append("dart_fs_quarter 컬럼 구성이 달라 합치지 않음")
            lines.append("DART 분기 재무: 컬럼 구성이 달라 건너뜀")
        else:
            rows = src.execute("SELECT * FROM dart_fs_quarter").fetchall()
            n = _insert(dst, "dart_fs_quarter", rows, lambda r: tuple(r[k] for k in qcols))
            lines.append(f"DART 분기 재무: 새 {n}행 (기존 행은 덮어쓰지 않음)")
        skipped_conv.append("시장별 수급(market_investor_daily)은 백테스트 DB에 표가 없어 합치지 않음")
        # 수정계수·종목표 다시 계산
        if added_daily:
            B.compute_adj_factors(dst)
            B._update_stock_table(dst)
    finally:
        src.close()
        dst.close()
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"backtest_sync_{stamp(now)}.md"
    path.write_text("\n".join([f"# 최근 데이터 합치기 {now.strftime('%Y-%m-%d %H:%M')}", "",
                               "확정값만 가져오고(잠정값 제외), 이미 있는 날짜·종목은 덮어쓰지 않았습니다.", ""]
                              + [f"- {l}" for l in lines] + ["", "## 변환하지 못한 항목", ""]
                              + [f"- {x}" for x in skipped_conv]) + "\n", encoding="utf-8")
    return lines + [f"변환하지 못한 항목: {x}" for x in skipped_conv], path


def _open_dst(path: Path):
    from .. import backfill as B
    from .. import db
    con = db.connect(path)
    db.init_db(con)
    con.executescript(B.BT_SCHEMA)
    return con


def _insert(dst, table, rows, conv) -> int:
    if not rows:
        return 0
    n_cols = len(_cols(dst, table))
    vals = [conv(r) for r in rows]
    vals = [v for v in vals if len(v) == n_cols]
    before = dst.total_changes
    dst.execute("BEGIN")
    dst.executemany(f"INSERT OR IGNORE INTO {table} VALUES({','.join('?' * n_cols)})", vals)
    dst.execute("COMMIT")
    return dst.total_changes - before


def _line(name, rows, n, key):
    if not rows:
        return f"{name}: 새 0행"
    ds = sorted(r[key] for r in rows)
    return f"{name}: {ds[0]}~{ds[-1]}, 새 {n}행 (이미 있어 건너뜀 {len(rows) - n}행)"


def export(bt_path: Path, desktop: Path, now: datetime) -> Path:
    """SQLite 백업 API로 일관 복사(백필 중에도 안전) 후 DB 파일 하나만 zip. 키·설정 파일은 넣지 않는다."""
    src = sqlite3.connect(f"file:{bt_path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        last = src.execute("SELECT MAX(date) FROM bt_daily").fetchone()[0] or stamp(now)[:8]
        desktop.mkdir(parents=True, exist_ok=True)
        out = desktop / f"backtest_{last}.zip"
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "backtest.sqlite"
            dst = sqlite3.connect(str(copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
            with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(copy, "backtest.sqlite")
    finally:
        src.close()
    return out
