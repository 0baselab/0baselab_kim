"""토스 수집기: 종목 목록 / 거래일 / 일봉(아침 필터링용) / 분봉(검증·보관용) / 현황.

- 거래일은 KOSPI 지수 일봉이 있는 날로 판단한다. 평일을 거래일로 가정하지 않는다.
- 종목은 지금 거래 중인 주식(STOCK) 보통주만 토스가 준다. 과거 날짜에도 현재 종목만 수집된다(생존 편향, 한계).
- 일봉·분봉 모두 adjusted=false(수정주가 미적용, 실제 체결 가격)로 받는다.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import datastore as ds
from .candles import page_candles, parse_ts
from .client import NotAllowedError, TossAuthError, TossClient

Log = Callable[[str], None]
CANDLES = "/api/v1/candles"
REG_START, REG_END = 9 * 60, 15 * 60 + 30   # 정규장: 봉 시작 09:00 ~ 15:30


def _ts(now) -> str:
    return now().isoformat(timespec="seconds")


def _kst(day: str, hms: str) -> str:
    return f"{day}T{hms}+09:00"


# ---------------------------------------------------------------- 종목 목록
def refresh_universe(client: TossClient, dcon, log: Log, now) -> int:
    today = now().date().isoformat()
    items = []
    for market in ("KOSPI", "KOSDAQ"):
        got = client.get("/api/v1/stocks/all", {"market": market, "status": "ACTIVE", "securityType": "STOCK",
                                                "commonShare": "true"}).get("result") or []
        items += [dict(i, market=market) for i in got if i.get("symbol")]
    if not items:
        raise SystemExit("종목 목록이 비어 있습니다 (stocks/all). 잠시 후 다시 실행하세요.")
    list_dates, nxt = {}, {}
    syms = [i["symbol"] for i in items]
    for k in range(0, len(syms), 200):  # stocks/all에는 상장일·NXT 여부가 없어 stocks로 조회
        for s in client.get("/api/v1/stocks", {"symbols": ",".join(syms[k:k + 200])}).get("result") or []:
            list_dates[s.get("symbol")] = s.get("listDate")
            v = (s.get("koreanMarketDetail") or {}).get("nxtSupported")
            nxt[s.get("symbol")] = None if v is None else int(bool(v))
    with dcon:
        dcon.execute("UPDATE universe SET active=0")
        for i in items:
            dcon.execute(
                "INSERT INTO universe(code,name,market,security_type,is_common,list_date,first_seen,last_seen,active)"
                " VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market,"
                " security_type=excluded.security_type, is_common=excluded.is_common,"
                " list_date=COALESCE(excluded.list_date, universe.list_date), last_seen=excluded.last_seen, active=1",
                (i["symbol"], i.get("name"), i["market"], i.get("securityType"), int(bool(i.get("isCommonShare"))),
                 list_dates.get(i["symbol"]), today, today))
            dcon.execute("UPDATE universe SET nxt_supported=? WHERE code=?", (nxt.get(i["symbol"]), i["symbol"]))
        dcon.execute("INSERT OR REPLACE INTO meta VALUES ('universe_refreshed', ?)", (today,))
    gone = dcon.execute("SELECT COUNT(*) FROM universe WHERE active=0").fetchone()[0]
    n_nxt = sum(1 for v in nxt.values() if v)
    log(f"[universe] 주식 보통주 {len(items):,}종목 (KOSPI·KOSDAQ, NXT 거래 가능 {n_nxt:,}), 목록에서 빠진 종목 누적 {gone}")
    return len(items)


def ensure_universe(client, dcon, log, now) -> None:
    r = dcon.execute("SELECT value FROM meta WHERE key='universe_refreshed'").fetchone()
    if not r or r[0] != now().date().isoformat():
        refresh_universe(client, dcon, log, now)


def active_codes(dcon, day: str | None = None) -> list[str]:
    """day가 있으면 그날 이미 상장된 종목만(상장일 모르면 포함)."""
    if day:
        q = "SELECT code FROM universe WHERE active=1 AND (list_date IS NULL OR list_date <= ?) ORDER BY code"
        return [r[0] for r in dcon.execute(q, (day,))]
    return [r[0] for r in dcon.execute("SELECT code FROM universe WHERE active=1 ORDER BY code")]


# ---------------------------------------------------------------- 거래일
def previous_business_day(client) -> str:
    cal = client.get("/api/v1/market-calendar/KR").get("result") or {}
    d = (cal.get("previousBusinessDay") or {}).get("date")
    if not d:
        raise SystemExit("전 영업일을 알 수 없습니다 (market-calendar/KR).")
    return d


def refresh_trading_days(client, dcon, log: Log, since: str) -> int:
    """KOSPI·KOSDAQ 지수 일봉을 저장하고(index_daily), KOSPI 지수 일봉 날짜 = 거래일. since(YYYY-MM-DD)까지 과거로.
    지수 캔들 timestamp는 봉 시작 시각이라 날짜 부분만 쓴다."""
    calls = 0
    for sym in ("KOSPI", "KOSDAQ"):
        have = dcon.execute("SELECT MIN(date), MAX(date) FROM index_daily WHERE symbol=?", (sym,)).fetchone()
        stop_at = datetime.fromisoformat(_kst(since, "00:00:00"))
        if have[0] and have[0] <= since:  # 과거는 이미 있음 → 마지막 날(진행 중이었을 수 있음)부터 다시
            stop_at = datetime.fromisoformat(_kst(have[1], "00:00:00"))
        r = page_candles(client, f"/api/v1/market-indicators/{sym}/candles", {"interval": "1d", "count": 200}, 20,
                         stop_when=lambda t: t < stop_at)
        calls += r.calls
        rows = [(sym, parse_ts(b["timestamp"]).date().isoformat(), ds.num(b.get("openPrice")), ds.num(b.get("highPrice")),
                 ds.num(b.get("lowPrice")), ds.num(b.get("closePrice")), ds.num(b.get("volume"))) for b in r.bars]
        with dcon:
            dcon.executemany("INSERT OR REPLACE INTO index_daily VALUES (?,?,?,?,?,?,?)", rows)
            if sym == "KOSPI":
                dcon.executemany("INSERT OR IGNORE INTO trading_day VALUES (?, 'KOSPI_1d')", [(x[1],) for x in rows])
    n = dcon.execute("SELECT COUNT(*) FROM trading_day").fetchone()[0]
    log(f"[trading-day] KOSPI·KOSDAQ 지수 일봉 저장, 거래일 갱신 (호출 {calls}회, 누적 {n}일)")
    return n


def trading_days(dcon, start: str, end: str) -> list[str]:
    return [r[0] for r in dcon.execute(
        "SELECT date FROM trading_day WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))]


# ---------------------------------------------------------------- 일봉
def minute_values(store_dir: Path, day: str) -> dict[str, int]:
    """완성된 분봉 파일에서 종목별 Σ(종가×거래량) = 추정 거래대금."""
    if ds.minute_status(store_dir, day) != "complete":
        return {}
    con = sqlite3.connect(f"{ds.minute_path(store_dir, day).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {c: int(v) for c, v in con.execute("SELECT code, SUM(close * volume) FROM bar GROUP BY code") if v is not None}
    finally:
        con.close()


def _fetch_daily(client, code: str, start: str, end: str) -> list[dict]:
    stop = datetime.fromisoformat(_kst(start, "00:00:00"))
    r = page_candles(client, CANDLES, {"symbol": code, "interval": "1d", "count": 200, "adjusted": "false",
                                       "before": _kst(end, "23:59:59")}, 10, stop_when=lambda t: t < stop)
    out = []
    for b in r.bars:
        d = parse_ts(b["timestamp"]).date().isoformat()
        if start <= d <= end:
            out.append((d, b))
    return out


def _fetch_investor(client, code: str, start: str, end: str) -> list[tuple]:
    p = {"count": 100, "until": end}
    out = []
    for _ in range(20):
        r = client.get(f"/api/v1/stocks/{code}/investor-trading", p).get("result") or {}
        recs = r.get("records") or []
        for x in recs:
            d = x.get("date")
            if d and start <= d <= end:
                g = lambda k: ds.num(((x.get(k) or {}) or {}).get("netBuyVolume"))
                out.append((code, d, g("foreigner"), g("institution"), g("individual"), g("otherCorporation"),
                            x.get("updatedAt")))
        nu = r.get("nextUntil")
        if not recs or not nu or nu < start or min(x.get("date") or end for x in recs) < start:
            break
        p["until"] = nu
    return out


def daily_update(client, dcon, store_dir: Path, log: Log, now, history_days: int = 365,
                 investor: bool = False, end: str | None = None) -> dict:
    """전 영업일까지 일봉(+선택: 수급)을 보충한다. 종목별 진행 기록(progress)으로 이어받는다."""
    ensure_universe(client, dcon, log, now)
    end = end or previous_business_day(client)
    first = (now().date() - timedelta(days=history_days)).isoformat()
    refresh_trading_days(client, dcon, log, first)
    codes = active_codes(dcon)
    log(f"[daily-update] 기준일 {end}, 처음 받는 종목은 {first}부터, 대상 {len(codes):,}종목, 수급 {'포함' if investor else '제외'}")
    vcache: dict[str, dict] = {}
    t0, c0 = time.monotonic(), client.calls
    done = rows = fails = 0
    for i, code in enumerate(codes, 1):
        for kind in (("daily", "investor") if investor else ("daily",)):
            r = dcon.execute("SELECT last_date FROM progress WHERE kind=? AND code=?", (kind, code)).fetchone()
            start = (Date.fromisoformat(r[0]) + timedelta(days=1)).isoformat() if r else first
            if start > end:
                continue
            try:
                if kind == "daily":
                    got = _fetch_daily(client, code, start, end)
                    recs = []
                    for d, b in got:
                        if d not in vcache:
                            vcache[d] = minute_values(store_dir, d)
                        close, vol = ds.num(b.get("closePrice")), ds.num(b.get("volume"))
                        mv = vcache[d].get(code)
                        est, src = (mv, "minute") if mv is not None else (
                            (int(close * vol), "daily_close") if close is not None and vol is not None else (None, None))
                        recs.append((code, d, ds.num(b.get("openPrice")), ds.num(b.get("highPrice")),
                                     ds.num(b.get("lowPrice")), close, vol, est, src))
                    sql = "INSERT OR REPLACE INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?)"
                else:
                    recs = _fetch_investor(client, code, start, end)
                    sql = "INSERT OR REPLACE INTO investor_daily VALUES (?,?,?,?,?,?,?)"
                with dcon:
                    dcon.executemany(sql, recs)
                    dcon.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?,?)", (kind, code, end, _ts(now)))
                rows += len(recs)
                done += 1
            except (NotAllowedError, TossAuthError):
                raise
            except Exception as exc:  # noqa: BLE001  다음 실행 때 같은 범위를 다시 시도
                fails += 1
                log(f"  실패 {code} {kind}: {exc}")
        if i % 300 == 0:
            el = time.monotonic() - t0
            log(f"  진행 {i}/{len(codes)}, 호출 {client.calls - c0}, {(client.calls - c0) / el if el else 0:.1f}/초, "
                f"429 {client.n429}, 남은 예상 {timedelta(seconds=int(el / i * (len(codes) - i)))}")
    el = time.monotonic() - t0
    log(f"[daily-update] 완료: 갱신 {done}건, 저장 {rows:,}행, 실패 {fails}, 호출 {client.calls - c0}회, {el:.0f}초")
    try:
        index_minute_update(client, dcon, log, now, history_days, end)
    except (NotAllowedError, TossAuthError):
        raise
    except Exception as exc:  # noqa: BLE001  지수 분봉 실패가 아침 일봉 보충을 막지 않게
        log(f"[index-minute] 실패 (다음 실행 때 다시 시도): {exc}")
    return {"end": end, "done": done, "rows": rows, "fails": fails, "calls": client.calls - c0}


# ---------------------------------------------------------------- 수정주가 (처음 1회 + 주 1회)
def adjust_due(dcon, now, every_days: int) -> bool:
    r = dcon.execute("SELECT value FROM meta WHERE key='adj_refreshed'").fetchone()
    return not r or (now().date() - Date.fromisoformat(r[0])).days >= every_days


def adjust_update(client, dcon, log: Log, now, history_days: int = 365, end: str | None = None,
                  max_minutes: float = 0, clock: Callable[[], float] = time.monotonic) -> dict:
    """수정주가(adjusted=true) 일봉 종가를 1년치 다시 받아 adj_daily를 통째로 갱신한다.
    수정주가는 분할·병합 등이 생기면 과거 값이 소급해 바뀌므로 매번 전체를 받는다(종목당 약 2회 호출)."""
    end = end or previous_business_day(client)
    first = (now().date() - timedelta(days=history_days)).isoformat()
    codes = active_codes(dcon)
    log(f"[adjust] 수정주가 종가 갱신: {len(codes):,}종목, {first}~{end} (주 1회)")
    t0, c0, n = clock(), client.calls, 0
    today = now().date().isoformat()
    for i, code in enumerate(codes, 1):
        if max_minutes and clock() - t0 > max_minutes * 60:
            log(f"[adjust] 실행 시간 제한으로 중단 ({i - 1}/{len(codes)}). 다음 실행 때 처음부터 다시 함")
            return {"done": False, "codes": i - 1}
        stop = datetime.fromisoformat(_kst(first, "00:00:00"))
        r = page_candles(client, CANDLES, {"symbol": code, "interval": "1d", "count": 200, "adjusted": "true",
                                           "before": _kst(end, "23:59:59")}, 10, stop_when=lambda t: t < stop)
        rows = []
        for b in r.bars:
            d = parse_ts(b["timestamp"]).date().isoformat()
            if first <= d <= end:
                rows.append((code, d, ds.num(b.get("closePrice")), today))
        with dcon:
            dcon.execute("DELETE FROM adj_daily WHERE code=?", (code,))
            dcon.executemany("INSERT INTO adj_daily(code, date, adj_close, as_of) VALUES (?,?,?,?)", rows)
            dcon.execute("UPDATE adj_daily SET adj_factor = adj_close * 1.0 / (SELECT close FROM daily_bar b"
                         " WHERE b.code=adj_daily.code AND b.date=adj_daily.date AND b.close > 0) WHERE code=?", (code,))
        n += len(rows)
        if i % 300 == 0:
            el = clock() - t0
            log(f"  진행 {i}/{len(codes)}, 호출 {client.calls - c0}, 남은 예상 {timedelta(seconds=int(el / i * (len(codes) - i)))}")
    with dcon:
        dcon.execute("INSERT OR REPLACE INTO meta VALUES ('adj_refreshed', ?)", (today,))
    events = dcon.execute(
        "SELECT COUNT(*) FROM (SELECT code, date, adj_factor, LAG(adj_factor) OVER (PARTITION BY code ORDER BY date) p"
        " FROM adj_daily) WHERE p IS NOT NULL AND ABS(adj_factor / p - 1) > 0.001").fetchone()[0]
    log(f"[adjust] 완료: {n:,}행, 호출 {client.calls - c0}회, 수정계수가 바뀐 날(분할·병합·권리락 등 추정) {events}건")
    return {"done": True, "rows": n, "events": events}


# ---------------------------------------------------------------- 분봉
def keep_bar(t: int, volume) -> bool:
    """정규장 봉은 모두, 장전·장후 봉은 거래량이 있을 때만 저장."""
    return REG_START <= t <= REG_END or (volume or 0) > 0


def fetch_minute_day(client, code: str, day: str) -> tuple[list[tuple], int]:
    """그날(day, YYYY-MM-DD) 분봉 → [(code, t, o, h, l, c, v)], 호출 수. timestamp(종료 시각) - 1분 = 시작 시각."""
    first_end = datetime.fromisoformat(_kst(day, "08:01:00"))
    r = page_candles(client, CANDLES, {"symbol": code, "interval": "1m", "count": 200, "adjusted": "false",
                                       "before": _kst(day, "20:00:00")}, 10, stop_when=lambda t: t <= first_end)
    out = []
    for b in r.bars:
        st = parse_ts(b["timestamp"]) - timedelta(minutes=1)
        if st.date().isoformat() != day:
            continue
        t = st.hour * 60 + st.minute
        v = ds.num(b.get("volume")) or 0
        if keep_bar(t, v):
            out.append((code, t, ds.num(b.get("openPrice")), ds.num(b.get("highPrice")), ds.num(b.get("lowPrice")),
                        ds.num(b.get("closePrice")), int(v)))
    return out, r.calls


def _finish_day(mcon, dcon, store_dir, day: str, total: int, now, log: Log) -> None:
    given_up = mcon.execute("SELECT COUNT(*) FROM code_fail WHERE code NOT IN (SELECT code FROM code_done)"
                            " AND fail_count >= (SELECT CAST(value AS INTEGER) FROM file_meta WHERE key='max_retry')"
                            ).fetchone()[0]
    with mcon:
        for k, v in (("complete", "1"), ("n_codes", str(total)), ("n_given_up", str(given_up)),
                     ("finished_at", _ts(now))):
            mcon.execute("INSERT OR REPLACE INTO file_meta VALUES (?,?)", (k, v))
    vals = {c: int(v) for c, v in mcon.execute("SELECT code, SUM(close * volume) FROM bar GROUP BY code") if v is not None}
    with dcon:
        dcon.executemany("UPDATE daily_bar SET value_est_krw=?, value_est_src='minute' WHERE code=? AND date=?",
                         [(v, c, day) for c, v in vals.items()])
    log(f"  {day} 완료: {total}종목 중 포기 {given_up}, 일봉 추정 거래대금 {len(vals)}종목 분봉 기준으로 갱신")


def minute_update(client, dcon, store_dir: Path, log: Log, now, history_days: int = 365,
                  max_minutes: float = 0, max_retry: int = 3, end: str | None = None, only_day: str | None = None,
                  clock: Callable[[], float] = time.monotonic) -> dict:
    """분봉 날짜 파일 채우기. 전 영업일부터 과거 방향으로(최근 날짜 우선). 종목 단위로 이어받는다."""
    ensure_universe(client, dcon, log, now)
    end = end or previous_business_day(client)
    first = (now().date() - timedelta(days=history_days)).isoformat()
    refresh_trading_days(client, dcon, log, first)
    days = [only_day] if only_day else [d for d in reversed(trading_days(dcon, first, end))
                                        if ds.minute_status(store_dir, d) != "complete"]
    log(f"[minute-update] 대상 날짜 {len(days)}일 (최근부터), 범위 {first}~{end}"
        + (f", 실행 시간 제한 {max_minutes:g}분" if max_minutes else ""))
    t0, c0 = clock(), client.calls
    out = {"days_done": [], "stop": ""}
    for day in days:
        codes = active_codes(dcon, day)
        mcon = ds.connect_minute(store_dir, day)
        with mcon:
            mcon.execute("INSERT OR REPLACE INTO file_meta VALUES ('max_retry', ?)", (str(max_retry),))
        done = {r[0] for r in mcon.execute("SELECT code FROM code_done")}
        dead = {r[0] for r in mcon.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
        todo = [c for c in codes if c not in done and c not in dead]
        log(f"[minute-update] {day}: {len(codes)}종목 중 남은 {len(todo)}")
        for i, code in enumerate(todo, 1):
            if max_minutes and clock() - t0 > max_minutes * 60:
                out["stop"] = f"실행 시간 제한 {max_minutes:g}분"
                break
            try:
                bars, calls = fetch_minute_day(client, code, day)
                with mcon:
                    mcon.executemany("INSERT OR REPLACE INTO bar VALUES (?,?,?,?,?,?,?)", bars)
                    mcon.execute("INSERT OR REPLACE INTO code_done VALUES (?,?,?,?)", (code, len(bars), calls, _ts(now)))
                    mcon.execute("DELETE FROM code_fail WHERE code=?", (code,))
            except (NotAllowedError, TossAuthError):
                mcon.close()
                raise
            except Exception as exc:  # noqa: BLE001
                with mcon:
                    mcon.execute("INSERT INTO code_fail VALUES (?,1,?,?) ON CONFLICT(code) DO UPDATE SET"
                                 " fail_count=fail_count+1, last_error=excluded.last_error, updated_at=excluded.updated_at",
                                 (code, str(exc)[:300], _ts(now)))
                log(f"  실패 {day} {code}: {exc}")
            if i % 300 == 0:
                el = clock() - t0
                log(f"  {day} 진행 {i}/{len(todo)}, 호출 {client.calls - c0}, "
                    f"{(client.calls - c0) / el if el else 0:.1f}/초, 429 {client.n429}")
        if out["stop"]:
            mcon.close()
            break
        done = {r[0] for r in mcon.execute("SELECT code FROM code_done")}
        dead = {r[0] for r in mcon.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
        if all(c in done or c in dead for c in codes):
            _finish_day(mcon, dcon, store_dir, day, len(codes), now, log)
            out["days_done"].append(day)
        else:
            log(f"  {day}: 실패한 종목이 있어 미완성으로 둠 (다음 실행 때 다시 시도)")
        mcon.close()
    el = clock() - t0
    log(f"[minute-update] 이번 실행 완성 {len(out['days_done'])}일, 호출 {client.calls - c0}회, {el:.0f}초"
        + (f", 멈춘 이유: {out['stop']}" if out["stop"] else ""))
    out["calls"] = client.calls - c0
    return out


# ---------------------------------------------------------------- 현황
def status(dcon, store_dir: Path, log: Log, now, history_days: int = 365) -> dict:
    first = (now().date() - timedelta(days=history_days)).isoformat()
    last = dcon.execute("SELECT MAX(date) FROM trading_day WHERE date < ?", (now().date().isoformat(),)).fetchone()[0]
    uni = dcon.execute("SELECT COUNT(*), SUM(active) FROM universe").fetchone()
    dmin, dmax, drows = dcon.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM daily_bar").fetchone()
    lag = dcon.execute("SELECT COUNT(*) FROM progress WHERE kind='daily' AND last_date < ?", (last or "",)).fetchone()[0]
    inv = dcon.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM investor_daily").fetchone()
    days = trading_days(dcon, first, last) if last else []
    st = {"complete": [], "partial": [], "missing": []}
    focus_n = 0
    for d in days:
        st[ds.minute_status(store_dir, d)].append(d)
        focus_n += focus_done(store_dir, d)
    size = sum(p.stat().st_size for p in (store_dir / "minute").rglob("*.sqlite")) if (store_dir / "minute").exists() else 0
    log(f"[status] 종목 목록 {uni[1] or 0:,}종목 (누적 {uni[0]:,})")
    log(f"[status] 일봉 {dmin}~{dmax}, {drows:,}행, 전 거래일({last})까지 안 받은 종목 {lag}")
    log(f"[status] 수급 {inv[0]}~{inv[1]}, {inv[2]:,}행")
    for sym, lo, hi, n in dcon.execute("SELECT symbol, MIN(date), MAX(date), COUNT(*) FROM index_minute GROUP BY symbol"):
        log(f"[status] 지수 분봉 {sym}: {lo}~{hi}, {n:,}봉")
    log(f"[status] 분봉 ({first}~{last}, 거래일 {len(days)}일): 완성 {len(st['complete'])}, 진행 중 {len(st['partial'])}, "
        f"없음 {len(st['missing'])}, 파일 합계 {size / 1024 / 1024:,.1f}MB")
    log(f"[status] 우선 수집(거래대금 상위 100 + 등락률 상위 50 + W7) 끝난 날 {focus_n}/{len(days)}일")
    if st["missing"]:
        log(f"  없는 날짜(최근 5): {st['missing'][-5:]}")
    return {"last": last, "minute": {k: len(v) for k, v in st.items()}, "minute_mb": size / 1024 / 1024, "focus": focus_n}


# ---------------------------------------------------------------- 분봉 과거 채우기 (빨리 채우기)
def _needed(dcon, store_dir: Path, days: list[str], max_retry: int) -> tuple[dict, dict]:
    """종목별로 아직 안 받은 날짜 집합. 반환: ({code: set(day)}, {day: [그날 대상 종목]})"""
    per_code: dict[str, set] = {}
    targets: dict[str, list] = {}
    for day in days:
        codes = active_codes(dcon, day)
        targets[day] = codes
        done, dead = set(), set()
        if ds.minute_path(store_dir, day).exists():
            con = sqlite3.connect(f"{ds.minute_path(store_dir, day).resolve().as_uri()}?mode=ro", uri=True)
            try:
                done = {r[0] for r in con.execute("SELECT code FROM code_done")}
                dead = {r[0] for r in con.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
            finally:
                con.close()
        for c in codes:
            if c not in done and c not in dead:
                per_code.setdefault(c, set()).add(day)
    return per_code, targets


def _backfill_code(client, code: str, need: set, conns: dict, store_dir: Path, now) -> int:
    """종목 하나를 need 날짜 전체에 걸쳐 연속으로 받는다(200봉씩 과거로).
    페이지를 받을 때마다, 더 과거 봉이 나온 날짜는 그날 분봉이 다 모인 것이므로 바로 그날 파일에 저장한다."""
    lo, hi = min(need), max(need)
    buf: dict[str, list] = {d: [] for d in need}
    flushed: set = set()

    def flush(d):
        con = conns.get(d) or conns.setdefault(d, ds.connect_minute(store_dir, d))
        with con:
            con.executemany("INSERT OR REPLACE INTO bar VALUES (?,?,?,?,?,?,?)", buf[d])
            con.execute("INSERT OR REPLACE INTO code_done VALUES (?,?,?,?)", (code, len(buf[d]), 0, _ts(now)))
            con.execute("DELETE FROM code_fail WHERE code=?", (code,))
        flushed.add(d)
        buf[d] = []

    p = {"symbol": code, "interval": "1m", "count": 200, "adjusted": "false", "before": _kst(hi, "20:00:00")}
    calls, seen = 0, set()
    while True:
        body = client.get(CANDLES, p)
        calls += 1
        r = body.get("result") or {}
        bars = r.get("candles") or []
        oldest = None
        for b in bars:
            st = parse_ts(b["timestamp"]) - timedelta(minutes=1)
            d = st.date().isoformat()
            oldest = d if oldest is None or d < oldest else oldest
            if d in buf and d not in flushed:
                t = st.hour * 60 + st.minute
                v = ds.num(b.get("volume")) or 0
                if keep_bar(t, v):
                    buf[d].append((code, t, ds.num(b.get("openPrice")), ds.num(b.get("highPrice")),
                                   ds.num(b.get("lowPrice")), ds.num(b.get("closePrice")), int(v)))
        nb = r.get("nextBefore")
        end_of_data = not bars or not nb or nb in seen
        for d in sorted(need - flushed, reverse=True):   # 이 페이지보다 최근인 날짜는 다 모였음
            if end_of_data or (oldest is not None and d > oldest):
                flush(d)
        if end_of_data or (oldest is not None and oldest < lo):
            for d in need - flushed:
                flush(d)
            break
        seen.add(nb)
        p["before"] = nb
    return calls


def minute_backfill(client, dcon, store_dir: Path, log: Log, now, history_days: int = 365,
                    max_minutes: float = 0, max_retry: int = 3, end: str | None = None,
                    clock: Callable[[], float] = time.monotonic, chunk_days: int = 20, focus: bool = True) -> dict:
    """우선 수집(focus=True면 먼저) → 빨리 채우기: 가장 최근 미완성 날짜는 날짜 단위로 먼저 완성하고, 나머지 과거 날짜는
    최근 구간(chunk_days 거래일)부터 종목 단위로 연속 수집한다. 구간 하나가 끝나면 그 구간 날짜들이 완성된다."""
    t0 = clock()
    ensure_universe(client, dcon, log, now)
    end = end or previous_business_day(client)
    first = (now().date() - timedelta(days=history_days)).isoformat()
    refresh_trading_days(client, dcon, log, first)
    all_days = [r[0] for r in dcon.execute("SELECT date FROM trading_day ORDER BY date")]
    days = [d for d in trading_days(dcon, first, end) if ds.minute_status(store_dir, d) != "complete"]
    out = {"days_done": [], "stop": "", "calls": 0}
    if not days:
        log("[minute-backfill] 채울 날짜 없음")
        return out
    if focus:  # 먼저 KIS 때와 같은 범위(우선 종목)를 전 기간 받고, 그다음 전 종목 채우기
        f = focus_phase(client, dcon, store_dir, log, now, days, all_days, max_minutes, max_retry, t0, clock)
        out["calls"] += f["calls"]
        if f["stop"]:
            out["stop"] = f["stop"]
            return out
    newest = days[-1]
    left = lambda: (max_minutes - (clock() - t0) / 60) if max_minutes else 0
    log(f"[minute-backfill] 미완성 {len(days)}일. 먼저 최근 날짜 {newest}를 날짜 단위로 완성합니다")
    r = minute_update(client, dcon, store_dir, log, now, history_days, max(left(), 0.01) if max_minutes else 0,
                      max_retry, end=end, only_day=newest, clock=clock)
    out["days_done"] += r["days_done"]
    out["calls"] += r["calls"]
    if r["stop"]:
        out["stop"] = r["stop"]
        return out
    rest = [d for d in days if d != newest]
    if not rest:
        return out
    # 최근 구간부터: 20거래일(chunk_days) 단위로 나눠, 한 구간을 종목 단위로 끝까지 채워 완성한 뒤 이전 구간으로
    rest_desc = list(reversed(rest))
    chunks = [sorted(rest_desc[k:k + chunk_days]) for k in range(0, len(rest_desc), chunk_days)]
    log(f"[minute-backfill] 과거 미완성 {len(rest)}일을 {chunk_days}거래일 구간 {len(chunks)}개로 나눠 최근 구간부터 채웁니다")
    c0, t1 = client.calls, clock()
    for ci, chunk in enumerate(chunks, 1):
        per_code, targets = _needed(dcon, store_dir, chunk, max_retry)
        codes = sorted(per_code)
        log(f"[minute-backfill] 구간 {ci}/{len(chunks)}: {chunk[0]}~{chunk[-1]} ({len(chunk)}일), 대상 {len(codes)}종목")
        conns: dict = {}
        tc, cc = clock(), client.calls
        try:
            for i, code in enumerate(codes, 1):
                if max_minutes and clock() - t0 > max_minutes * 60:
                    out["stop"] = f"실행 시간 제한 {max_minutes:g}분"
                    break
                try:
                    _backfill_code(client, code, per_code[code], conns, store_dir, now)
                except (NotAllowedError, TossAuthError):
                    raise
                except Exception as exc:  # noqa: BLE001  이 종목의 남은 날짜마다 실패 1회 기록
                    log(f"  실패 {code}: {exc}")
                    for d in per_code[code]:
                        con = conns.get(d) or conns.setdefault(d, ds.connect_minute(store_dir, d))
                        with con:
                            if not con.execute("SELECT 1 FROM code_done WHERE code=?", (code,)).fetchone():
                                con.execute("INSERT INTO code_fail VALUES (?,1,?,?) ON CONFLICT(code) DO UPDATE SET"
                                            " fail_count=fail_count+1, last_error=excluded.last_error,"
                                            " updated_at=excluded.updated_at", (code, str(exc)[:300], _ts(now)))
                if i % 50 == 0:
                    el = clock() - tc
                    n = client.calls - cc
                    log(f"  구간 {ci} 진행 {i}/{len(codes)}종목, 호출 {n}, {n / el if el else 0:.1f}/초, 429 {client.n429}, "
                        f"이 구간 남은 예상 {timedelta(seconds=int(el / i * (len(codes) - i)))}")
            for day in chunk:  # 다 모인 날짜는 완성 처리
                con = conns.get(day) or (ds.connect_minute(store_dir, day)
                                         if ds.minute_path(store_dir, day).exists() else None)
                if con is None:
                    continue
                conns[day] = con
                with con:
                    con.execute("INSERT OR REPLACE INTO file_meta VALUES ('max_retry', ?)", (str(max_retry),))
                done = {r[0] for r in con.execute("SELECT code FROM code_done")}
                dead = {r[0] for r in con.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
                if all(c in done or c in dead for c in targets[day]):
                    _finish_day(con, dcon, store_dir, day, len(targets[day]), now, log)
                    out["days_done"].append(day)
        finally:
            for con in conns.values():
                con.close()
        if out["stop"]:
            break
        log(f"[minute-backfill] 구간 {ci} 끝 ({chunk[0]}~{chunk[-1]})")
    out["calls"] += client.calls - c0
    log(f"[minute-backfill] 이번 실행 완성 {len(out['days_done'])}일, 호출 {out['calls']}회"
        + (f", 멈춘 이유: {out['stop']}" if out["stop"] else ""))
    return out


# ---------------------------------------------------------------- 우선 수집 (KIS 때와 같은 범위 먼저)
FOCUS_TOP_VALUE = 100
FOCUS_TOP_CHANGE = 50


def focus_targets(dcon, days: list[str], all_days: list[str], watch_settings=None) -> dict[str, set]:
    """날짜별 우선 수집 종목.
    - 그날 일봉 기준 추정 거래대금 상위 100 + 등락률 상위 50 (거래량 있는 종목)
    - 그날 W7 관심종목 (전날까지의 일봉으로 고름)
    - 다음 날 W7 관심종목 (그 종목의 '전날 정규장 종가'를 이 날 분봉에서 구해야 하므로)
    """
    from .trade.settings import TradeSettings
    from .trade.watchlist import build_watchlist
    ws = watch_settings or TradeSettings()
    out: dict[str, set] = {}
    idx = {d: i for i, d in enumerate(all_days)}
    w7_cache: dict[str, list] = {}

    def w7(day):
        if day not in w7_cache:
            w7_cache[day] = [w.code for w in build_watchlist(dcon, day, ws)[0]]
        return w7_cache[day]

    for day in days:
        codes = {r[0] for r in dcon.execute(
            "SELECT code FROM daily_bar WHERE date=? AND volume > 0 ORDER BY value_est_krw DESC, code LIMIT ?",
            (day, FOCUS_TOP_VALUE))}
        i = idx.get(day)
        if i:
            prev = all_days[i - 1]
            codes |= {r[0] for r in dcon.execute(
                "SELECT b.code FROM daily_bar b JOIN daily_bar p ON p.code=b.code AND p.date=?"
                " WHERE b.date=? AND b.volume > 0 AND p.close > 0 ORDER BY b.close * 1.0 / p.close DESC, b.code LIMIT ?",
                (prev, day, FOCUS_TOP_CHANGE))}
        codes |= set(w7(day))
        if i is not None and i + 1 < len(all_days):
            codes |= set(w7(all_days[i + 1]))
        out[day] = codes
    return out


def focus_done(store_dir: Path, day: str) -> bool:
    p = ds.minute_path(store_dir, day)
    if not p.exists():
        return False
    con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT value FROM file_meta WHERE key IN ('focus_done', 'complete') AND value='1'").fetchone()
        return r is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def focus_phase(client, dcon, store_dir: Path, log: Log, now, days: list[str], all_days: list[str],
                max_minutes: float = 0, max_retry: int = 3, t0: float | None = None,
                clock: Callable[[], float] = time.monotonic) -> dict:
    """우선 수집: 최근 날짜부터, 날짜마다 우선 종목만 날짜 단위로 받는다. 다 받은 날은 file_meta.focus_done=1."""
    t0 = clock() if t0 is None else t0
    todo_days = [d for d in reversed(days) if not focus_done(store_dir, d)]
    out = {"days": [], "stop": "", "calls": 0}
    if not todo_days:
        return out
    targets = focus_targets(dcon, todo_days, all_days)
    total = sum(len(v) for v in targets.values())
    log(f"[focus] 우선 수집: {len(todo_days)}일, 종목·날짜 약 {total:,}건 (거래대금 상위 {FOCUS_TOP_VALUE} + 등락률 상위 "
        f"{FOCUS_TOP_CHANGE} + W7 관심종목과 다음 날 W7의 전날 분)")
    c0 = client.calls
    for k, day in enumerate(todo_days, 1):
        listed = set(active_codes(dcon, day))
        codes = sorted(c for c in targets[day] if c in listed)
        mcon = ds.connect_minute(store_dir, day)
        try:
            complete = _fetch_codes(client, mcon, day, codes, log, now, max_minutes, max_retry, t0, clock, out)
            if out["stop"]:
                break
            if complete:
                with mcon:
                    mcon.execute("INSERT OR REPLACE INTO file_meta VALUES ('focus_done', '1')")
                    mcon.execute("INSERT OR REPLACE INTO file_meta VALUES ('focus_codes', ?)", (str(len(codes)),))
                out["days"].append(day)
        finally:
            mcon.close()
        if k % 10 == 0:
            el = clock() - t0
            n = client.calls - c0
            log(f"  [focus] {k}/{len(todo_days)}일 ({day}까지), 호출 {n}, {n / el if el else 0:.1f}/초, 429 {client.n429}, "
                f"남은 예상 {timedelta(seconds=int(el / k * (len(todo_days) - k)))}")
    out["calls"] = client.calls - c0
    log(f"[focus] 이번 실행 우선 수집 끝난 날 {len(out['days'])}일, 호출 {out['calls']}회"
        + (f", 멈춘 이유: {out['stop']}" if out["stop"] else ""))
    return out


def _fetch_codes(client, mcon, day: str, codes, log: Log, now, max_minutes: float, max_retry: int,
                 t0: float, clock, out: dict) -> bool:
    """그날 파일에 codes를 받는다(이미 받은 것·포기한 것은 건너뜀). 시간 제한이면 out['stop']을 채운다.
    반환: codes가 모두 받아졌거나 포기됐는지."""
    done = {r[0] for r in mcon.execute("SELECT code FROM code_done")}
    dead = {r[0] for r in mcon.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
    for code in codes:
        if code in done or code in dead:
            continue
        if max_minutes and clock() - t0 > max_minutes * 60:
            out["stop"] = f"실행 시간 제한 {max_minutes:g}분"
            return False
        try:
            bars, calls = fetch_minute_day(client, code, day)
            with mcon:
                mcon.execute("INSERT OR REPLACE INTO file_meta VALUES ('max_retry', ?)", (str(max_retry),))
                mcon.executemany("INSERT OR REPLACE INTO bar VALUES (?,?,?,?,?,?,?)", bars)
                mcon.execute("INSERT OR REPLACE INTO code_done VALUES (?,?,?,?)", (code, len(bars), calls, _ts(now)))
                mcon.execute("DELETE FROM code_fail WHERE code=?", (code,))
        except (NotAllowedError, TossAuthError):
            raise
        except Exception as exc:  # noqa: BLE001
            with mcon:
                mcon.execute("INSERT INTO code_fail VALUES (?,1,?,?) ON CONFLICT(code) DO UPDATE SET"
                             " fail_count=fail_count+1, last_error=excluded.last_error,"
                             " updated_at=excluded.updated_at", (code, str(exc)[:300], _ts(now)))
            log(f"  실패 {day} {code}: {exc}")
    done = {r[0] for r in mcon.execute("SELECT code FROM code_done")}
    dead = {r[0] for r in mcon.execute("SELECT code FROM code_fail WHERE fail_count >= ?", (max_retry,))}
    return all(c in done or c in dead for c in codes)


# ---------------------------------------------------------------- 지정 목록 수집 (--targets CSV)
def read_targets(path: Path) -> dict[str, set]:
    """CSV(date,code)를 읽는다. 머리글은 있어도 없어도 된다. 날짜는 YYYY-MM-DD 또는 YYYYMMDD,
    종목코드는 앞자리 0이 빠졌으면 6자리로 채운다. 잘못된 줄은 오류로 멈춘다(조용히 건너뛰지 않음)."""
    import csv
    out: dict[str, set] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for n, row in enumerate(csv.reader(f), 1):
            row = [x.strip() for x in row]
            if not row or not any(row):
                continue
            if n == 1 and not row[0][:1].isdigit():
                continue  # 머리글
            if len(row) < 2:
                raise ValueError(f"{n}번째 줄: date,code 두 칸이 필요합니다: {row}")
            d, c = row[0].replace("-", ""), row[1]
            if not (len(d) == 8 and d.isdigit()) or not c.isalnum() or len(c) > 6:
                raise ValueError(f"{n}번째 줄 형식 오류: {row}")
            out.setdefault(f"{d[:4]}-{d[4:6]}-{d[6:]}", set()).add(c.zfill(6) if c.isdigit() else c)
    return out


def targets_update(client, dcon, store_dir: Path, log: Log, now, path: Path, max_minutes: float = 0,
                   max_retry: int = 3, with_prev: bool = True, clock: Callable[[], float] = time.monotonic) -> dict:
    """지정 목록(날짜·종목)만 분봉을 받는다. with_prev면 같은 종목의 전 거래일도 받는다(전일 정규장 종가 계산용).
    목록 순서와 관계없이 최근 날짜부터 받는다. 완성 표시(complete·focus_done)는 바꾸지 않는다."""
    ensure_universe(client, dcon, log, now)
    want = read_targets(path)
    if not want:
        log("[targets] 목록이 비어 있습니다")
        return {"days": [], "stop": "", "calls": 0, "pairs": 0}
    first = min(want)
    end = previous_business_day(client)
    refresh_trading_days(client, dcon, log, first)
    all_days = trading_days(dcon, "2000-01-01", end)
    idx = {d: i for i, d in enumerate(all_days)}
    plan: dict[str, set] = {}
    skipped = {"not_trading_day": 0, "future": 0}
    for d, codes in want.items():
        if d > end:
            skipped["future"] += len(codes)
            continue
        if d not in idx:
            skipped["not_trading_day"] += len(codes)
            continue
        plan.setdefault(d, set()).update(codes)
        if with_prev and idx[d] > 0:
            plan.setdefault(all_days[idx[d] - 1], set()).update(codes)
    pairs = sum(len(v) for v in plan.values())
    log(f"[targets] 목록 {sum(len(v) for v in want.values()):,}건 ({len(want)}일)"
        + (", 전 거래일 포함" if with_prev else "") + f" → 받을 종목·날짜 {pairs:,}건, {len(plan)}일"
        + (f", 제외 {skipped}" if any(skipped.values()) else ""))
    t0, c0 = clock(), client.calls
    out = {"days": [], "stop": "", "calls": 0, "pairs": pairs, "skipped": skipped}
    days = sorted(plan, reverse=True)
    for k, day in enumerate(days, 1):
        listed = set(active_codes(dcon, day))
        codes = sorted(plan[day])
        unknown = [c for c in codes if c not in listed]
        if unknown:  # 목록에 없는 종목도 일단 받아 본다(그날 거래 종목일 수 있음). 수만 알린다.
            log(f"  {day}: 종목 목록에 없는 코드 {len(unknown)}개 (예: {unknown[:3]})")
        mcon = ds.connect_minute(store_dir, day)
        try:
            if _fetch_codes(client, mcon, day, codes, log, now, max_minutes, max_retry, t0, clock, out):
                out["days"].append(day)
        finally:
            mcon.close()
        if out["stop"]:
            break
        if k % 10 == 0:
            el = clock() - t0
            n = client.calls - c0
            log(f"  [targets] {k}/{len(days)}일 ({day}까지), 호출 {n}, {n / el if el else 0:.1f}/초, 429 {client.n429}, "
                f"남은 예상 {timedelta(seconds=int(el / k * (len(days) - k)))}")
    out["calls"] = client.calls - c0
    log(f"[targets] 끝난 날 {len(out['days'])}/{len(days)}일, 호출 {out['calls']}회"
        + (f", 멈춘 이유: {out['stop']}" if out["stop"] else ""))
    return out



# ---------------------------------------------------------------- 지수 분봉 (KOSPI·KOSDAQ)
INDEX_SYMBOLS = ("KOSPI", "KOSDAQ")


def index_minute_update(client, dcon, log: Log, now, history_days: int = 365, end: str | None = None) -> dict:
    """KOSPI·KOSDAQ 지수 1분봉을 index_minute에 채운다. 처음엔 history_days, 이후엔 마지막 저장일부터.
    지수 캔들 timestamp는 봉 '시작' 시각이므로 그대로 t로 쓴다(종목 캔들과 다름).
    호출 한도 그룹이 MARKET_INDICATOR_CHART라 종목 분봉 수집 속도에 영향이 없다."""
    end = end or previous_business_day(client)
    first = (now().date() - timedelta(days=history_days)).isoformat()
    out = {}
    for sym in INDEX_SYMBOLS:
        last = dcon.execute("SELECT MAX(date) FROM index_minute WHERE symbol=?", (sym,)).fetchone()[0]
        start = last if last and last >= first else first   # 마지막 날은 다시 받음(그때 진행 중이었을 수 있음)
        if last and last >= end:
            out[sym] = 0
            continue
        stop = datetime.fromisoformat(_kst(start, "00:00:00"))
        r = page_candles(client, f"/api/v1/market-indicators/{sym}/candles",
                         {"interval": "1m", "count": 200, "before": _kst(end, "23:59:59")}, 3000,
                         stop_when=lambda t: t < stop)
        rows = []
        for b in r.bars:
            st = parse_ts(b["timestamp"])
            d = st.date().isoformat()
            if start <= d <= end:
                rows.append((sym, d, st.hour * 60 + st.minute, ds.num(b.get("openPrice")), ds.num(b.get("highPrice")),
                             ds.num(b.get("lowPrice")), ds.num(b.get("closePrice")), ds.num(b.get("volume"))))
        with dcon:
            dcon.execute("DELETE FROM index_minute WHERE symbol=? AND date >= ?", (sym, start))
            dcon.executemany("INSERT OR REPLACE INTO index_minute VALUES (?,?,?,?,?,?,?,?)", rows)
        out[sym] = r.calls
        log(f"[index-minute] {sym}: {start}~{end}, {len(rows):,}봉, 호출 {r.calls}회")
    return out
