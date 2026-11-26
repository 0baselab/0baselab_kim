"""DART 주기 수집 (명세서 9장, 14.4). 주 1회·월 1회 수동 실행용. 시간 제한 없음.

흐름: corpCode.xml -> list.json(정기보고서 + 전체 공시 제목) -> 새 정기보고서마다 fnlttSinglAcntAll(CFS, 013이면 OFS)
-> 주요 계정 분기 단독 값 환산(dart_fs_quarter).
- 하루 호출 수는 DART_DAILY_LIMIT(기본 18,000)로 제한하고, 다 쓰거나 020이 오면 멈춘 뒤 다음 실행 때 이어서 받는다.
- 결산월이 12월이 아닌 회사는 보고서 이름의 (YYYY.MM)로 분기를 정할 수 없어 재무를 받지 않고 기록만 한다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import db
from .dart import NO_DATA, DartClient, DartLimitError
from .http import ApiError
from .krx_collect import to_int
from .rawstore import save_raw

_save_raw_file = save_raw

log = logging.getLogger("collector")

PERIODIC_RE = re.compile(r"(분기보고서|반기보고서|사업보고서)\s*\((\d{4})\.(\d{2})\)")
# 12월 결산 기준: 보고서 종류 + 기간 끝 월 -> (보고서 코드, 분기)
REPORT_MAP = {("분기보고서", "03"): ("11013", 1), ("반기보고서", "06"): ("11012", 2),
              ("분기보고서", "09"): ("11014", 3), ("사업보고서", "12"): ("11011", 4)}
INITIAL_YEARS = 3
LIST_CHUNK_DAYS = 90
TITLES_INITIAL_DAYS = 30
STATUS_OK = "000"


class DartBudgetExhausted(Exception):
    pass


@dataclass
class DartStats:
    calls: int = 0
    filings: int = 0
    periodic_new: int = 0
    fs_ok: int = 0
    fs_nodata: int = 0
    fs_failed: int = 0
    fs_dedup: int = 0  # 같은 기간 정정본이라 따로 부르지 않은 접수번호 수
    skipped_fy: int = 0
    quarters: int = 0
    remaining: int = 0
    stopped: str | None = None
    errors: list = field(default_factory=list)


class Budget:
    def __init__(self, con, day: str, limit: int):
        self.con, self.key, self.limit = con, f"dart_calls_{day}", limit

    def used(self) -> int:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
        return int(r[0]) if r else 0

    def take(self) -> None:
        if self.used() >= self.limit:
            raise DartBudgetExhausted(f"오늘 DART 호출 {self.limit}회를 다 써서 멈춤. 다음 실행 때 이어서 받습니다.")
        self.con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (self.key, str(self.used() + 1)))


def classify(report_nm: str) -> tuple[str | None, str | None, int | None]:
    """(fiscal_year, reprt_code, quarter). 정기보고서가 아니거나 12월 결산으로 해석 안 되면 None."""
    m = PERIODIC_RE.search(report_nm or "")
    if not m:
        return None, None, None
    hit = REPORT_MAP.get((m.group(1), m.group(3)))
    if not hit:
        return m.group(2), None, None
    return m.group(2), hit[0], hit[1]


def _days(a: str, b: str):
    d, e = datetime.strptime(a, "%Y%m%d"), datetime.strptime(b, "%Y%m%d")
    while d <= e:
        end = min(d + timedelta(days=LIST_CHUNK_DAYS - 1), e)
        yield d.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        d = end + timedelta(days=1)


def run(con, dart: DartClient, root, now_fn: Callable[[], datetime], limit: int, target_codes: set[str],
        progress: Callable[[str], None] = lambda s: None, list_start: str | None = None,
        titles: bool = True, min_year: str | None = None, save_raw_files: bool = True) -> DartStats:
    """list_start: 첫 실행의 정기공시 목록 시작일(기본 3년 전). titles=False면 전체 공시 제목은 받지 않음.
    min_year: 이보다 이른 사업연도 보고서는 재무를 받지 않음. save_raw_files=False면 원본 응답 파일을 남기지 않음."""
    global save_raw
    if not save_raw_files:
        save_raw = lambda *a, **k: None  # noqa: E731 (백필: 원본은 검증·저장 후 버린다)
    try:
        return _run(con, dart, root, now_fn, limit, target_codes, progress, list_start, titles, min_year)
    finally:
        save_raw = _save_raw_file


def _run(con, dart, root, now_fn, limit, target_codes, progress, list_start, titles, min_year) -> DartStats:
    st = DartStats()
    now = now_fn()
    today = now.strftime("%Y%m%d")
    budget = Budget(con, today, limit)
    touched: set[tuple[str, str]] = set()
    try:
        _update_corps(con, dart, root, now, budget, st)
        _update_filings(con, dart, root, now_fn, budget, st, target_codes, progress, list_start, titles)
        _fetch_statements(con, dart, root, now_fn, budget, st, target_codes, progress, touched, min_year)
    except (DartBudgetExhausted, DartLimitError) as exc:
        st.stopped = str(exc)
    finally:
        # 한도로 멈춰도 그날 받은 것과, 예전에 받아 두고 계산되지 않은 것을 모두 분기 계산한다
        st.quarters += recompute_pending(con, touched, now_fn().isoformat(timespec="seconds"))
    st.remaining = con.execute(
        "SELECT COUNT(*) FROM dart_filings f WHERE is_periodic=1 AND reprt_code IS NOT NULL AND stock_code IN "
        f"({','.join('?' * len(target_codes)) or 'NULL'}) AND NOT EXISTS (SELECT 1 FROM collect_log c WHERE c.source='DART'"
        " AND c.dataset='fs' AND c.unit_key=f.rcept_no AND c.status IN ('success','empty'))",
        sorted(target_codes)).fetchone()[0]
    return st


def _update_corps(con, dart, root, now, budget, st) -> None:
    today = now.strftime("%Y%m%d")
    if db.get_status(con, "DART", "corp_code", today) == "success":
        return
    budget.take()
    st.calls += 1
    corps = dart.corp_list()
    ts = now.isoformat(timespec="seconds")
    with db.Tx(con):
        for c in corps:
            if c["corp_code"]:
                con.execute("INSERT OR REPLACE INTO dart_corp VALUES(?,?,?,?,?)",
                            (c["corp_code"], c["stock_code"] or None, c["corp_name"], c["modify_date"], ts))
        db.set_status(con, "DART", "corp_code", today, "success", ts)


def _update_filings(con, dart, root, now_fn, budget, st, target_codes, progress, list_start=None, titles=True) -> None:
    now = now_fn()
    today = now.strftime("%Y%m%d")
    last = con.execute("SELECT value FROM meta WHERE key='dart_list_until'").fetchone()
    first_run = last is None
    start_periodic = (list_start or (now - timedelta(days=365 * INITIAL_YEARS + 120)).strftime("%Y%m%d")) \
        if first_run else last[0]
    start_titles = (now - timedelta(days=TITLES_INITIAL_DAYS)).strftime("%Y%m%d") if first_run else last[0]
    # 정기공시(A)는 3년치(+보고서 제출 기한 여유), 전체 공시 제목은 첫 실행 30일, 이후 마지막 실행일부터
    jobs = [("A", a, b) for a, b in _days(start_periodic, today)]
    if titles:
        jobs += [(None, a, b) for a, b in _days(start_titles, today)]
    for ty, a, b in jobs:
        key = f"{ty or 'ALL'}|{a}|{b}"
        if b < today and db.get_status(con, "DART", "list", key) == "success":
            continue
        page, total, failed = 1, 1, False
        while page <= total:
            budget.take()
            st.calls += 1
            body = dart.filings(a, b, page, pblntf_ty=ty)
            ts = now_fn().isoformat(timespec="seconds")
            status = body.get("status")
            if status == NO_DATA:
                break
            if status != STATUS_OK:
                st.errors.append(f"list.json {key} page {page}: {status} {body.get('message')}")
                with db.Tx(con):
                    db.set_status(con, "DART", "list", key, "failed", ts, f"{status} {body.get('message')}")
                failed = True
                break
            raw = save_raw(root, "dart", f"list_{ty or 'ALL'}", b, body, now_fn())
            total = int(body.get("total_page") or 1)
            with db.Tx(con):
                for r in body.get("list") or []:
                    _save_filing(con, r, ty, ts, raw, st)
            page += 1
        if not failed:
            with db.Tx(con):
                db.set_status(con, "DART", "list", key, "success", now_fn().isoformat(timespec="seconds"))
        progress(f"  DART 공시목록 {a}~{b} ({ty or '전체'})")
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('dart_list_until', ?)", (today,))


def _save_filing(con, r, ty, ts, raw, st) -> None:
    rcept_no = (r.get("rcept_no") or "").strip()
    if not rcept_no:
        return
    name = (r.get("report_nm") or "").strip()
    year, reprt, _ = classify(name)
    periodic = 1 if PERIODIC_RE.search(name) else 0
    exists = con.execute("SELECT 1 FROM dart_filings WHERE rcept_no=?", (rcept_no,)).fetchone()
    con.execute("INSERT OR REPLACE INTO dart_filings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rcept_no, r.get("corp_code"), (r.get("stock_code") or "").strip() or None, r.get("corp_name"), name,
                 r.get("rcept_dt"), ty or "ALL", 1 if "정정" in name else 0, periodic, year, reprt, ts, raw))
    if not exists:
        st.filings += 1
        if periodic:
            st.periodic_new += 1


def _fetch_statements(con, dart, root, now_fn, budget, st, target_codes, progress, touched: set,
                      min_year: str | None = None) -> None:
    rows = con.execute("SELECT * FROM dart_filings WHERE is_periodic=1 ORDER BY rcept_dt DESC, rcept_no DESC").fetchall()
    # fnlttSinglAcntAll은 (회사, 연도, 보고서 코드)만 받으므로 같은 기간의 정정본은 응답이 같다.
    # 아직 안 받은 접수번호가 있는 기간만 한 번 부르고, 그 기간의 미수집 접수번호를 모두 같은 결과로 기록한다.
    # 마지막으로 받은 뒤 새 정정 공시가 생기면 그 접수번호가 미수집이므로 기간 전체를 다시 받는다.
    groups: dict[tuple, list] = {}
    for f in rows:
        if f["stock_code"] not in target_codes:
            continue
        if f["reprt_code"] is None:
            st.skipped_fy += 1
            continue
        if min_year and (f["fiscal_year"] or "") < min_year:
            continue
        if db.get_status(con, "DART", "fs", f["rcept_no"]) in ("success", "empty"):
            continue
        groups.setdefault((f["corp_code"], f["fiscal_year"], f["reprt_code"]), []).append(f)
    todo = sorted(groups.values(), key=lambda g: (g[0]["rcept_dt"] or "", g[0]["rcept_no"]), reverse=True)
    for i, group in enumerate(todo, 1):
        f = group[0]  # 접수일이 가장 늦은 공시 (rows가 접수일 내림차순)
        if i % 100 == 0:
            progress(f"  DART 재무 {i}/{len(todo)}")
        got = None
        for fs_div in ("CFS", "OFS"):
            budget.take()
            st.calls += 1
            try:
                body = dart.fs_all(f["corp_code"], f["fiscal_year"], f["reprt_code"], fs_div)
            except ApiError as exc:
                body = {"status": "ERR", "message": str(exc)[:200]}
            if body.get("status") == NO_DATA:  # 연결 없음 -> 별도. 오류가 아님 (9.1)
                continue
            got = (fs_div, body)
            break
        now = now_fn()
        ts = now.isoformat(timespec="seconds")
        if got is None:
            with db.Tx(con):
                for g in group:
                    db.set_status(con, "DART", "fs", g["rcept_no"], "empty", ts, "CFS·OFS 모두 013")
            st.fs_nodata += 1
            continue
        fs_div, body = got
        raw = save_raw(root, "dart", f"fs_{f['stock_code']}_{f['reprt_code']}", f["rcept_dt"] or now.strftime("%Y%m%d"),
                       body, now)
        if body.get("status") != STATUS_OK:
            with db.Tx(con):
                for g in group:
                    db.set_status(con, "DART", "fs", g["rcept_no"], "failed", ts,
                                  f"{body.get('status')} {body.get('message')}", raw)
            st.fs_failed += 1
            continue
        # 응답의 rcept_no가 목록과 다를 수 있다(같은 기간의 최신 정정본). 응답 기준으로 저장한다.
        with db.Tx(con):
            for r in body.get("list") or []:
                rn = r.get("rcept_no") or f["rcept_no"]
                con.execute("INSERT OR REPLACE INTO dart_fs_raw VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (rn, fs_div, r.get("sj_div") or "", r.get("account_id") or "", to_int(r.get("ord")) or 0,
                             r.get("account_nm"), r.get("thstrm_nm"), r.get("thstrm_amount"), r.get("thstrm_add_amount"),
                             r.get("currency"), ts, raw))
            # 같은 기간에 정정본이 있으면 접수일이 가장 늦은 보고서를 분기 계산에 쓴다
            src_key = f"dart_fs_src|{f['corp_code']}|{f['fiscal_year']}|{f['reprt_code']}|{fs_div}"
            rn = (body.get("list") or [{}])[0].get("rcept_no") or f["rcept_no"]
            cur = con.execute("SELECT value FROM meta WHERE key=?", (src_key,)).fetchone()
            new_val = f"{f['rcept_dt'] or ''}|{rn}"
            if cur is None or new_val >= cur[0]:
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (src_key, new_val))
            for g in group:
                db.set_status(con, "DART", "fs", g["rcept_no"], "success", ts, None, raw)
        st.fs_ok += 1
        st.fs_dedup += len(group) - 1
        touched.add((f["corp_code"], f["fiscal_year"]))


def missing_quarter_pairs(con) -> set[tuple[str, str]]:
    """재무 원문은 받아 두었는데(dart_fs_src) 분기 계산(dart_fs_quarter)이 없는 (회사, 연도)."""
    have = {(r[0], r[1]) for r in con.execute("SELECT DISTINCT corp_code, fiscal_year FROM dart_fs_quarter")}
    out = set()
    for (key,) in con.execute("SELECT key FROM meta WHERE key LIKE 'dart_fs_src|%'"):
        _, corp, year, *_ = key.split("|")
        if (corp, year) not in have:
            out.add((corp, year))
    return out


def recompute_pending(con, touched: set, ts: str) -> int:
    from .dart_quarter import recompute
    n = 0
    for corp, year in sorted(touched | missing_quarter_pairs(con)):
        with db.Tx(con):
            n += recompute(con, corp, year, ts)
    return n
