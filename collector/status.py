"""상태 점검 (메뉴 6, 명세서 2.2). DB만 읽는다."""
from __future__ import annotations

from . import trading_calendar as cal
from .app import App
from .krx_collect import datasets
from .secrets import KEY_NAMES, mask


def report(app: App) -> list[str]:
    con = app.con
    today = app.now().strftime("%Y%m%d")
    L = [f"데이터 폴더: {app.root}",
         "키: " + ", ".join(f"{k}={mask(app.keys.get(k))}" for k in KEY_NAMES)]
    lo, hi, n = cal.coverage(con)
    L.append(f"거래일 달력: {lo or '-'} ~ {hi or '-'} (거래일 {n}일)")
    retention = int(app.settings.get("KRX_RETENTION_DAYS") or 250)
    targets = cal.last_open_days(con, today, retention)
    L.append(f"KRX 보관 대상 거래일: {len(targets)}일 ({targets[0] if targets else '-'} ~ {targets[-1] if targets else '-'})")
    L.append("")
    L.append("KRX 데이터별 마지막 성공일 / 빠진 거래일 수")
    for ds in datasets():
        last = con.execute("SELECT MAX(unit_key) FROM collect_log WHERE source='KRX' AND dataset=? AND status='success'",
                           (ds,)).fetchone()[0]
        ok = {r[0] for r in con.execute(
            "SELECT unit_key FROM collect_log WHERE source='KRX' AND dataset=? AND status='success'", (ds,))}
        missing = [d for d in targets if d not in ok]
        L.append(f"  {ds:14s} 마지막 {last or '-'}  빠짐 {len(missing)}일" +
                 (f" (최근: {', '.join(sorted(missing, reverse=True)[:5])})" if missing else ""))
    kis_last = con.execute("SELECT MAX(substr(unit_key, 8)) FROM collect_log WHERE source='KIS' AND dataset='investor'"
                           " AND status='success'").fetchone()[0]
    kis_prov = con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KIS' AND dataset='investor'"
                           " AND status='provisional'").fetchone()[0]
    kis_codes = con.execute("SELECT COUNT(DISTINCT code) FROM investor_daily").fetchone()[0]
    L.append(f"KIS 수급: 종목 {kis_codes}개, 마지막 확정일 {kis_last or '-'}, 잠정(당일) {kis_prov}건")
    for mk in ("KOSPI", "KOSDAQ"):
        last = con.execute("SELECT MAX(date) FROM market_investor_daily WHERE market=?", (mk,)).fetchone()[0]
        L.append(f"KIS 시장별 수급 {mk}: 마지막 {last or '-'}")
    d_last = con.execute("SELECT MAX(rcept_dt) FROM dart_filings").fetchone()[0]
    d_q = con.execute("SELECT COUNT(*), COUNT(DISTINCT corp_code) FROM dart_fs_quarter").fetchone()
    d_left = con.execute("SELECT COUNT(*) FROM dart_filings f WHERE is_periodic=1 AND reprt_code IS NOT NULL AND NOT EXISTS"
                         " (SELECT 1 FROM collect_log c WHERE c.source='DART' AND c.dataset='fs' AND c.unit_key=f.rcept_no"
                         " AND c.status IN ('success','empty'))").fetchone()[0]
    d_used = con.execute("SELECT value FROM meta WHERE key=?", (f"dart_calls_{today}",)).fetchone()
    L.append(f"DART: 마지막 공시 접수일 {d_last or '-'}, 분기 재무 {d_q[0]}행({d_q[1]}개사), "
             f"아직 안 받은 정기보고서 {d_left}건, 오늘 호출 {d_used[0] if d_used else 0}회")
    admin = con.execute("SELECT COUNT(*) FROM stock_master WHERE is_admin=1").fetchone()[0]
    halted = con.execute("SELECT COUNT(*) FROM stock_master WHERE is_halted=1").fetchone()[0]
    L.append(f"KIS 주식기본조회: 관리종목 {admin}개, 거래정지 {halted}개")
    L.append("")
    recheck = con.execute("SELECT source, dataset, unit_key, empty_streak FROM collect_log WHERE status='empty'"
                          " AND empty_streak >= 3 ORDER BY unit_key DESC LIMIT 20").fetchall()
    L.append(f"재확인 필요 (빈 결과 3회 연속): {len(recheck)}건")
    L += [f"  {r['source']} {r['dataset']} {r['unit_key']} ({r['empty_streak']}회)" for r in recheck]
    failed = con.execute("SELECT source, dataset, unit_key, last_error FROM collect_log WHERE status='failed'"
                         " ORDER BY unit_key DESC LIMIT 20").fetchall()
    L.append(f"실패: {len(failed)}건")
    L += [f"  {r['source']} {r['dataset']} {r['unit_key']}: {r['last_error']}" for r in failed]
    latest = con.execute("SELECT MAX(date) FROM universe_daily").fetchone()[0]
    if latest:
        n_in = con.execute("SELECT COUNT(*) FROM universe_daily WHERE date=? AND in_universe=1", (latest,)).fetchone()[0]
        reasons = con.execute("SELECT exclude_reason, COUNT(*) c FROM universe_daily WHERE date=? AND in_universe=0"
                              " GROUP BY exclude_reason", (latest,)).fetchall()
        L.append(f"대상 종목 ({latest}): {n_in}개, 제외: " + ", ".join(f"{r[0]} {r[1]}" for r in reasons))
    # 단위 눈으로 확인용 (시가총액 MKTCAP 단위는 아직 실측 대조 전)
    r = con.execute("SELECT date, close, value_krw, mktcap_krw FROM price_daily WHERE code='005930'"
                    " ORDER BY date DESC LIMIT 1").fetchone()
    if r:
        L.append(f"확인용 삼성전자 {r['date']}: 종가 {r['close']:,.0f}, 거래대금 {r['value_krw']:,}원, "
                 f"시가총액 {r['mktcap_krw']:,} (원 단위라면 수백조 자리)" if r["mktcap_krw"] is not None else
                 f"확인용 삼성전자 {r['date']}: 시가총액 NULL")
    last_run = con.execute("SELECT run_id, mode, ended_at, calls, errors FROM run_log ORDER BY run_id DESC LIMIT 1").fetchone()
    if last_run:
        L.append(f"마지막 실행: {last_run['run_id']} ({last_run['mode']}), 호출 {last_run['calls']}, 오류 {last_run['errors']}")
    return L
