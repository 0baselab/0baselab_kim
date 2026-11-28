"""백테스트용 10년 과거 데이터 백필 (명세서 16장). 매매·주문 기능 없음.

- 저장: 데이터 폴더/backtest/backtest.sqlite (별도 DB). 수집 DB(market.sqlite)는 읽기 전용으로만 연다.
- 원본 응답 파일을 남기지 않는다. 응답을 검증해 DB에 넣고 버린다.
- 진행 상태는 backtest.sqlite의 bt_job 표에 기록해 끊겨도 이어받는다. 단위마다 트랜잭션.
- 빈 값은 NULL, 금액 컬럼은 _krw/_mil. 단위 미확인 값은 _unconfirmed.
- 정기 수집과 같은 잠금 파일(system/collector.lock)을 써서 동시에 돌지 않는다.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import db
from .http import ApiClient, ApiError
from .kis import INVESTOR_BY_STOCK, KisAuthError, KisClient, investor_params, investor_window_ok, is_ok
from .krx import BASE_INFO_PATHS, ROWS_KEY, TRADE_PATHS, KrxClient
from .krx_collect import to_float, to_int, valid_code
from .secrets import redact
from .timeutil import stamp

log = logging.getLogger("collector")

START = "20160101"
DART_MIN_YEAR = "2015"
DART_LIST_START = "20160101"  # 2015년 사업보고서(2016년 3월 제출)부터
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_SETTING = {"KOSPI": "KRX_INDEX_KOSPI_URL", "KOSDAQ": "KRX_INDEX_KOSDAQ_URL"}
KIS_PAGE_DAYS = 30
BIG_MKTCAP_KRW = 200_000_000_000   # 2,000억 원
BIG_VALUE20_KRW = 5_000_000_000    # 20일 평균 거래대금 50억 원
DELISTED_PROBE = 5
DELISTED_GAP_DAYS = 60  # 마지막 거래일이 전체 마지막 날보다 이만큼 이전이면 상장폐지로 봄
BIG_WINDOW = 20

BT_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_job (
    kind TEXT NOT NULL, unit_key TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    cursor TEXT, rows INTEGER, last_error TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, unit_key));
CREATE TABLE IF NOT EXISTS bt_daily (
    date TEXT NOT NULL, code TEXT NOT NULL, market TEXT, name TEXT, sect_tp TEXT,
    open REAL, high REAL, low REAL, close REAL, cmpprevdd_prc REAL, change_rate_pct REAL,
    volume_sh INTEGER, value_krw INTEGER, mktcap_krw INTEGER, listed_shares INTEGER,
    adj_factor REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (date, code));
CREATE INDEX IF NOT EXISTS bt_daily_code ON bt_daily(code, date);
CREATE TABLE IF NOT EXISTS bt_index (
    date TEXT NOT NULL, series TEXT NOT NULL, index_name TEXT NOT NULL, index_class TEXT,
    open REAL, high REAL, low REAL, close REAL, change_rate_pct REAL,
    volume_unconfirmed INTEGER, value_unconfirmed INTEGER, mktcap_unconfirmed INTEGER,
    PRIMARY KEY (date, series, index_name));
CREATE TABLE IF NOT EXISTS bt_base_info (
    date TEXT NOT NULL, code TEXT NOT NULL, market TEXT, name TEXT, listed_date TEXT,
    secugrp TEXT, sect_tp TEXT, kind TEXT, PRIMARY KEY (date, code));
CREATE TABLE IF NOT EXISTS bt_stock (
    code TEXT PRIMARY KEY, name TEXT, market TEXT, first_date TEXT, last_date TEXT,
    is_common INTEGER, common_method TEXT, priority INTEGER);
CREATE TABLE IF NOT EXISTS bt_corp_event (
    code TEXT NOT NULL, date TEXT NOT NULL, base_price REAL, prev_close REAL, ratio REAL,
    PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS bt_investor (
    date TEXT NOT NULL, code TEXT NOT NULL,
    frgn_net_qty INTEGER, orgn_net_qty INTEGER, prsn_net_qty INTEGER,
    frgn_net_amt_mil INTEGER, orgn_net_amt_mil INTEGER, prsn_net_amt_mil INTEGER,
    PRIMARY KEY (date, code));
"""


class BackfillStop(Exception):
    pass


# ------------------------------------------------------------------ DB
def bt_path(root: Path) -> Path:
    return root / "backtest" / "backtest.sqlite"


def open_bt(root: Path) -> sqlite3.Connection:
    """백테스트 DB. DART 재사용을 위해 수집 스키마(collect_log, dart_* 등)도 함께 만든다(이 파일 안에만)."""
    con = db.connect(bt_path(root))
    db.init_db(con)
    con.executescript(BT_SCHEMA)
    return con


def open_collect_ro(root: Path) -> sqlite3.Connection | None:
    p = root / "db" / "market.sqlite"
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def job_status(con, kind, unit):
    r = con.execute("SELECT status, cursor FROM bt_job WHERE kind=? AND unit_key=?", (kind, unit)).fetchone()
    return (r["status"], r["cursor"]) if r else (None, None)


def set_job(con, kind, unit, status, now, cursor=None, rows=None, error=None):
    prev = con.execute("SELECT attempts FROM bt_job WHERE kind=? AND unit_key=?", (kind, unit)).fetchone()
    con.execute("INSERT OR REPLACE INTO bt_job VALUES(?,?,?,?,?,?,?,?)",
                (kind, unit, status, (prev["attempts"] if prev else 0) + 1, cursor, rows,
                 redact(error)[:300] if error else None, now))


def weekdays(start: str, end: str) -> list[str]:
    """요청할 날짜: 주말은 장이 서지 않으므로 빼고, 평일은 모두 불러 빈 응답이면 휴장으로 기록한다."""
    d, e = datetime.strptime(start, "%Y%m%d").date(), datetime.strptime(end, "%Y%m%d").date()
    out = []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


@dataclass
class Report:
    kind: str
    started: datetime
    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    stopped: str | None = None

    def add(self, s):
        self.lines.append(s)
        log.info(s)


# ------------------------------------------------------------------ KRX
PREF_NAME = re.compile(r"(우[A-C]?|우\(전환\)|[0-9]우[A-C]?)$")
NOT_COMMON_WORDS = ("스팩", "리츠", "인프라", "부동산", "선박", "ETN", "ETF")


def name_rule_common(name: str, code: str) -> bool:
    """종목명·코드 규칙: 우선주(…우, …우B, (전환)), 스팩, 리츠·인프라 등은 보통주가 아님."""
    n = (name or "").strip()
    if PREF_NAME.search(n) or any(w in n for w in NOT_COMMON_WORDS):
        return False
    return code[-1:] == "0"  # 보통주 단축코드는 끝자리가 0 (우선주 5·7·9·K 등)


def krx_backfill(con, krx: KrxClient, settings, now_fn, out, end: str | None = None) -> Report:
    rep = Report("krx", now_fn())
    end = end or (now_fn().date() - timedelta(days=1)).strftime("%Y%m%d")  # 당일은 장 마감 후에도 KRX가 안 줌
    days = weekdays(START, end)
    units = [("trade_" + m, d) for d in days for m in MARKETS] + [("index_" + m, d) for d in days for m in MARKETS]
    todo = [(k, d) for k, d in units if job_status(con, "krx_" + k, d)[0] not in ("success", "empty")]
    rep.add(f"KRX 기간 {START}~{end}, 평일 {len(days)}일, 남은 호출 {len(todo)}회")
    ok = empty = fail = 0
    try:
        for i, (k, d) in enumerate(sorted(todo, key=lambda x: (x[1], x[0]))):
            if i % 200 == 0:
                out(f"  KRX {i}/{len(todo)} ({d})")
            kind, market = k.split("_")
            ts = now_fn().isoformat(timespec="seconds")
            try:
                if kind == "trade":
                    body = krx.get(TRADE_PATHS[market], d)
                else:
                    url = (settings.get(INDEX_SETTING[market]) or "").strip()
                    if not url:
                        continue
                    body = krx.get_url(url, d)
            except ApiError as exc:
                if "인증 실패" in str(exc):
                    raise BackfillStop(str(exc))
                with db.Tx(con):
                    set_job(con, "krx_" + k, d, "failed", ts, error=str(exc))
                fail += 1
                rep.failures.append(f"{k} {d}: {exc}")
                continue
            rows = body.get(ROWS_KEY)
            if not isinstance(rows, list):
                with db.Tx(con):
                    set_job(con, "krx_" + k, d, "failed", ts, error=f"응답 형식 다름 {list(body)[:4]}")
                fail += 1
                rep.failures.append(f"{k} {d}: 응답 형식 다름")
                continue
            with db.Tx(con):
                if rows:
                    (_save_trade if kind == "trade" else _save_index)(con, d, market, rows)
                set_job(con, "krx_" + k, d, "success" if rows else "empty", ts, rows=len(rows))
            ok, empty = ok + bool(rows), empty + (not rows)
            _record_size_after_one_year(con, rep)
    except KeyboardInterrupt:
        rep.stopped = "사용자 중단 (받은 날짜는 저장됨, 다음 실행 때 이어받기)"
    rep.add(f"KRX 이번 실행: 성공 {ok}, 빈 응답(휴장 등) {empty}, 실패 {fail}")
    if rep.stopped is None:
        _base_info_and_common(con, krx, now_fn, out, rep)
        n = compute_adj_factors(con)
        rep.add(f"수정계수: 기준가 조정 이벤트 {n}건 반영")
        _update_stock_table(con)
    return rep


def _save_trade(con, d, market, rows):
    for r in rows:
        code = str(r.get("ISU_CD") or "").strip()
        if not valid_code(code):
            continue
        con.execute(
            "INSERT OR REPLACE INTO bt_daily(date, code, market, name, sect_tp, open, high, low, close, cmpprevdd_prc,"
            " change_rate_pct, volume_sh, value_krw, mktcap_krw, listed_shares) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d, code, market, str(r.get("ISU_NM") or "").strip() or None, str(r.get("SECT_TP_NM") or "").strip() or None,
             to_float(r.get("TDD_OPNPRC")), to_float(r.get("TDD_HGPRC")), to_float(r.get("TDD_LWPRC")),
             to_float(r.get("TDD_CLSPRC")), to_float(r.get("CMPPREVDD_PRC")), to_float(r.get("FLUC_RT")),
             to_int(r.get("ACC_TRDVOL")), to_int(r.get("ACC_TRDVAL")), to_int(r.get("MKTCAP")), to_int(r.get("LIST_SHRS"))))


def _save_index(con, d, market, rows):
    for r in rows:
        name = str(r.get("IDX_NM") or "").strip()
        if name:
            con.execute("INSERT OR REPLACE INTO bt_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d, market, name, r.get("IDX_CLSS"), to_float(r.get("OPNPRC_IDX")), to_float(r.get("HGPRC_IDX")),
                         to_float(r.get("LWPRC_IDX")), to_float(r.get("CLSPRC_IDX")), to_float(r.get("FLUC_RT")),
                         to_int(r.get("ACC_TRDVOL")), to_int(r.get("ACC_TRDVAL")), to_int(r.get("MKTCAP"))))


def _record_size_after_one_year(con, rep):
    if con.execute("SELECT value FROM meta WHERE key='bt_size_1y'").fetchone():
        return
    n = con.execute("SELECT COUNT(*) FROM bt_job WHERE kind='krx_trade_KOSPI' AND status='success'").fetchone()[0]
    if n >= 245:
        size = db_size(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES('bt_size_1y', ?)", (f"{size}|{n}",))
        rep.add(f"KRX 1년치({n}거래일) 직후 DB 크기 {size / 1e6:.1f}MB")


def db_size(con) -> int:
    pc = con.execute("PRAGMA page_count").fetchone()[0]
    ps = con.execute("PRAGMA page_size").fetchone()[0]
    return pc * ps


def _base_info_and_common(con, krx, now_fn, out, rep):
    """보통주 판별. 먼저 종목기본정보를 과거 basDd로 부를 수 있는지 확인하고,
    되면 매월 첫 거래일 기준으로, 안 되면 현재 기준 정보 + 종목명 규칙으로 판별한다."""
    method = con.execute("SELECT value FROM meta WHERE key='bt_common_method'").fetchone()
    if method is None:
        first = con.execute("SELECT MIN(date) FROM bt_daily").fetchone()[0]
        ok = False
        if first:
            ok = _fetch_base(con, krx, first, now_fn, rep) > 0
        method_v = "monthly_base_info" if ok else "current_base_info+name_rule"
        con.execute("INSERT OR REPLACE INTO meta VALUES('bt_common_method', ?)", (method_v,))
        rep.add(f"종목기본정보 과거 basDd({first}) 조회: {'가능' if ok else '불가 (빈 응답)'} -> 판별 방법 {method_v}")
    else:
        method_v = method[0]
    if method_v == "monthly_base_info":
        months = [r[0] for r in con.execute(
            "SELECT MIN(date) FROM bt_daily GROUP BY substr(date,1,6) ORDER BY 1")]
        last = con.execute("SELECT MAX(date) FROM bt_daily").fetchone()[0]
        targets = sorted(set(months + ([last] if last else [])))
    else:
        last = con.execute("SELECT MAX(date) FROM bt_daily").fetchone()[0]
        targets = [last] if last else []
    for d in targets:
        if job_status(con, "krx_base", d)[0] in ("success", "empty"):
            continue
        _fetch_base(con, krx, d, now_fn, rep)


def _fetch_base(con, krx, d, now_fn, rep) -> int:
    total = 0
    with db.Tx(con):
        for m in MARKETS:
            try:
                body = krx.get(BASE_INFO_PATHS[m], d)
            except ApiError as exc:
                rep.failures.append(f"base_info {m} {d}: {exc}")
                continue
            for r in body.get(ROWS_KEY) or []:
                code = str(r.get("ISU_SRT_CD") or "").strip()
                if valid_code(code):
                    con.execute("INSERT OR REPLACE INTO bt_base_info VALUES(?,?,?,?,?,?,?,?)",
                                (d, code, m, str(r.get("ISU_ABBRV") or "").strip(), r.get("LIST_DD"),
                                 r.get("SECUGRP_NM"), r.get("SECT_TP_NM"), r.get("KIND_STKCERT_TP_NM")))
                    total += 1
        set_job(con, "krx_base", d, "success" if total else "empty", now_fn().isoformat(timespec="seconds"), rows=total)
    return total


def compute_adj_factors(con) -> int:
    """기준가 조정일 = (종가 − 전일대비) ≠ 그 종목의 직전 종가. 비율 = 기준가 ÷ 직전 종가.
    수정계수 = 그 날 이후 모든 조정 비율의 곱 (최근 가격 기준 연속 시계열: 수정종가 = 종가 × 수정계수)."""
    events: dict[str, list[tuple[str, float, float, float]]] = {}
    prev: dict[str, float] = {}
    for r in con.execute("SELECT code, date, close, cmpprevdd_prc FROM bt_daily ORDER BY code, date"):
        c = r["code"]
        if c in prev and r["close"] is not None and r["cmpprevdd_prc"] is not None and prev[c]:
            base = r["close"] - r["cmpprevdd_prc"]
            if abs(base - prev[c]) > 0.5 and base > 0:
                events.setdefault(c, []).append((r["date"], base, prev[c], base / prev[c]))
        if r["close"] is not None:
            prev[c] = r["close"]
    with db.Tx(con):
        con.execute("DELETE FROM bt_corp_event")
        con.execute("UPDATE bt_daily SET adj_factor=1.0 WHERE adj_factor != 1.0")
        for c, evs in events.items():
            for d, base, pc, ratio in evs:
                con.execute("INSERT INTO bt_corp_event VALUES(?,?,?,?,?)", (c, d, base, pc, ratio))
            factor = 1.0
            bounds = []  # (이 날짜 이전 행에 적용할 계수)
            for d, _, _, ratio in reversed(evs):
                factor *= ratio
                bounds.append((d, factor))
            # 가장 오래된 구간부터: date < 첫 이벤트 -> 전체 곱, ...
            prev_d = None
            for d, f in reversed(bounds):
                if prev_d is None:
                    con.execute("UPDATE bt_daily SET adj_factor=? WHERE code=? AND date < ?", (f, c, d))
                else:
                    con.execute("UPDATE bt_daily SET adj_factor=? WHERE code=? AND date >= ? AND date < ?", (f, c, prev_d, d))
                prev_d = d
    return sum(len(v) for v in events.values())


def _update_stock_table(con):
    """종목별 첫·마지막 거래일, 보통주 여부(판별 방법 기록), KIS 우선순위."""
    method = (con.execute("SELECT value FROM meta WHERE key='bt_common_method'").fetchone() or ["name_rule"])[0]
    base = {}
    for r in con.execute("SELECT code, secugrp, kind, sect_tp, name FROM bt_base_info ORDER BY date"):
        base[r["code"]] = r  # 가장 최근 기준일 정보로 덮어씀
    big = _ever_big(con)
    with db.Tx(con):
        con.execute("DELETE FROM bt_stock")
        for r in con.execute("SELECT code, MAX(name) AS name, MAX(market) AS market, MIN(date) AS f, MAX(date) AS l,"
                             " MAX(CASE WHEN sect_tp LIKE 'SPAC%' THEN 1 ELSE 0 END) AS spac FROM bt_daily GROUP BY code").fetchall():
            b = base.get(r["code"])
            if b is not None:
                common = int(b["secugrp"] == "주권" and b["kind"] == "보통주" and b["sect_tp"] != "SPAC(소속부없음)"
                             and "스팩" not in (b["name"] or ""))
                how = f"종목기본정보({method})"
            else:
                common = int(name_rule_common(r["name"], r["code"]) and not r["spac"])
                how = "종목명·코드 규칙 (기본정보에 없음)"
            con.execute("INSERT INTO bt_stock VALUES(?,?,?,?,?,?,?,?)",
                        (r["code"], r["name"], r["market"], r["f"], r["l"], common, how, 1 if r["code"] in big else 2))


def _ever_big(con) -> set[str]:
    """기간 중 한 번이라도 '시가총액 2,000억 이상 + 직전 20일 평균 거래대금 50억 이상'인 종목."""
    out, cur, window = set(), None, []
    for r in con.execute("SELECT code, mktcap_krw, value_krw FROM bt_daily ORDER BY code, date"):
        if r["code"] != cur:
            cur, window = r["code"], []
        if cur in out:
            continue
        window.append(r["value_krw"])
        if len(window) > BIG_WINDOW:
            window.pop(0)
        if (r["mktcap_krw"] or 0) >= BIG_MKTCAP_KRW and len(window) == BIG_WINDOW and None not in window \
                and sum(window) / BIG_WINDOW >= BIG_VALUE20_KRW:
            out.add(cur)
    return out


# ------------------------------------------------------------------ KIS
INV_COLS = {"frgn_net_qty": "frgn_ntby_qty", "orgn_net_qty": "orgn_ntby_qty", "prsn_net_qty": "prsn_ntby_qty",
            "frgn_net_amt_mil": "frgn_ntby_tr_pbmn", "orgn_net_amt_mil": "orgn_ntby_tr_pbmn",
            "prsn_net_amt_mil": "prsn_ntby_tr_pbmn"}


def kis_targets(con) -> list:
    return con.execute("SELECT * FROM bt_stock WHERE is_common=1 ORDER BY priority, last_date DESC, code").fetchall()


def kis_remaining_calls(con) -> int:
    n = 0
    for s in kis_targets(con):
        st, cursor = job_status(con, "kis", s["code"])
        if st in ("success", "empty"):
            continue
        hi = cursor or s["last_date"]
        lo = max(s["first_date"], START)
        if hi < lo:
            continue
        days = con.execute("SELECT COUNT(DISTINCT date) FROM bt_daily WHERE code=? AND date BETWEEN ? AND ?",
                           (s["code"], lo, hi)).fetchone()[0]
        n += max(1, -(-days // KIS_PAGE_DAYS))
    return n


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def _kis_call(kis, code, d):
    body = kis.get(INVESTOR_BY_STOCK, investor_params(code, d)).body  # 시장 J, FID_ORG_ADJ_PRC=0
    rows = [r for r in (body.get("output2") or []) if r.get("stck_bsop_date")]
    return body, rows


BLOCKED_MSG = "KIS가 지금 시간대를 막고 있음(OPSQ2001 TIME LIMIT). 받은 것은 저장했고, 15:40~23:59에 다시 실행하면 이어받습니다."


def kis_backfill(con, kis: KisClient, now_fn, out, ignore_window: bool = False) -> Report:
    """ignore_window=True면 15:40~23:59 시간 제한 없이 시작한다. OPSQ2001이 오면 즉시 저장하고 멈춘다(재시도 없음)."""
    rep = Report("kis", now_fn())
    if ignore_window:
        rep.add(f"--ignore-window: 시간 제한 없이 시작 ({now_fn().strftime('%H:%M')})")
    if not ignore_window and not investor_window_ok(now_fn()):
        rep.stopped = (f"실행 시각 {now_fn().strftime('%H:%M')}: KIS 투자자매매동향은 00:00~15:40에 막힙니다(OPSQ2001). "
                       "15:40~23:59에 실행하세요.")
        rep.add(rep.stopped)
        return rep
    targets = kis_targets(con)
    if not targets:
        rep.stopped = "대상 종목이 없습니다. 먼저 backfill krx를 끝내세요."
        rep.add(rep.stopped)
        return rep
    _probe_delisted(con, kis, now_fn, rep)
    rep.add(f"KIS 대상 보통주 {len(targets)}개 (우선 {sum(1 for t in targets if t['priority'] == 1)}개), "
            f"남은 예상 호출 {kis_remaining_calls(con)}회")
    calls = rows_saved = done = 0
    try:
        for i, s in enumerate(targets):
            code = s["code"]
            st, cursor = job_status(con, "kis", code)
            if st in ("success", "empty"):
                continue
            lo = max(s["first_date"], START)
            q = cursor or s["last_date"]
            while True:
                if not ignore_window and not investor_window_ok(now_fn()):
                    raise BackfillStop(f"{now_fn().strftime('%H:%M')} 조회 가능 시각(15:40~23:59)을 벗어나 멈춤. 다음 실행 때 이어받기")
                if q < lo:
                    with db.Tx(con):
                        set_job(con, "kis", code, "success", now_fn().isoformat(timespec="seconds"), cursor=q)
                    done += 1
                    break
                try:
                    body, rows = _kis_call(kis, code, q)
                except KisAuthError:
                    raise
                except ApiError as exc:
                    with db.Tx(con):
                        set_job(con, "kis", code, "failed", now_fn().isoformat(timespec="seconds"), cursor=q, error=str(exc))
                    rep.failures.append(f"{code} {q}: {exc}")
                    break
                calls += 1
                ts = now_fn().isoformat(timespec="seconds")
                if not is_ok(body):
                    if body.get("msg_cd") == "OPSQ2001":
                        raise BackfillStop(BLOCKED_MSG if ignore_window else "KIS OPSQ2001 TIME LIMIT — 15:40 이후에 다시 실행하세요")
                    with db.Tx(con):
                        set_job(con, "kis", code, "failed", ts, cursor=q, error=f"{body.get('msg_cd')} {body.get('msg1')}")
                    rep.failures.append(f"{code} {q}: {body.get('msg_cd')} {body.get('msg1')}")
                    break
                rows = [r for r in rows if r["stck_bsop_date"] <= q and r["stck_bsop_date"] >= START]
                with db.Tx(con):
                    for r in rows:
                        con.execute("INSERT OR REPLACE INTO bt_investor VALUES(?,?,?,?,?,?,?,?)",
                                    (r["stck_bsop_date"], code, *[to_int(r.get(src)) for src in INV_COLS.values()]))
                    if not rows:
                        # 더 과거 자료가 없음: 이 종목은 여기까지 (받은 게 하나도 없으면 empty)
                        had = con.execute("SELECT 1 FROM bt_investor WHERE code=? LIMIT 1", (code,)).fetchone()
                        set_job(con, "kis", code, "success" if had else "empty", ts, cursor=q,
                                error=None if had else "응답 없음")
                    else:
                        q = _prev_day(min(r["stck_bsop_date"] for r in rows))
                        set_job(con, "kis", code, "running", ts, cursor=q, rows=len(rows))
                rows_saved += len(rows)
                if not rows:
                    done += 1
                    break
            if i % 20 == 0:
                out(f"  KIS {i}/{len(targets)} ({code}), 이번 실행 호출 {calls}")
    except BackfillStop as exc:
        rep.stopped = str(exc)
    except KeyboardInterrupt:
        rep.stopped = "사용자 중단 (받은 구간은 저장됨, 다음 실행 때 이어받기)"
    rep.add(f"KIS 이번 실행: 호출 {calls}회, 저장 {rows_saved}행, 끝난 종목 {done}개, 한도 초과 {len(kis.rate_limit_events)}회")
    return rep


def _probe_delisted(con, kis, now_fn, rep):
    """상장폐지 종목 3~5개로 응답 여부를 먼저 확인 (한 번만)."""
    if con.execute("SELECT value FROM meta WHERE key='bt_delisted_probe'").fetchone():
        rep.add("상장폐지 종목 확인: " + con.execute("SELECT value FROM meta WHERE key='bt_delisted_probe'").fetchone()[0])
        return
    last_all = con.execute("SELECT MAX(date) FROM bt_daily").fetchone()[0]
    cutoff = (datetime.strptime(last_all, "%Y%m%d") - timedelta(days=DELISTED_GAP_DAYS)).strftime("%Y%m%d")
    picks = con.execute("SELECT code, name, last_date FROM bt_stock WHERE is_common=1 AND last_date < ?"
                        " ORDER BY last_date DESC LIMIT ?", (cutoff, DELISTED_PROBE)).fetchall()
    res = []
    for p in picks:
        try:
            body, rows = _kis_call(kis, p["code"], p["last_date"])
            res.append(f"{p['code']} {p['name']}(마지막 거래 {p['last_date']}): "
                       f"{body.get('msg_cd')}, {len(rows)}행" + (f", {min(r['stck_bsop_date'] for r in rows)}~{max(r['stck_bsop_date'] for r in rows)}" if rows else ""))
        except ApiError as exc:
            res.append(f"{p['code']}: 오류 {exc}")
    any_ok = any(", 0행" not in x and "오류" not in x for x in res)
    summary = ("응답 있음" if any_ok else "응답 없음 (폐지 종목 수급은 비게 됨, 그대로 진행)") + " | " + " ; ".join(res)
    con.execute("INSERT OR REPLACE INTO meta VALUES('bt_delisted_probe', ?)", (summary,))
    rep.add("상장폐지 종목 확인: " + summary)


# ------------------------------------------------------------------ DART
def dart_remaining_limit(collect_con, settings, today: str) -> tuple[int, int]:
    """(백필에 쓸 수 있는 오늘 한도, 수집 DB에서 오늘 이미 쓴 호출 수)."""
    limit = int(settings.get("DART_DAILY_LIMIT") or 18000)
    used = 0
    if collect_con is not None:
        try:
            r = collect_con.execute("SELECT value FROM meta WHERE key=?", (f"dart_calls_{today}",)).fetchone()
            used = int(r[0]) if r else 0
        except sqlite3.Error:
            used = 0
    return max(0, limit - used), used


def dart_backfill(con, dart, root, collect_con, settings, now_fn, out, reserve: int = 0) -> Report:
    """reserve: 정기 DART 수집용으로 남겨 둘 호출 수 (backfill auto는 BACKFILL_DART_RESERVE)."""
    from .dart_collect import run
    rep = Report("dart", now_fn())
    today = now_fn().strftime("%Y%m%d")
    allowed, used = dart_remaining_limit(collect_con, settings, today)
    allowed = max(0, allowed - reserve)
    targets = {r[0] for r in con.execute("SELECT code FROM bt_stock WHERE is_common=1")}
    if not targets:
        rep.stopped = "대상 종목이 없습니다. 먼저 backfill krx를 끝내세요."
        rep.add(rep.stopped)
        return rep
    rep.add(f"DART 오늘 한도: 설정 {settings.get('DART_DAILY_LIMIT') or 18000} − 정기 수집 사용 {used} − 예비 {reserve}"
            f" = 백필 {allowed}회 "
            "(백필이 오늘 이미 쓴 호출도 이 안에서 셈)")
    try:
        st = run(con, dart, root, now_fn, allowed, targets, progress=out, list_start=DART_LIST_START,
                 titles=False, min_year=DART_MIN_YEAR, save_raw_files=False)
    except KeyboardInterrupt:
        rep.stopped = "사용자 중단"
        return rep
    rep.add(f"DART: 호출 {st.calls}회, 재무 받음 {st.fs_ok}(정정본 중복 생략 {st.fs_dedup}), 실패 {st.fs_failed}, "
            f"분기 계산 {st.quarters}행, 남은 정기보고서 {st.remaining}건")
    rep.failures += st.errors
    rep.stopped = st.stopped
    return rep


# ------------------------------------------------------------------ 상태·보고서
def status_lines(con, collect_con, settings, today: str) -> list[str]:
    L = []
    days = con.execute("SELECT COUNT(DISTINCT unit_key) FROM bt_job WHERE kind LIKE 'krx_trade_%'").fetchone()[0]
    for k in ("krx_trade_KOSPI", "krx_trade_KOSDAQ", "krx_index_KOSPI", "krx_index_KOSDAQ"):
        r = con.execute("SELECT SUM(status='success'), SUM(status='empty'), SUM(status='failed'), MAX(unit_key)"
                        " FROM bt_job WHERE kind=?", (k,)).fetchone()
        L.append(f"{k}: 성공 {r[0] or 0}, 빈 응답 {r[1] or 0}, 실패 {r[2] or 0}, 마지막 {r[3] or '-'}")
    total_days = len(weekdays(START, today))
    L.append(f"KRX 진행: {days}/{total_days} 평일 (남은 예상 호출 {max(0, (total_days - days) * 4)}회)")
    size = db_size(con)
    one = con.execute("SELECT value FROM meta WHERE key='bt_size_1y'").fetchone()
    if one:
        s1, n1 = map(int, one[0].split("|"))
        L.append(f"DB 크기 {size / 1e6:.1f}MB, 1년치({n1}거래일) 직후 {s1 / 1e6:.1f}MB -> 10년 예상 약 {s1 * 10 / 1e9:.2f}GB (KRX만, 수급·재무 별도)")
    else:
        L.append(f"DB 크기 {size / 1e6:.1f}MB (10년 예상은 KRX 1년치를 받은 뒤 표시)")
    m = con.execute("SELECT value FROM meta WHERE key='bt_common_method'").fetchone()
    L.append(f"보통주 판별 방법: {m[0] if m else '아직 없음'}")
    tg = con.execute("SELECT COUNT(*), SUM(priority=1) FROM bt_stock WHERE is_common=1").fetchone()
    kd = con.execute("SELECT SUM(status IN ('success','empty')), SUM(status='failed') FROM bt_job WHERE kind='kis'").fetchone()
    L.append(f"KIS 수급: 대상 {tg[0] or 0}개(우선 {tg[1] or 0}), 끝난 종목 {kd[0] or 0}, 실패 {kd[1] or 0}, "
             f"남은 예상 호출 {kis_remaining_calls(con)}회 (초당 10건이면 약 {kis_remaining_calls(con) / 10 / 3600:.1f}시간)")
    probe = con.execute("SELECT value FROM meta WHERE key='bt_delisted_probe'").fetchone()
    if probe:
        L.append(f"상장폐지 종목 수급 확인: {probe[0]}")
    left = con.execute("SELECT COUNT(*) FROM dart_filings f WHERE is_periodic=1 AND reprt_code IS NOT NULL AND fiscal_year >= ?"
                       " AND NOT EXISTS (SELECT 1 FROM collect_log c WHERE c.source='DART' AND c.dataset='fs'"
                       " AND c.unit_key=f.rcept_no AND c.status IN ('success','empty'))", (DART_MIN_YEAR,)).fetchone()[0]
    q = con.execute("SELECT COUNT(*) FROM dart_fs_quarter").fetchone()[0]
    allowed, used = dart_remaining_limit(collect_con, settings, today)
    bt_used = con.execute("SELECT value FROM meta WHERE key=?", (f"dart_calls_{today}",)).fetchone()
    L.append(f"DART: 분기 재무 {q}행, 남은 정기보고서 {left}건, 오늘 남은 한도 {max(0, allowed - int(bt_used[0] if bt_used else 0))}회")
    for kind in ("krx", "kis", "dart"):
        r = con.execute("SELECT unit_key, last_error, updated_at FROM bt_job WHERE kind LIKE ? AND last_error IS NOT NULL"
                        " AND status='failed' ORDER BY updated_at DESC LIMIT 1", (kind + "%",)).fetchone()
        if r:
            L.append(f"마지막 오류 ({kind}): {r['unit_key']} {r['last_error']} ({r['updated_at']})")
    return L


def write_report(rep: Report, con, out_dir: Path, now: datetime) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    L = [f"# 백필 보고서: {rep.kind} {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)", ""]
    if rep.stopped:
        L += [f"**멈춤:** {rep.stopped}", ""]
    L += ["## 이번 실행", ""] + [f"- {x}" for x in rep.lines]
    if rep.kind == "krx":
        r = con.execute("SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT code) FROM bt_daily").fetchone()
        L += ["", "## 받은 기간과 행 수", "", f"- 종목 일봉: {r[0]}~{r[1]}, {r[2]}행, 종목 {r[3]}개(상장폐지 포함)"]
        r = con.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM bt_index").fetchone()
        L.append(f"- 지수: {r[0]}~{r[1]}, {r[2]}행")
        empties = [x[0] for x in con.execute("SELECT unit_key FROM bt_job WHERE kind='krx_trade_KOSPI' AND status='empty' ORDER BY 1")]
        L += ["", f"## 빈 날짜 (평일인데 코스피 일별매매가 빈 응답: 휴장 또는 미제공) {len(empties)}일", "",
              ", ".join(empties) or "없음"]
        n_ev = con.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM bt_corp_event").fetchone()
        L += ["", f"- 기준가 조정 이벤트(수정계수 반영): {n_ev[0]}건, {n_ev[1]}종목",
              f"- 보통주: {con.execute('SELECT COUNT(*) FROM bt_stock WHERE is_common=1').fetchone()[0]}개 / 전체 "
              f"{con.execute('SELECT COUNT(*) FROM bt_stock').fetchone()[0]}개"]
    if rep.kind == "kis":
        r = con.execute("SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT code) FROM bt_investor").fetchone()
        L += ["", "## 받은 기간과 행 수", "", f"- 종목별 수급: {r[0]}~{r[1]}, {r[2]}행, {r[3]}종목"]
        probe = con.execute("SELECT value FROM meta WHERE key='bt_delisted_probe'").fetchone()
        L += ["", "## 상장폐지 종목 수급 확인", "", probe[0] if probe else "아직 안 함"]
    if rep.kind == "dart":
        r = con.execute("SELECT MIN(fiscal_year), MAX(fiscal_year), COUNT(*), COUNT(DISTINCT corp_code) FROM dart_fs_quarter").fetchone()
        L += ["", "## 받은 기간과 행 수", "", f"- 분기 재무: {r[0]}~{r[1]}년, {r[2]}행, {r[3]}개사"]
    L += ["", f"## 실패 목록 ({len(rep.failures)}건)", ""] + ([f"- {x}" for x in rep.failures[:200]] or ["없음"])
    path = out_dir / f"backfill_{rep.kind}_{stamp(now)}.md"
    path.write_text(redact("\n".join(L)) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ 자동 (backfill auto)
def krx_done(con, today: str) -> bool:
    """어제까지 모든 평일·4종류가 성공 또는 빈 응답이고, 보통주 판별이 끝났으면 True."""
    end = (datetime.strptime(today, "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
    need = len(weekdays(START, end)) * 4
    have = con.execute("SELECT COUNT(*) FROM bt_job WHERE (kind LIKE 'krx_trade_%' OR kind LIKE 'krx_index_%')"
                       " AND status IN ('success','empty') AND unit_key <= ?", (end,)).fetchone()[0]
    stocks = con.execute("SELECT COUNT(*) FROM bt_stock").fetchone()[0]
    return have >= need and stocks > 0


SHUTDOWN_CMD = ["shutdown", "/s", "/t", "60"]
ABORT_CMD = ["shutdown", "/a"]


@dataclass
class AutoResult:
    stages: list[tuple[str, str]] = field(default_factory=list)  # (단계, 결과)
    failed: list[str] = field(default_factory=list)
    reports: list[Path] = field(default_factory=list)


def run_auto(con, root, collect_con, settings, now_fn, out, make_krx, make_kis, make_dart,
             report_dir: Path, ignore_window: bool = False) -> AutoResult:
    """KRX(남았으면) -> KIS(15:40~23:59이고 남았으면, 23:59까지) -> DART(예비 한도 남김).
    한 단계가 오류로 멈춰도 다음 단계를 진행하고 실패 단계를 기록한다."""
    res = AutoResult()
    reserve = int(settings.get("BACKFILL_DART_RESERVE") or 3000)

    def stage(name, fn):
        out(f"\n=== {name} ===")
        try:
            rep = fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 인증 실패 등: 이 단계만 실패로 적고 다음으로
            log.exception("backfill auto %s 실패", name)
            res.failed.append(f"{name}: {type(exc).__name__}: {redact(str(exc))[:200]}")
            res.stages.append((name, "실패"))
            return
        res.reports.append(write_report(rep, con, report_dir, now_fn()))
        if rep.stopped and ("인증" in rep.stopped or "오류" in rep.stopped):
            res.failed.append(f"{name}: {rep.stopped}")
        res.stages.append((name, rep.stopped or "완료"))

    today = now_fn().strftime("%Y%m%d")
    if krx_done(con, today):
        res.stages.append(("KRX", "이미 끝남 (건너뜀)"))
    else:
        stage("KRX", lambda: krx_backfill(con, make_krx(), settings, now_fn, out))
    if not ignore_window and not investor_window_ok(now_fn()):
        res.stages.append(("KIS", f"{now_fn().strftime('%H:%M')}: 15:40~23:59가 아니어서 건너뜀"))
    elif kis_targets(con) and kis_remaining_calls(con) == 0:
        res.stages.append(("KIS", "이미 끝남 (건너뜀)"))
    else:
        stage("KIS", lambda: kis_backfill(con, make_kis(), now_fn, out, ignore_window=ignore_window))
    stage("DART", lambda: dart_backfill(con, make_dart(), root, collect_con, settings, now_fn, out, reserve=reserve))
    return res


def shutdown_countdown(run_cmd, out, sleep, seconds: int = 60, is_windows: bool | None = None) -> bool:
    """Windows 종료 예약(shutdown /s /t 60) 후 60초 카운트다운. Ctrl+C면 취소(shutdown /a). True = 종료 진행."""
    import sys as _sys
    is_windows = _sys.platform == "win32" if is_windows is None else is_windows
    if not is_windows:
        out("Windows가 아니어서 PC를 끄지 않습니다.")
        return False
    run_cmd(SHUTDOWN_CMD)
    out(f"{seconds}초 뒤 PC를 끕니다. 취소하려면 Ctrl+C를 누르세요.")
    try:
        for left in range(seconds, 0, -10):
            out(f"  {left}초 남음")
            sleep(min(10, left))
    except KeyboardInterrupt:
        run_cmd(ABORT_CMD)
        out("PC 끄기를 취소했습니다.")
        return False
    return True
