"""분석용 부분 DB 내보내기 (export).

기존 수집기 DB를 읽기 전용(mode=ro)으로 붙여(ATTACH) 필요한 행만 새 파일로 복사한다.
- date 컬럼 있는 테이블: date >= since 행만 (형식 YYYYMMDD / YYYY-MM-DD를 표본으로 판단)
- date 컬럼 없는 테이블: nodate_limit 행 이하면 통째로, 초과면 건너뜀
- 원본 응답(JSON 원문) 같은 큰 텍스트 컬럼이 있는 테이블은 건너뜀
- 결과가 max_mb를 넘으면 큰 테이블 목록을 출력하고 실패 (자동으로 잘라내지 않음, 결과 파일은 지움)
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]

DATE8 = re.compile(r"^\d{8}$")
DATE10 = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAMPLE = 200
RAW_AVG_CHARS = 2000      # 표본 평균 글자 수가 이 이상이고 JSON처럼 시작하면 원본 응답으로 본다
NODATE_LIMIT = 200_000


class ExportError(Exception):
    pass


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def date_style(values: list) -> str | None:
    """'8'(YYYYMMDD), '10'(YYYY-MM-DD), 판단 불가면 None."""
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return None
    if all(DATE8.match(v) for v in vals):
        return "8"
    if all(DATE10.match(v) for v in vals):
        return "10"
    return None


def raw_json_column(con, table: str, cols: list[str]) -> str | None:
    """원본 응답(JSON 원문)으로 보이는 큰 텍스트 컬럼 이름."""
    for c in cols:
        rows = [r[0] for r in con.execute(
            f"SELECT {_q(c)} FROM src.{_q(table)} WHERE typeof({_q(c)})='text' LIMIT {SAMPLE}")]
        if not rows:
            continue
        jsonish = [v for v in rows if v.lstrip()[:1] in ("{", "[")]
        if jsonish and len(jsonish) >= len(rows) / 2 and sum(map(len, rows)) / len(rows) >= RAW_AVG_CHARS:
            return c
    return None


def _table_sizes(con) -> list[tuple[str, int]]:
    try:
        rows = con.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC").fetchall()
        return [(n, int(s)) for n, s in rows]
    except sqlite3.OperationalError:  # dbstat이 없는 sqlite: 값 길이 합으로 추정
        out = []
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            cols = [r[1] for r in con.execute(f"PRAGMA main.table_info({_q(t)})")]
            expr = "+".join(f"COALESCE(length({_q(c)}),0)" for c in cols) or "0"
            out.append((t, int(con.execute(f"SELECT COALESCE(SUM({expr}),0) FROM {_q(t)}").fetchone()[0])))
        return sorted(out, key=lambda x: -x[1])


def export_subset(src_path: Path, out_path: Path, log: Log, since: str = "20250901", max_mb: float = 380,
                  nodate_limit: int = NODATE_LIMIT) -> dict:
    if not DATE8.match(since):
        raise ExportError(f"--since는 YYYYMMDD 형식이어야 합니다: {since}")
    if not src_path.exists():
        raise ExportError(f"기존 수집기 DB가 없습니다: {src_path}")
    since10 = f"{since[:4]}-{since[4:6]}-{since[6:]}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    for p in (tmp, out_path):  # 덮어쓰기: 이전 결과는 먼저 지운다 (실패 시 반쪽 파일이 남지 않게)
        p.unlink(missing_ok=True)

    # ATTACH에 URI(mode=ro)를 쓰려면 이 연결도 URI로 열어야 한다
    con = sqlite3.connect(tmp.resolve().as_uri(), uri=True)
    con.execute("ATTACH DATABASE ? AS src", (f"{src_path.resolve().as_uri()}?mode=ro",))
    log(f"[export] 원본(읽기 전용): {src_path}")
    log(f"[export] 기준 날짜 date >= {since}, 무날짜 테이블 한도 {nodate_limit:,}행, 파일 한도 {max_mb:g}MB")
    tables = con.execute("SELECT name, sql FROM src.sqlite_master WHERE type='table'"
                         " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    result = []
    try:
        for name, sql in tables:
            cols = [r[1] for r in con.execute(f"PRAGMA src.table_info({_q(name)})")]
            total = con.execute(f"SELECT COUNT(*) FROM src.{_q(name)}").fetchone()[0]
            info = {"table": name, "total": total, "copied": 0, "cols": cols, "skip": None}
            raw = raw_json_column(con, name, cols)
            where, params = "", ()
            if raw:
                info["skip"] = f"원본 응답(JSON 원문)으로 보이는 큰 텍스트 컬럼 {raw}"
            elif "date" in cols:
                sample = [r[0] for r in con.execute(
                    f"SELECT date FROM src.{_q(name)} WHERE date IS NOT NULL LIMIT {SAMPLE}")]
                style = date_style(sample)
                if total and style is None:
                    info["skip"] = f"date 형식 판단 불가 (표본 예: {sample[:3]})"
                else:
                    where, params = " WHERE date >= ?", ((since10 if style == "10" else since),)
                    info["date_style"] = {"8": "YYYYMMDD", "10": "YYYY-MM-DD", None: "(빈 테이블)"}[style]
            elif total > nodate_limit:
                info["skip"] = f"date 컬럼 없음, {total:,}행 > {nodate_limit:,}행"
            if not info["skip"]:
                con.execute(sql)  # 원래 테이블 정의 그대로 (기본키 포함). 별도 인덱스는 복사하지 않음
                con.execute(f"INSERT INTO main.{_q(name)} SELECT * FROM src.{_q(name)}{where}", params)
                info["copied"] = con.execute(f"SELECT COUNT(*) FROM main.{_q(name)}").fetchone()[0]
                con.commit()
            result.append(info)
            how = f"건너뜀: {info['skip']}" if info["skip"] else (
                f"date({info['date_style']}) 필터" if "date_style" in info else "통째로 복사")
            log(f"  {name}: {info['copied']:,} / {total:,}행, {how}")
            log(f"    컬럼: {', '.join(cols)}")
        con.execute("DETACH DATABASE src")
        con.execute("VACUUM")
        size = tmp.stat().st_size
        mb = size / 1024 / 1024
        log(f"[export] 결과 파일 크기: {mb:,.1f}MB ({size:,} bytes)")
        if mb > max_mb:
            log(f"[export] 실패: {max_mb:g}MB를 넘었습니다. 큰 테이블부터:")
            for t, s in _table_sizes(con)[:15]:
                log(f"    {t}: {s / 1024 / 1024:,.1f}MB")
            log("[export] --since를 늦추거나 테이블을 줄여 다시 실행하세요. 결과 파일은 지웠습니다.")
            con.close()
            tmp.unlink(missing_ok=True)
            return {"ok": False, "size": size, "tables": result}
        con.close()
        tmp.replace(out_path)
        log(f"[export] 완료: {out_path}")
        return {"ok": True, "size": size, "tables": result}
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
