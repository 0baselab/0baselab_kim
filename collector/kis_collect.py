"""KIS 수집 (명세서 6장, 14.2): 종목별 수급 + 당일 시세(잠정), 당일 지수(잠정), 주식기본조회.

- 15:40~23:59에만 호출한다. 자정을 넘기면 멈추고 다음 실행 때 이어서 받는다.
- 종목별 투자자매매동향: 1회 30거래일. 기준일을 가장 오래된 날 하루 전으로 옮겨 과거로 이어 받는다.
- 단위(실측): *_ntby_tr_pbmn 백만 원 -> *_amt_mil, acml_tr_pbmn 원 -> value_krw.
- 당일 행은 잠정(provisional). 다음 실행 때 다시 받아 같으면 그대로, 다르면 value_changes 기록 후 success.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from . import db
from . import trading_calendar as cal
from .kis import (ADJ_PRICE, INDEX_DAILY, INVESTOR_BY_STOCK, MARKET_INVESTOR, STOCK_INFO, KisClient, index_daily_params,
                  investor_params, investor_window_ok, is_ok, market_investor_params, stock_info_params,
                  adj_price_params)
from .http import ApiError
from .krx_collect import to_float, to_int
from .rawstore import save_raw

log = logging.getLogger("collector")

TIME_LIMIT_CODE = "OPSQ2001"
MAX_PAGES = 5
INVESTOR_COLS = {
    "frgn_net_qty": "frgn_ntby_qty", "orgn_net_qty": "orgn_ntby_qty", "prsn_net_qty": "prsn_ntby_qty",
    "frgn_net_amt_mil": "frgn_ntby_tr_pbmn", "orgn_net_amt_mil": "orgn_ntby_tr_pbmn",
    "prsn_net_amt_mil": "prsn_ntby_tr_pbmn",
}
PRICE_COLS = {"open": "stck_oprc", "high": "stck_hgpr", "low": "stck_lwpr", "close": "stck_clpr"}
KIS_INDEXES = {"0001": ("KOSPI", "코스피"), "1001": ("KOSDAQ", "코스닥")}


class KisTimeLimit(Exception):
    pass


@dataclass
class KisStats:
    stocks_done: int = 0
    stocks_failed: int = 0
    calls: int = 0
    provisional: int = 0
    confirmed: int = 0
    changed: int = 0
    empty: int = 0
    not_listed: int = 0
    stopped: str | None = None
    errors: list = field(default_factory=list)


def _prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def target_dates(con, today: str, n: int) -> list[str]:
    """보관 대상 거래일: 오늘 이전 n-1개 + 오늘(개장일일 때)."""
    days = cal.last_open_days(con, today, n)
    if cal.is_open(con, today):
        days = days[1:] + [today] if len(days) >= n else days + [today]
    return days


def needed_dates(con, code: str, targets: list[str]) -> list[str]:
    rows = con.execute(
        "SELECT unit_key, status FROM collect_log WHERE source='KIS' AND dataset='investor'"
        " AND unit_key BETWEEN ? AND ?", (f"{code}|", f"{code}|~")).fetchall()
    done = {r["unit_key"].split("|", 1)[1] for r in rows if r["status"] in ("success", "not_listed", "holiday")}
    return [d for d in targets if d not in done]


def estimate_calls(con, codes: list[str], targets: list[str]) -> int:
    total = 0
    for c in codes:
        need = needed_dates(con, c, targets)
        if need:
            total += max(1, -(-len(need) // 30)) if len(need) > 30 else 1
    return total


def collect_investor(con, kis: KisClient, root, codes: list[str], targets: list[str],
                     now_fn: Callable[[], datetime], progress: Callable[[str], None] = lambda s: None) -> KisStats:
    st = KisStats()
    today = now_fn().strftime("%Y%m%d")
    prev = cal.prev_open(con, today)
    for i, code in enumerate(codes, 1):
        if not investor_window_ok(now_fn()):
            st.stopped = f"{now_fn().strftime('%H:%M')} 조회 가능 시각(15:40~23:59)을 벗어나 멈춤. 다음 실행 때 이어서 받습니다."
            break
        need = needed_dates(con, code, targets)
        if not need:
            continue
        if i % 50 == 0 or i == len(codes):
            progress(f"  KIS 수급 {i}/{len(codes)}")
        try:
            _collect_one(con, kis, root, code, need, today, prev, now_fn, st)
            st.stocks_done += 1
        except KisTimeLimit as exc:
            st.stopped = str(exc)
            break
        except Exception as exc:  # 한 종목 실패가 전체를 멈추지 않게
            if exc.__class__.__name__ == "KisAuthError":
                raise
            st.stocks_failed += 1
            st.errors.append(f"{code}: {exc}")
            ts = now_fn().isoformat(timespec="seconds")
            with db.Tx(con):
                for d in need:
                    if db.get_status(con, "KIS", "investor", f"{code}|{d}") is None:
                        db.set_status(con, "KIS", "investor", f"{code}|{d}", "failed", ts, str(exc)[:200])
    return st


def _collect_one(con, kis, root, code, need, today, prev, now_fn, st: KisStats) -> None:
    listed = (con.execute("SELECT listed_date FROM stock_master WHERE code=?", (code,)).fetchone() or [None])[0]
    need_set = set(need)
    got: dict[str, tuple[dict, str]] = {}
    query = today
    oldest_need = min(need)
    for _ in range(MAX_PAGES):
        now = now_fn()
        body = kis.get(INVESTOR_BY_STOCK, investor_params(code, query)).body
        st.calls += 1
        raw = save_raw(root, "kis", f"investor_{code}", query, body, now)
        if not is_ok(body):
            if body.get("msg_cd") == TIME_LIMIT_CODE:
                raise KisTimeLimit(f"KIS {TIME_LIMIT_CODE} {body.get('msg1')} — 조회 가능 시각이 아니어서 멈춤")
            raise RuntimeError(f"{body.get('msg_cd')} {body.get('msg1')}")
        rows = [r for r in (body.get("output2") or []) if r.get("stck_bsop_date")]
        new = [r for r in rows if r["stck_bsop_date"] not in got]
        for r in new:
            got[r["stck_bsop_date"]] = (r, raw)
        if not new:
            break
        oldest = min(got)
        if oldest <= oldest_need or (listed and oldest <= listed):
            break
        query = _prev_day(oldest)
    ts = now_fn().isoformat(timespec="seconds")
    with db.Tx(con):
        for d, (r, raw) in sorted(got.items()):
            _save_investor(con, d, code, r, today, ts, raw, st)
            _save_price(con, d, code, r, today, prev, ts, raw)
            if d in need_set:
                status = "provisional" if d == today else "success"
                db.set_status(con, "KIS", "investor", f"{code}|{d}", status, ts, None, raw)
                if status == "provisional":
                    st.provisional += 1
                else:
                    st.confirmed += 1
        lo = min(got) if got else None
        for d in need:
            if d in got:
                continue
            if listed and d < listed:
                db.set_status(con, "KIS", "investor", f"{code}|{d}", "not_listed", ts)
                st.not_listed += 1
            elif lo and d >= lo:  # 응답 범위 안인데 없는 개장일
                db.set_status(con, "KIS", "investor", f"{code}|{d}", "empty", ts)
                st.empty += 1


def _save_investor(con, d, code, r, today, ts, raw, st: KisStats) -> None:
    new = {col: to_int(r.get(src)) for col, src in INVESTOR_COLS.items()}
    old = con.execute("SELECT * FROM investor_daily WHERE date=? AND code=?", (d, code)).fetchone()
    if old is not None:
        diffs = [c for c in INVESTOR_COLS if old[c] != new[c]]
        for c in diffs:
            db.record_change(con, "investor_daily", f"{d}|{code}", c, old[c], new[c], ts,
                             "잠정 -> 재조회" if old["is_provisional"] else "재조회 값 변경")
        if diffs:
            st.changed += 1
    con.execute(
        "INSERT OR REPLACE INTO investor_daily(date, code, frgn_net_qty, orgn_net_qty, prsn_net_qty, frgn_net_amt_mil,"
        " orgn_net_amt_mil, prsn_net_amt_mil, detail_json, is_provisional, source, fetched_at, raw_file)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,'KIS',?,?)",
        (d, code, *[new[c] for c in INVESTOR_COLS], json.dumps(r, ensure_ascii=False),
         1 if d == today else 0, ts, raw))


def _save_price(con, d, code, r, today, prev, ts, raw) -> None:
    """KRX 확정값이 없는 날만 KIS 값을 잠정으로 넣는다 (14.2). KRX 값은 절대 덮어쓰지 않는다."""
    old = con.execute("SELECT * FROM price_daily WHERE date=? AND code=?", (d, code)).fetchone()
    if old is not None and not old["is_provisional"]:
        return
    new = {col: to_float(r.get(src)) for col, src in PRICE_COLS.items()}
    new["change_rate_pct"] = to_float(r.get("prdy_ctrt"))
    new["volume_sh"] = to_int(r.get("acml_vol"))
    new["value_krw"] = to_int(r.get("acml_tr_pbmn"))  # 실측: 원 단위
    new["cmpprevdd_prc"] = to_float(r.get("prdy_vrss"))  # 전일대비 (기준가 조정 판정용, 부호 미확인)
    # 시가총액(잠정) = 직전 거래일 KRX 상장주식수 × 종가
    base_day = cal.prev_open(con, d) if d == today else d
    # KRX 일별매매정보의 LIST_SHRS를 먼저 쓰고(기업행동 판단과 같은 출처), 없으면 종목기본정보
    shares_row = con.execute("SELECT listed_shares FROM price_daily WHERE code=? AND date<=? AND source='KRX'"
                             " AND listed_shares IS NOT NULL ORDER BY date DESC LIMIT 1", (code, base_day or d)).fetchone() \
        or con.execute("SELECT list_shares FROM krx_base_daily WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
                       (code, base_day or d)).fetchone()
    shares = shares_row[0] if shares_row else None
    new["listed_shares"] = shares
    new["mktcap_krw"] = int(round(shares * new["close"])) if shares is not None and new["close"] is not None else None
    if old is not None:
        for c in ("open", "high", "low", "close", "volume_sh", "value_krw"):
            if old[c] != new[c]:
                db.record_change(con, "price_daily", f"{d}|{code}", c, old[c], new[c], ts, "KIS 잠정 재조회")
    market = (con.execute("SELECT market FROM stock_master WHERE code=?", (code,)).fetchone() or [None])[0]
    con.execute(
        "INSERT OR REPLACE INTO price_daily(date, code, market, open, high, low, close, change_rate_pct, volume_sh,"
        " value_krw, mktcap_krw, listed_shares, cmpprevdd_prc, is_provisional, source, fetched_at, raw_file)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,'KIS',?,?)",
        (d, code, market, new["open"], new["high"], new["low"], new["close"], new["change_rate_pct"],
         new["volume_sh"], new["value_krw"], new["mktcap_krw"], new["listed_shares"], new["cmpprevdd_prc"], ts, raw))


def collect_index_today(con, kis: KisClient, root, now: datetime) -> list[str]:
    """당일 코스피·코스닥 지수(잠정). 실패하면 빈 칸 + 사유 (14.2)."""
    today = now.strftime("%Y%m%d")
    ts = now.isoformat(timespec="seconds")
    out = []
    for code, (series, name) in KIS_INDEXES.items():
        try:
            body = kis.get(INDEX_DAILY, index_daily_params(code, today)).body
        except ApiError as exc:
            body = {"rt_cd": "1", "msg_cd": "ERROR", "msg1": str(exc)[:200]}
        raw = save_raw(root, "kis", f"index_{code}", today, body, now)
        row = next((r for r in (body.get("output2") or []) if r.get("stck_bsop_date") == today), None) if is_ok(body) else None
        with db.Tx(con):
            if row is None:
                reason = f"{body.get('msg_cd')} {body.get('msg1')}" if not is_ok(body) else "응답에 당일 행 없음"
                db.set_status(con, "KIS", f"index_{series}", today, "failed" if not is_ok(body) else "empty", ts, reason, raw)
                out.append(f"{name} 지수: 빈 칸 ({reason})")
                continue
            existing = con.execute("SELECT is_provisional FROM index_daily WHERE date=? AND series=? AND index_name=?",
                                   (today, series, name)).fetchone()
            if existing is None or existing[0]:
                con.execute(
                    "INSERT OR REPLACE INTO index_daily(date, series, index_name, index_class, open, high, low, close,"
                    " change_rate_pct, volume_unconfirmed, value_unconfirmed, mktcap_unconfirmed, is_provisional,"
                    " source, fetched_at, raw_file) VALUES(?,?,?,NULL,?,?,?,?,?,?,?,NULL,1,'KIS',?,?)",
                    (today, series, name, to_float(row.get("bstp_nmix_oprc")), to_float(row.get("bstp_nmix_hgpr")),
                     to_float(row.get("bstp_nmix_lwpr")), to_float(row.get("bstp_nmix_prpr")),
                     to_float(row.get("bstp_nmix_prdy_ctrt")), to_int(row.get("acml_vol")),
                     to_int(row.get("acml_tr_pbmn")), ts, raw))
            db.set_status(con, "KIS", f"index_{series}", today, "provisional", ts, None, raw)
            out.append(f"{name} 지수: {row.get('bstp_nmix_prpr')} (잠정)")
    return out


def stockinfo_targets(con, codes: list[str], now: datetime, refresh_days: int) -> list[str]:
    cutoff = (now - timedelta(days=refresh_days)).isoformat(timespec="seconds")
    rows = {r["code"]: r["kis_checked_at"] for r in con.execute("SELECT code, kis_checked_at FROM stock_master")}
    return [c for c in codes if not rows.get(c) or rows[c] < cutoff]


def collect_stockinfo(con, kis: KisClient, root, codes: list[str], now_fn) -> tuple[int, int]:
    """관리종목·거래정지 표시용. 필터에는 쓰지 않는다 (14.3)."""
    ok = fail = 0
    for code in codes:
        if not investor_window_ok(now_fn()):
            break
        now = now_fn()
        try:
            body = kis.get(STOCK_INFO, stock_info_params(code)).body
        except Exception as exc:
            if exc.__class__.__name__ == "KisAuthError":
                raise
            fail += 1
            continue
        if not is_ok(body):
            fail += 1
            continue
        o = body.get("output") or {}
        yn = lambda v: None if v not in ("Y", "N") else (1 if v == "Y" else 0)
        raw = save_raw(root, "kis", f"stockinfo_{code}", now.strftime("%Y%m%d"), body, now)
        with db.Tx(con):
            con.execute("UPDATE stock_master SET kis_scty_grp=?, kis_stck_kind=?, is_admin=?, is_halted=?,"
                        " kis_checked_at=? WHERE code=?",
                        (o.get("scty_grp_id_cd"), o.get("stck_kind_cd"), yn(o.get("admn_item_yn")),
                         yn(o.get("tr_stop_yn")), now.isoformat(timespec="seconds"), code))
            db.set_status(con, "KIS", "stockinfo", code, "success", now.isoformat(timespec="seconds"), None, raw)
        ok += 1
    return ok, fail


def apply_flags(con, day: str) -> None:
    """stock_master의 관리종목·거래정지 표시를 해당 날짜 universe_daily에 반영."""
    con.execute("UPDATE universe_daily SET is_admin=(SELECT is_admin FROM stock_master s WHERE s.code=universe_daily.code),"
                " is_halted=(SELECT is_halted FROM stock_master s WHERE s.code=universe_daily.code) WHERE date=?", (day,))


# ---------------------------------------------------------------------------
# 시장별 투자자매매동향 (FHPTJ04040000). 단위는 KIS 문서에 없어 _unconfirmed 컬럼에 저장.
# ---------------------------------------------------------------------------
MARKETS = {"KOSPI": ("0001", "KSP"), "KOSDAQ": ("1001", "KSQ")}
MKT_COLS = {
    "frgn_net_qty_unconfirmed": "frgn_ntby_qty", "orgn_net_qty_unconfirmed": "orgn_ntby_qty",
    "prsn_net_qty_unconfirmed": "prsn_ntby_qty", "frgn_net_amt_unconfirmed": "frgn_ntby_tr_pbmn",
    "orgn_net_amt_unconfirmed": "orgn_ntby_tr_pbmn", "prsn_net_amt_unconfirmed": "prsn_ntby_tr_pbmn",
}


def collect_market_investor(con, kis: KisClient, root, targets: list[str], now_fn) -> list[str]:
    today = now_fn().strftime("%Y%m%d")
    out = []
    for market, (idx, mcode) in MARKETS.items():
        ds = f"market_investor_{market}"
        done = {r[0] for r in con.execute("SELECT unit_key FROM collect_log WHERE source='KIS' AND dataset=?"
                                          " AND status IN ('success','holiday')", (ds,))}
        need = sorted((d for d in targets if d not in done), reverse=True)
        calls = saved = 0
        first_rows = None
        for d in need:
            if db.get_status(con, "KIS", ds, d) == "success":
                continue  # 앞선 응답에 이미 들어와 확정됨
            if not investor_window_ok(now_fn()):
                out.append(f"시장별 수급 {market}: 조회 가능 시각을 벗어나 멈춤")
                break
            now = now_fn()
            ts = now.isoformat(timespec="seconds")
            try:
                body = kis.get(MARKET_INVESTOR, market_investor_params(idx, mcode, d)).body
            except ApiError as exc:
                body = {"rt_cd": "1", "msg_cd": "ERROR", "msg1": str(exc)[:200]}
            calls += 1
            raw = save_raw(root, "kis", f"market_investor_{market}", d, body, now)
            if not is_ok(body):
                with db.Tx(con):
                    db.set_status(con, "KIS", ds, d, "failed", ts, f"{body.get('msg_cd')} {body.get('msg1')}", raw)
                if body.get("msg_cd") == TIME_LIMIT_CODE:
                    out.append(f"시장별 수급 {market}: {TIME_LIMIT_CODE}로 멈춤")
                    break
                continue
            rows = [r for r in (body.get("output") or []) if r.get("stck_bsop_date")]
            if first_rows is None:
                first_rows = len(rows)
            with db.Tx(con):
                got = set()
                for r in rows:
                    rd = r["stck_bsop_date"]
                    if rd not in targets and rd != d:
                        continue
                    got.add(rd)
                    _save_market(con, rd, market, r, today, ts, raw)
                    if db.get_status(con, "KIS", ds, rd) != "success" or rd == today:
                        db.set_status(con, "KIS", ds, rd, "provisional" if rd == today else "success", ts, None, raw)
                    saved += 1
                if d not in got:
                    db.set_status(con, "KIS", ds, d, "empty", ts, None, raw)
        if need:
            out.append(f"시장별 수급 {market}: 호출 {calls}회, 저장 {saved}행 (1회 응답 일수 {first_rows if first_rows is not None else '-'})")
    return out


def _save_market(con, d, market, r, today, ts, raw) -> None:
    new = {c: to_int(r.get(src)) for c, src in MKT_COLS.items()}
    old = con.execute("SELECT * FROM market_investor_daily WHERE date=? AND market=?", (d, market)).fetchone()
    if old is not None:
        for c in MKT_COLS:
            if old[c] != new[c]:
                db.record_change(con, "market_investor_daily", f"{d}|{market}", c, old[c], new[c], ts,
                                 "잠정 -> 재조회" if old["is_provisional"] else "재조회 값 변경")
    con.execute(
        "INSERT OR REPLACE INTO market_investor_daily(date, market, frgn_net_qty_unconfirmed, orgn_net_qty_unconfirmed,"
        " prsn_net_qty_unconfirmed, frgn_net_amt_unconfirmed, orgn_net_amt_unconfirmed, prsn_net_amt_unconfirmed,"
        " index_close, detail_json, is_provisional, source, fetched_at, raw_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,'KIS',?,?)",
        (d, market, *[new[c] for c in MKT_COLS], to_float(r.get("bstp_nmix_prpr")), json.dumps(r, ensure_ascii=False),
         1 if d == today else 0, ts, raw))


# ---------------------------------------------------------------------------
# 수정주가 재조회 (명세서 8.4): 기준가가 조정된 종목만 (0.6.1 실측 반영). 원본 price_daily는 건드리지 않는다.
# ---------------------------------------------------------------------------
ADJ_WINDOW = 251  # 250일 수익률에 필요한 거래일 수
ADJ_CHUNK = 100   # 1회 최대 100건 (KIS 문서)


def latest_adjust_days(con, day: str, codes: list[str] | None = None) -> dict[str, str]:
    """day까지 최근 ADJ_WINDOW 거래일 안에서 기준가가 조정된 마지막 날 (종목별)."""
    from .krx_collect import base_price_adjust_days
    days = sorted(r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date <= ? ORDER BY date DESC LIMIT ?", (day, ADJ_WINDOW)))
    if not days:
        return {}
    return {c: ds[-1] for c, ds in base_price_adjust_days(con, days[0], days[-1], codes).items()}


def collect_adjusted(con, kis: KisClient, root, codes: list[str], now_fn) -> tuple[int, int, int]:
    """기업행동 의심 종목의 수정주가 250거래일. (받음, 실패, 이미 받음) 개수."""
    today = now_fn().strftime("%Y%m%d")
    changes = latest_adjust_days(con, today, codes)
    days = sorted(r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date <= ? ORDER BY date DESC LIMIT ?", (today, ADJ_WINDOW)))
    ok = fail = skip = 0
    for code, changed in sorted(changes.items()):
        key = f"{code}|{changed}"
        if db.get_status(con, "KIS", "adj_price", key) == "success":
            skip += 1
            continue
        if not investor_window_ok(now_fn()):
            break
        now = now_fn()
        ts = now.isoformat(timespec="seconds")
        rows, raws, err = {}, [], None
        for i in range(0, len(days), ADJ_CHUNK):
            chunk = days[i:i + ADJ_CHUNK]
            try:
                body = kis.get(ADJ_PRICE, adj_price_params(code, chunk[0], chunk[-1])).body
            except ApiError as exc:
                err = str(exc)[:200]
                break
            raws.append(save_raw(root, "kis", f"adj_price_{code}", chunk[-1], body, now))
            if not is_ok(body):
                err = f"{body.get('msg_cd')} {body.get('msg1')}"
                break
            for r in body.get("output2") or []:
                d = r.get("stck_bsop_date")
                if d and chunk[0] <= d <= chunk[-1]:
                    rows[d] = r
        with db.Tx(con):
            if err or not rows:
                db.set_status(con, "KIS", "adj_price", key, "failed" if err else "empty", ts, err,
                              raws[-1] if raws else None)
                fail += 1
                continue
            con.execute("DELETE FROM adj_price_daily WHERE code=?", (code,))
            for d, r in rows.items():
                con.execute("INSERT INTO adj_price_daily VALUES(?,?,?,?,?,?,?,'KIS',?,?)",
                            (code, d, changed, to_float(r.get("stck_oprc")), to_float(r.get("stck_hgpr")),
                             to_float(r.get("stck_lwpr")), to_float(r.get("stck_clpr")), ts, raws[0]))
            db.set_status(con, "KIS", "adj_price", key, "success", ts, None, raws[0])
        ok += 1
    return ok, fail, skip
