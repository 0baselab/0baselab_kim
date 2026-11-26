"""과거 데이터 조회 가능 기간 실측 (verify history). 추세추종 백테스트 준비용.

- 수집 기능이 아니다. market.sqlite·collect_log에 아무것도 쓰지 않는다(DB는 있으면 읽기 전용으로만 연다).
- DB가 없는 PC에서도 돈다: 데이터 폴더·DB를 새로 만들지 않는다.
- 총 호출 MAX_CALLS(200) 이하. KIS는 KisClient의 균등 간격·한도 초과 시 감속을 그대로 쓴다.
- 보고서에 키·계좌번호·토큰을 쓰지 않는다(redact).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from ..dart import DartClient
from ..http import ApiClient, ApiError
from ..kis import INVESTOR_BY_STOCK, KisClient, investor_params, investor_window_ok, is_ok
from ..krx import ROWS_KEY, TRADE_PATHS, KrxClient
from ..secrets import redact
from ..timeutil import stamp
from ..version import __version__
from .common import md_table

MAX_CALLS = 200
KIS_OFFSETS_MONTHS = [3, 6, 12, 24, 36, 60, 84, 120]
KRX_YEARS = [1, 3, 5, 10, 15]
DART_YEARS = ["2015", "2018", "2020"]
SAMSUNG_CORP_CODE = "00126380"  # 실측보고서(2026-11-19)에서 확인
KIS_FIXED = ["005930", "247540"]
# DB가 없을 때 쓰는 코스닥 소형주 고정 코드 (강스템바이오텍, 코스닥 기술성장기업부). 응답으로 확인한다.
FALLBACK_SMALLCAP = "217730"
KRX_RETRY = 5
HAS_DATA_WINDOW_DAYS = 62  # 요청일 이전 이 기간 안의 행이 있으면 "그 시점 데이터 있음"


class CallBudgetExceeded(Exception):
    pass


@dataclass
class Call:
    api: str
    target: str
    req: str
    rows: int | None = None
    first: str | None = None
    last: str | None = None
    code: str = ""
    empty: bool | None = None
    note: str = ""


@dataclass
class Ctx:
    now: datetime
    kis: KisClient | None
    krx: KrxClient | None
    dart: DartClient | None
    settings: dict
    con: sqlite3.Connection | None
    calls: list[Call] = field(default_factory=list)
    summary: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def count(self) -> None:
        if len(self.calls) >= MAX_CALLS:
            raise CallBudgetExceeded(f"총 호출 {MAX_CALLS}회에 도달해 멈춤")


# ---------------------------------------------------------------- 날짜 도우미
def months_ago(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - n, 12)
    m += 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31,
                      30, 31][m - 1])
    return date(y, m, day)


def prev_weekday(d: date) -> date:
    """d 이전(불포함) 평일. DB 거래일 달력이 없을 때의 근사."""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def prev_trading_day(con, d: date) -> tuple[date, bool]:
    """d 직전 거래일. (날짜, 달력 사용 여부). 달력이 그 시점을 덮지 않으면 평일로 근사."""
    if con is not None:
        try:
            return _prev_from_calendar(con, d) or (prev_weekday(d), False)
        except sqlite3.Error:
            pass
    return prev_weekday(d), False


def _prev_from_calendar(con, d: date):
    if True:
        ds = d.strftime("%Y%m%d")
        lo = (d - timedelta(days=10)).strftime("%Y%m%d")
        covered = con.execute("SELECT COUNT(*) FROM trading_calendar WHERE date BETWEEN ? AND ?", (lo, ds)).fetchone()[0]
        if covered:
            r = con.execute("SELECT MAX(date) FROM trading_calendar WHERE is_open=1 AND date < ?", (ds,)).fetchone()[0]
            if r:
                return datetime.strptime(r, "%Y%m%d").date(), True
    return None


def search_oldest_month(has_data: Callable[[int], bool], offsets: list[int]) -> tuple[int | None, int | None, dict]:
    """offsets(개월 전, 오름차순)를 차례로 확인해 데이터가 있는 가장 먼 offset(L)과 그 다음 없는 offset(H)을 찾고,
    L~H 사이를 이분 탐색해 데이터가 있는 가장 먼 '개월 전'을 돌려준다. (결과, 처음 없는 offset, 확인 기록)
    결과 None = 3개월 전도 없음. 처음 없는 offset None = 가장 먼 offset까지 모두 있음."""
    seen: dict[int, bool] = {}

    def ok(n):
        if n not in seen:
            seen[n] = has_data(n)
        return seen[n]

    low, high = None, None
    for n in offsets:
        if ok(n):
            low = n
        else:
            high = n
            break
    if low is None or high is None:
        return low, high, seen
    while high - low > 1:
        mid = (low + high) // 2
        if ok(mid):
            low = mid
        else:
            high = mid
    return low, high, seen


# ---------------------------------------------------------------- 대상 종목
def pick_smallcap(con) -> tuple[str, str]:
    """유니버스 중 시가총액 하위 10% 코스닥 종목 1개 (하위 10% 구간의 가운데). DB가 없으면 고정 종목."""
    if con is not None:
        try:
            day = con.execute("SELECT MAX(date) FROM universe_daily").fetchone()[0]
            rows = con.execute(
                "SELECT u.code, p.mktcap_krw FROM universe_daily u JOIN stock_master s ON s.code=u.code"
                " JOIN price_daily p ON p.code=u.code AND p.date=u.date"
                " WHERE u.date=? AND u.in_universe=1 AND s.market='KOSDAQ' AND p.mktcap_krw IS NOT NULL"
                " ORDER BY p.mktcap_krw, u.code", (day,)).fetchall() if day else []
        except sqlite3.Error:
            rows = []
        if rows:
            bottom = rows[: max(1, len(rows) // 10)]
            code, cap = bottom[len(bottom) // 2]
            return code, f"DB 자동 선택 ({day} 코스닥 대상 종목 {len(rows)}개 중 시가총액 하위 10% 구간 가운데, {cap:,}원)"
    return FALLBACK_SMALLCAP, "고정 종목 사용 (수집 DB 없음)"


def open_db_readonly(db_path: Path) -> sqlite3.Connection | None:
    """DB 파일이 있을 때만 읽기 전용으로 연다. 없으면 만들지 않는다."""
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- KIS
def _kis_probe(ctx: Ctx, code: str, req: date) -> tuple[bool, Call]:
    ctx.count()
    ds = req.strftime("%Y%m%d")
    c = Call("KIS 투자자매매동향 FHPTJ04160001", code, ds)
    try:
        body = ctx.kis.get(INVESTOR_BY_STOCK, investor_params(code, ds)).body
    except ApiError as exc:
        c.code, c.empty, c.note = "ERROR", None, str(exc)[:120]
        ctx.calls.append(c)
        return False, c
    c.code = f"{body.get('rt_cd')}/{body.get('msg_cd')}"
    dates = sorted(r["stck_bsop_date"] for r in (body.get("output2") or []) if r.get("stck_bsop_date"))
    c.rows = len(dates)
    c.first, c.last = (dates[0], dates[-1]) if dates else (None, None)
    c.empty = not dates
    if not is_ok(body):
        c.note = str(body.get("msg1") or "")[:80]
    lo = (req - timedelta(days=HAS_DATA_WINDOW_DAYS)).strftime("%Y%m%d")
    near = [d for d in dates if lo <= d <= ds]
    if dates and not near:
        c.note = (c.note + " 요청일 근처 행 없음").strip()
    ctx.calls.append(c)
    return is_ok(body) and bool(near), c


def kis_history(ctx: Ctx, code: str, label: str) -> None:
    today = ctx.now.date()
    oldest_seen: dict[int, str] = {}

    def has_data(n: int) -> bool:
        req, _ = prev_trading_day(ctx.con, months_ago(today, n) + timedelta(days=1))
        ok, c = _kis_probe(ctx, code, req)
        if ok and c.first:
            oldest_seen[n] = c.first
        return ok

    low, high, _ = search_oldest_month(has_data, KIS_OFFSETS_MONTHS)
    if low is None:
        result = "3개월 전도 데이터 없음"
    elif high is None:
        result = f"{KIS_OFFSETS_MONTHS[-1]}개월 전까지 모두 있음 (가장 오래된 응답 {oldest_seen.get(low, '-')})"
    else:
        m = months_ago(today, low)
        result = f"{m.strftime('%Y-%m')} (약 {low}개월 전, 응답의 가장 오래된 날 {oldest_seen.get(low, '-')})"
    ctx.summary.append(["KIS 투자자매매동향", f"{code} {label}", result])


# ---------------------------------------------------------------- KRX
def krx_history(ctx: Ctx) -> None:
    today = ctx.now.date()
    targets = [("KRX 유가증권 일별매매 stk_bydd_trd", lambda d: ctx.krx.get(TRADE_PATHS["KOSPI"], d)),
               ("KRX 코스닥 일별매매 ksq_bydd_trd", lambda d: ctx.krx.get(TRADE_PATHS["KOSDAQ"], d))]
    for name, key in (("KRX 코스피 지수 kospi_dd_trd", "KRX_INDEX_KOSPI_URL"),
                      ("KRX 코스닥 지수 kosdaq_dd_trd", "KRX_INDEX_KOSDAQ_URL")):
        url = (ctx.settings.get(key) or "").strip()
        if url:
            targets.append((name, lambda d, u=url: ctx.krx.get_url(u, d)))
        else:
            ctx.summary.append([name, "-", "설정 필요 (URL 비어 있음)"])
    for name, fetch in targets:
        oldest = None
        for years in KRX_YEARS:
            d, from_cal = prev_trading_day(ctx.con, months_ago(today, years * 12) + timedelta(days=1))
            for attempt in range(KRX_RETRY + 1 if not from_cal else 1):
                ctx.count()
                ds = d.strftime("%Y%m%d")
                c = Call(name, f"{years}년 전", ds, note="거래일 달력" if from_cal else "거래일 근사(평일)")
                if attempt:
                    c.note += f", 재시도 {attempt}"
                try:
                    body = fetch(ds)
                    rows = body.get(ROWS_KEY)
                    if not isinstance(rows, list):
                        c.code, c.empty = "응답 형식 다름", None
                        c.note += f", 최상위 키 {list(body)[:4]}"
                        rows = []
                    else:
                        c.code = "OK"
                    c.rows, c.empty = len(rows), not rows
                    bd = sorted({str(r.get("BAS_DD")) for r in rows if r.get("BAS_DD")})
                    c.first, c.last = (bd[0], bd[-1]) if bd else (ds if rows else None, ds if rows else None)
                except ApiError as exc:
                    c.code, c.empty, c.note = "ERROR", None, c.note + ", " + str(exc)[:100]
                    rows = []
                ctx.calls.append(c)
                if rows:
                    oldest = ds if oldest is None or ds < oldest else oldest
                    break
                if from_cal or c.code == "ERROR":
                    break
                d = prev_weekday(d)
        ctx.summary.append([name, "basDd 1·3·5·10·15년 전", f"{oldest} (값이 나온 가장 오래된 요청일)" if oldest else "모두 빈 응답"])


# ---------------------------------------------------------------- DART
def dart_history(ctx: Ctx) -> None:
    oldest = None
    for y in DART_YEARS:
        ctx.count()
        c = Call("DART fnlttSinglAcntAll", f"삼성전자 {SAMSUNG_CORP_CODE} 11011 CFS", y)
        try:
            body = ctx.dart.fs_all(SAMSUNG_CORP_CODE, y, "11011", "CFS")
            lst = body.get("list") or []
            c.code, c.rows, c.empty = str(body.get("status")), len(lst), not lst
            if lst:
                c.first = c.last = str(lst[0].get("rcept_no", ""))[:8] or None
                c.note = f"thstrm_nm {lst[0].get('thstrm_nm', '')}"
                oldest = y if oldest is None or y < oldest else oldest
            elif body.get("message"):
                c.note = str(body.get("message"))[:80]
        except Exception as exc:
            c.code, c.note = "ERROR", str(exc)[:120]
        ctx.calls.append(c)
    ctx.summary.append(["DART 전체 재무제표", "삼성전자 사업보고서", f"{oldest}년 사업보고서" if oldest else "모두 빈 응답"])


# ---------------------------------------------------------------- 실행
def run_history(*, now: datetime, settings: dict, keys: dict, out_dir: Path, db_path: Path,
                http_factory: Callable[[str, float], ApiClient] | None = None,
                out: Callable[[str], None] = print) -> Path:
    http_factory = http_factory or (lambda name, rate: ApiClient(name, rate))
    con = open_db_readonly(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    kis = KisClient(http_factory("KIS", float(settings.get("KIS_CALLS_PER_SEC") or 10)), settings["KIS_BASE_URL"],
                    keys["KIS_APP_KEY"], keys["KIS_APP_SECRET"], out_dir / "kis_token.json", now=lambda: now) \
        if keys.get("KIS_APP_KEY") and keys.get("KIS_APP_SECRET") else None
    krx = KrxClient(http_factory("KRX", float(settings.get("KRX_CALLS_PER_SEC") or 2)), settings["KRX_BASE_URL"],
                    keys["KRX_AUTH_KEY"]) if keys.get("KRX_AUTH_KEY") else None
    dart = DartClient(http_factory("DART", 5), settings["DART_BASE_URL"], keys["DART_API_KEY"]) \
        if keys.get("DART_API_KEY") else None
    ctx = Ctx(now=now, kis=kis, krx=krx, dart=dart, settings=settings, con=con)
    ctx.notes.append(f"수집 DB: {'있음 (읽기 전용)' if con else '없음 (거래일은 평일로 근사, 3번째 종목은 고정 종목)'}")
    try:
        # 1. KIS
        if kis is None:
            ctx.summary.append(["KIS 투자자매매동향", "-", "KIS 키 없음"])
        elif not investor_window_ok(now):
            msg = (f"실행 시각 {now.strftime('%H:%M')}: KIS 투자자매매동향은 00:00~15:40에 OPSQ2001로 막히므로 "
                   "호출하지 않았습니다. 15:40 이후에 다시 실행하세요.")
            out(msg)
            ctx.notes.append(msg)
            ctx.summary.append(["KIS 투자자매매동향", "-", "15:40 이후 실행 필요 (호출 안 함)"])
        else:
            small, why = pick_smallcap(con)
            for code, label in ((KIS_FIXED[0], "삼성전자"), (KIS_FIXED[1], "에코프로비엠"), (small, why)):
                out(f"  KIS {code} ...")
                kis_history(ctx, code, label)
        # 2. KRX
        if krx is None:
            ctx.summary.append(["KRX 일별 시세", "-", "KRX 키 없음"])
        else:
            out("  KRX ...")
            krx_history(ctx)
        # 3. DART
        if dart is None:
            ctx.summary.append(["DART 전체 재무제표", "-", "DART 키 없음"])
        else:
            out("  DART ...")
            dart_history(ctx)
    except CallBudgetExceeded as exc:
        ctx.notes.append(str(exc))
    finally:
        if con is not None:
            con.close()
    events = kis.rate_limit_events if kis else []
    L = [f"# 과거 데이터 조회 가능 기간 실측 {now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)", "",
         f"- 프로그램 버전: {__version__}", f"- 총 호출: {len(ctx.calls)}회 (상한 {MAX_CALLS})",
         f"- KIS 한도 초과(EGW00201): {len(events)}회"] + [f"- {n}" for n in ctx.notes] + [
         "- 수집 DB(market.sqlite)·collect_log에는 아무것도 쓰지 않았습니다.", "",
         "## 요약: 확인된 가장 오래된 날짜", ""]
    L += md_table(["API", "대상", "확인된 가장 오래된 날짜"], ctx.summary)
    L += ["", "## 호출 기록", "",
          "KIS 판정 기준: 응답 행 중 요청일 이전 62일 안의 날짜가 있으면 '그 시점 데이터 있음'. "
          "KIS는 3·6개월, 1·2·3·5·7·10년 전으로 부른 뒤, 있음/없음 경계를 개월 단위 이분 탐색.", ""]
    L += md_table(["#", "API", "대상", "요청 날짜", "응답 행 수", "응답 첫 날짜", "응답 끝 날짜", "rt_cd/msg_cd·status", "빈 응답", "비고"],
                  [[i, c.api, c.target, c.req, "" if c.rows is None else c.rows, c.first or "", c.last or "", c.code,
                    "" if c.empty is None else ("예" if c.empty else "아니오"), c.note]
                   for i, c in enumerate(ctx.calls, 1)])
    path = out_dir / f"history_{stamp(now)}.md"
    path.write_text(redact("\n".join(L)) + "\n", encoding="utf-8")
    return path
