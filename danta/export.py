"""분석용 부분 DB 내보내기 (export).

기존 수집기 DB를 읽기 전용(mode=ro)으로 붙여(ATTACH) 필요한 행만 새 파일로 복사한다.
- date 컬럼 있는 테이블: date >= since 행만 (형식 YYYYMMDD / YYYY-MM-DD를 표본으로 판단)
- date 컬럼 없는 테이블: nodate_limit 행 이하면 통째로, 초과면 건너뜀
- 원본 응답(JSON 원문) 같은 큰 텍스트 컬럼이 있는 테이블은 건너뜀
- 결과가 max_mb를 넘으면 큰 테이블 목록을 출력하고 실패 (자동으로 잘라내지 않음, 결과 파일은 지움)
"""
from __future__ import annotations

import fnmatch
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


# 분석용 프로필: 표 → 열 목록 (None이면 전체). '*'가 들어간 항목은 패턴으로 원본 열을 고른다.
ANALYSIS_PROFILE: dict[str, list[str] | None] = {
    "price_daily": ["date", "code", "market", "open", "high", "low", "close", "change_rate_pct", "volume_sh",
                    "value_krw", "mktcap_krw", "listed_shares", "is_provisional"],
    "universe_daily": ["date", "code", "in_universe", "exclude_reason", "is_admin", "is_halted",
                       "days_since_listing", "is_new_listing"],
    "investor_daily": ["date", "code", "frgn_net_qty", "orgn_net_qty", "prsn_net_qty", "frgn_net_amt_mil",
                       "orgn_net_amt_mil", "prsn_net_amt_mil", "is_provisional"],
    "market_investor_daily": ["date", "market", "*_unconfirmed", "index_close", "is_provisional"],
    "index_daily": ["date", "series", "index_name", "index_class", "open", "high", "low", "close",
                    "change_rate_pct", "is_provisional"],
    "adj_price_daily": ["code", "date", "as_of", "open", "high", "low", "close"],
    "trading_calendar": ["date", "is_open"],
    "stock_master": ["code", "name", "market", "listed_date", "krx_secugrp", "krx_sect_tp", "krx_kind",
                     "is_admin", "is_halted", "first_seen", "last_seen"],
    "theme_membership": None,
    "theme_versions": None,
}
PROFILES = ("analysis", "all")


def pick_columns(wanted: list[str] | None, src_cols: list[str]) -> tuple[list[str], list[str]]:
    """(복사할 열, 원본에 없는 열). 패턴은 원본 열 순서대로 펼친다."""
    if wanted is None:
        return list(src_cols), []
    out, missing = [], []
    for w in wanted:
        if "*" in w:
            hits = [c for c in src_cols if fnmatch.fnmatchcase(c, w) and c not in out]
            if hits:
                out += hits
            else:
                missing.append(w)
        elif w in src_cols:
            if w not in out:
                out.append(w)
        else:
            missing.append(w)
    return out, missing


def _create_sql(con, table: str, cols: list[str]) -> str:
    """원본 열 타입을 유지한 CREATE TABLE. 기본키 열이 모두 남아 있으면 기본키도 유지."""
    info = {r[1]: r for r in con.execute(f"PRAGMA src.table_info({_q(table)})")}
    defs = [f"{_q(c)} {info[c][2]}".strip() for c in cols]
    pk = [r[1] for r in sorted(info.values(), key=lambda r: r[5]) if r[5] > 0]
    if pk and all(c in cols for c in pk):
        defs.append(f"PRIMARY KEY ({', '.join(_q(c) for c in pk)})")
    return f"CREATE TABLE {_q(table)} ({', '.join(defs)})"


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


def _date_where(con, name: str, cols: list[str], total: int, since: str, since10: str, info: dict):
    """date 필터. 반환 (where, params) 또는 형식 판단 불가면 None."""
    if "date" not in cols:
        return "", ()
    sample = [r[0] for r in con.execute(f"SELECT date FROM src.{_q(name)} WHERE date IS NOT NULL LIMIT {SAMPLE}")]
    style = date_style(sample)
    if total and sample and style is None:
        info["skip"] = f"date 형식 판단 불가 (표본 예: {sample[:3]})"
        return None
    info["date_style"] = {"8": "YYYYMMDD", "10": "YYYY-MM-DD", None: "(값 없음)"}[style]
    return " WHERE date >= ?", ((since10 if style == "10" else since),)


def export_subset(src_path: Path, out_path: Path, log: Log, since: str = "20250901", max_mb: float = 380,
                  nodate_limit: int = NODATE_LIMIT, profile: str = "analysis") -> dict:
    if profile not in PROFILES:
        raise ExportError(f"--profile은 {', '.join(PROFILES)} 중 하나여야 합니다: {profile}")
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
    log(f"[export] 프로필 {profile}, 기준 날짜 date >= {since}, 파일 한도 {max_mb:g}MB"
        + (f", 무날짜 테이블 한도 {nodate_limit:,}행" if profile == "all" else ""))
    src_tables = [r[0] for r in con.execute("SELECT name FROM src.sqlite_master WHERE type='table'"
                                            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    targets = src_tables if profile == "all" else list(ANALYSIS_PROFILE)
    result = []
    try:
        for name in targets:
            if name not in src_tables:
                log(f"  경고: {name} 표가 원본에 없어 건너뜀")
                result.append({"table": name, "total": 0, "copied": 0, "cols": [], "skip": "원본에 없음"})
                continue
            src_cols = [r[1] for r in con.execute(f"PRAGMA src.table_info({_q(name)})")]
            total = con.execute(f"SELECT COUNT(*) FROM src.{_q(name)}").fetchone()[0]
            if profile == "all":
                cols, missing = src_cols, []
            else:
                cols, missing = pick_columns(ANALYSIS_PROFILE[name], src_cols)
                for m in missing:
                    log(f"  경고: {name}.{m} 열이 원본에 없어 건너뜀")
            info = {"table": name, "total": total, "copied": 0, "cols": cols, "skip": None, "missing": missing}
            where, params = "", ()
            raw = raw_json_column(con, name, cols) if profile == "all" else None
            if raw:
                info["skip"] = f"원본 응답(JSON 원문)으로 보이는 큰 텍스트 컬럼 {raw}"
            elif not cols:
                info["skip"] = "복사할 열 없음"
            else:
                w = _date_where(con, name, cols, total, since, since10, info)
                if w is not None:
                    where, params = w
                    if profile == "all" and "date" not in cols and total > nodate_limit:
                        info["skip"] = f"date 컬럼 없음, {total:,}행 > {nodate_limit:,}행"
            if not info["skip"]:
                col_sql = ", ".join(_q(c) for c in cols)
                con.execute(_create_sql(con, name, cols))
                con.execute(f"INSERT INTO main.{_q(name)} ({col_sql}) SELECT {col_sql} FROM src.{_q(name)}{where}",
                            params)
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
        sizes = dict(_table_sizes(con))
        log("[export] 표별 행 수와 크기")
        for i in result:
            if not i["skip"]:
                log(f"    {i['table']}: {i['copied']:,}행, {sizes.get(i['table'], 0) / 1024 / 1024:,.1f}MB")
        investor_summary(con, log)
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


def investor_summary(con, log: Log) -> dict | None:
    """investor_daily 날짜별 종목 수(최소·평균·최대)와 price_daily 대비 수급 보유 비율."""
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "investor_daily" not in names:
        log("[export] investor_daily 없음 → 수급 요약 생략")
        return None
    mn, avg, mx, nd = con.execute(
        "SELECT MIN(n), AVG(n), MAX(n), COUNT(*) FROM (SELECT date, COUNT(*) n FROM investor_daily GROUP BY date)"
    ).fetchone()
    out = {"min": mn, "avg": avg, "max": mx, "days": nd}
    log(f"[export] investor_daily 날짜별 종목 수: 최소 {mn or 0:,}, 평균 {avg or 0:,.1f}, 최대 {mx or 0:,} ({nd}일)")
    if "price_daily" in names:
        tot, hit = con.execute(
            "SELECT COUNT(*), SUM(i.code IS NOT NULL) FROM price_daily p"
            " LEFT JOIN investor_daily i ON i.date=p.date AND i.code=p.code").fetchone()
        out["ratio_all"] = (hit or 0) / tot if tot else None
        log(f"[export] price_daily 대비 수급 있는 비율: {hit or 0:,} / {tot:,}"
            + (f" ({(hit or 0) / tot * 100:.1f}%)" if tot else ""))
        if "universe_daily" in names:
            tot, hit = con.execute(
                "SELECT COUNT(*), SUM(i.code IS NOT NULL) FROM price_daily p"
                " JOIN universe_daily u ON u.date=p.date AND u.code=p.code AND u.in_universe=1"
                " LEFT JOIN investor_daily i ON i.date=p.date AND i.code=p.code").fetchone()
            out["ratio_universe"] = (hit or 0) / tot if tot else None
            log(f"[export]   보통주 범위(in_universe=1)만: {hit or 0:,} / {tot:,}"
                + (f" ({(hit or 0) / tot * 100:.1f}%)" if tot else ""))
    return out
