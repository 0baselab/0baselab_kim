"""수집 실행 (명세서 6장, 14장): 거래일 달력 -> KRX(시간 무관) -> KIS(15:40~23:59)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from . import db, maintenance
from . import kis_collect, krx_collect
from . import trading_calendar as cal
from . import universe
from .app import App
from .http import ApiError
from .kis import KisAuthError, investor_window_ok
from .timeutil import stamp
from .version import __version__

log = logging.getLogger("collector")

KRX_SEC_PER_CALL_DEFAULT = 0.5


@dataclass
class RunResult:
    lines: list[str] = field(default_factory=list)
    errors: int = 0
    stopped: str | None = None

    def add(self, s: str) -> None:
        self.lines.append(s)
        log.info(s)


def _int(settings, key, default):
    try:
        return int(settings.get(key) or default)
    except ValueError:
        return default


def update_calendar(app: App, res: RunResult, need: int) -> None:
    kis = app.kis()
    if kis is None:
        res.add("거래일 달력: KIS 키가 없어 갱신하지 못했습니다.")
        return
    now = app.now()
    try:
        res.add("거래일 달력(휴장일조회): " + cal.update_holidays(app.con, kis, app.root, now))
        res.add("거래일 달력(코스피 지수 날짜): " + cal.update_past(app.con, kis, app.root, now, need))
    except KisAuthError:
        raise
    except ApiError as exc:
        res.errors += 1
        res.add(f"거래일 달력 갱신 실패: {exc}")
    lo, hi, n = cal.coverage(app.con)
    res.add(f"거래일 달력 범위: {lo or '-'} ~ {hi or '-'} (거래일 {n}일)")


def krx_targets(app: App, mode: str, start: str | None = None, end: str | None = None) -> list[str]:
    today = app.now().strftime("%Y%m%d")
    if mode == "daily":
        days = cal.last_open_days(app.con, today, _int(app.settings, "KRX_RETENTION_DAYS", 250))
    else:
        days = [d for d in cal.open_days(app.con, start, end) if d < today]
    return sorted(days, reverse=True)  # 최근 날짜부터


def run_collect(app: App, mode: str = "daily", start: str | None = None, end: str | None = None,
                force: bool = False, confirm: Callable[[str], bool] = lambda q: True,
                out: Callable[[str], None] = print) -> RunResult:
    res = RunResult()
    run_id = stamp(app.started)
    t0 = app.now()
    app.con.execute("INSERT OR REPLACE INTO run_log(run_id, mode, version, started_at) VALUES(?,?,?,?)",
                    (run_id, mode, __version__, t0.isoformat(timespec="seconds")))
    res.add(f"주식데이터수집기 {__version__} / {'당일' if mode == 'daily' else '기간'} 수집 ({t0.strftime('%Y-%m-%d %H:%M')})")
    retention = max(_int(app.settings, "KRX_RETENTION_DAYS", 250), _int(app.settings, "KIS_RETENTION_DAYS", 120))
    try:
        update_calendar(app, res, retention)
        days = krx_targets(app, mode, start, end)
        if not days:
            res.add("수집할 거래일이 없습니다. 거래일 달력이 비어 있으면 15:40~23:59에 다시 실행해 보세요 "
                    "(KIS 지수 API의 조회 가능 시각은 아직 확인되지 않았습니다).")
        _run_krx(app, res, days, force, confirm, out)
        _run_kis(app, res, confirm, out)
    except (KisAuthError, krx_collect.KrxAuthError) as exc:
        res.stopped = str(exc)
        res.errors += 1
        res.add(f"중단 (인증 실패): {exc}")
    except KeyboardInterrupt:
        res.stopped = "사용자 중단"
        res.add("사용자가 중단했습니다. 진행 중이던 단위만 버렸고, 다음 실행 때 이어서 받습니다.")
    finally:
        _finish(app, res, run_id, t0)
    return res


def _run_krx(app, res, days, force, confirm, out) -> None:
    krx = app.krx()
    if krx is None:
        res.add("KRX: 키가 없어 건너뜀")
        return
    units = krx_collect.pending_units(app.con, days, force)
    if not units:
        res.add(f"KRX: 받을 단위 없음 (대상 거래일 {len(days)}일 모두 수집 완료)")
        _refresh_universe(app, res, set(), days)
        return
    first_load = app.con.execute(
        "SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND status='success'").fetchone()[0] == 0
    minutes = len(units) * (1.0 / float(app.settings.get("KRX_CALLS_PER_SEC") or 2)) / 60
    msg = f"KRX {len(days)}거래일, {len(units)}회 호출 예정. 예상 약 {max(1, round(minutes))}분."
    if first_load:
        msg = "처음 실행(초기 적재)입니다. " + msg + " 중간에 꺼도 다음 실행 때 이어서 받습니다."
        if not confirm(msg + " 진행할까요?"):
            res.add("사용자가 초기 적재를 취소했습니다.")
            return
    else:
        out(msg)
    stats = krx_collect.collect(app.con, krx, app.settings, app.root, units, app.now, progress=out)
    res.errors += stats.failed
    res.add(f"KRX: 성공 {stats.success}, 빈 결과(empty) {stats.empty}, 실패 {stats.failed}, "
            f"KIS 잠정값 교체 {stats.replaced_provisional}, 종목코드 형식 오류 {stats.bad_codes}")
    for e in stats.errors[:10]:
        res.add(f"  - {e}")
    _refresh_universe(app, res, stats.base_dates, days)


def _refresh_universe(app, res, base_dates: set, days: list[str]) -> None:
    """이번에 받은 날짜 + 아직 계산 안 된 날짜의 대상 종목을 계산한다."""
    done = {r[0] for r in app.con.execute("SELECT DISTINCT date FROM universe_daily")}
    have = {r[0] for r in app.con.execute("SELECT DISTINCT date FROM krx_base_daily")}
    todo = sorted((base_dates | (have - done)) & have)
    ts = app.now().isoformat(timespec="seconds")
    for d in todo:
        with db.Tx(app.con):
            universe.compute(app.con, d, ts)
    if have:
        latest = max(have)
        n = app.con.execute("SELECT COUNT(*) FROM universe_daily WHERE date=? AND in_universe=1", (latest,)).fetchone()[0]
        res.add(f"대상 종목: {latest} 기준 {n}개 (대상 종목 계산 {len(todo)}일)")


def _finish(app, res, run_id, t0) -> None:
    t1 = app.now()
    calls = 0
    for attr in ("_kis", "_krx"):
        c = getattr(app, attr, None)
        if c is not None:
            calls += c.api.calls
    try:
        if res.stopped != "사용자 중단":
            path = maintenance.backup_db(app.con, app.dirs["backup"], t1, _int(app.settings, "BACKUP_KEEP", 7))
            res.add(f"DB 스냅샷: backup/{path.name}")
        removed = maintenance.cleanup_logs(app.dirs["logs"], t1, _int(app.settings, "LOG_KEEP_DAYS", 90))
        if removed:
            res.add(f"오래된 로그 {removed}개 삭제")
    except Exception as exc:  # 백업 실패가 수집 결과를 지우지 않게
        res.add(f"백업·정리 실패: {exc}")
    res.add(f"걸린 시간: {int((t1 - t0).total_seconds())}초, API 호출 {calls}회")
    app.con.execute("UPDATE run_log SET ended_at=?, calls=?, errors=?, summary=? WHERE run_id=?",
                    (t1.isoformat(timespec="seconds"), calls, res.errors,
                     json.dumps(res.lines, ensure_ascii=False), run_id))


AFTER_MARKET_END = 20 * 60


def _run_kis(app, res, confirm, out) -> None:
    now = app.now()
    if not investor_window_ok(now):
        res.add(f"KIS 단계 건너뜀: 실행 시각 {now.strftime('%H:%M')}은 KIS 수급 조회 가능 시각(15:40~23:59)이 아닙니다 "
                "(00:00~15:40에는 OPSQ2001 TIME LIMIT).")
        return
    kis = app.kis()
    if kis is None:
        res.add("KIS: 키가 없어 건너뜀")
        return
    if now.hour * 60 + now.minute < AFTER_MARKET_END:
        if not confirm("지금은 애프터마켓(16:00~20:00) 진행 중이라 당일 거래량·대금이 더 늘어날 수 있습니다. "
                       "KIS 수집을 계속할까요?"):
            res.add("KIS 단계: 사용자가 애프터마켓 시간이라 건너뜀")
            return
    today = now.strftime("%Y%m%d")
    base_day = app.con.execute("SELECT MAX(date) FROM krx_base_daily WHERE date < ?", (today,)).fetchone()[0]
    if not base_day:
        res.add("KIS 단계 건너뜀: KRX 종목기본정보가 없어 대상 종목을 정할 수 없습니다.")
        return
    codes = universe.universe_codes(app.con, base_day)
    targets = kis_collect.target_dates(app.con, today, _int(app.settings, "KIS_RETENTION_DAYS", 120))
    first = app.con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KIS' AND dataset='investor'"
                            " AND status IN ('success','provisional')").fetchone()[0] == 0
    est = kis_collect.estimate_calls(app.con, codes, targets)
    rate = float(app.settings.get("KIS_CALLS_PER_SEC") or 10)
    msg = (f"KIS 수급: 대상 종목 {len(codes)}개({base_day} 기준), {len(targets)}거래일, 약 {est}회 호출, "
           f"예상 약 {max(1, round(est / rate / 60))}분.")
    if first:
        if not confirm("KIS 초기 적재입니다. " + msg + " 자정을 넘기면 멈추고 다음 실행 때 이어서 받습니다. 진행할까요?"):
            res.add("사용자가 KIS 초기 적재를 취소했습니다.")
            return
    else:
        out(msg)
    if cal.is_open(app.con, today):
        for line in kis_collect.collect_index_today(app.con, kis, app.root, now):
            res.add(line)
    st = kis_collect.collect_investor(app.con, kis, app.root, codes, targets, app.now, progress=out)
    res.errors += st.stocks_failed
    res.add(f"KIS 수급: 종목 {st.stocks_done}개 처리, 실패 {st.stocks_failed}, 호출 {st.calls}회, "
            f"확정 {st.confirmed}, 잠정(당일) {st.provisional}, 값 바뀜 {st.changed}, 빈 결과 {st.empty}, 상장 전 {st.not_listed}")
    for e in st.errors[:10]:
        res.add(f"  - {e}")
    if st.stopped:
        res.add("KIS 수급 " + st.stopped)
        return
    for line in kis_collect.collect_market_investor(app.con, kis, app.root, targets, app.now):
        res.add(line)
    a_ok, a_fail, a_skip = kis_collect.collect_adjusted(app.con, kis, app.root, codes, app.now)
    if a_ok or a_fail or a_skip:
        res.add(f"KIS 수정주가(상장주식수 변경 종목): {a_ok}개 받음, 실패 {a_fail}, 이미 받음 {a_skip}")
    info = kis_collect.stockinfo_targets(app.con, codes, app.now(), _int(app.settings, "KIS_STOCKINFO_REFRESH_DAYS", 7))
    if info:
        ok, fail = kis_collect.collect_stockinfo(app.con, kis, app.root, info, app.now)
        res.add(f"KIS 주식기본조회(관리종목·거래정지): {ok}개 갱신, 실패 {fail}")
    with db.Tx(app.con):
        kis_collect.apply_flags(app.con, base_day)
    if kis.rate_limit_events:
        res.add(f"KIS 한도 초과(EGW00201) {len(kis.rate_limit_events)}회, 마지막 속도 초당 {kis.api.limiter.per_sec:.2f}건")
