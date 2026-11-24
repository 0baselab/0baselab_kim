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
    list_dates = {}
    syms = [i["symbol"] for i in items]
    for k in range(0, len(syms), 200):  # stocks/all에는 상장일이 없어 stocks로 조회
        for s in client.get("/api/v1/stocks", {"symbols": ",".join(syms[k:k + 200])}).get("result") or []:
            list_dates[s.get("symbol")] = s.get("listDate")
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
        dcon.execute("INSERT OR REPLACE INTO meta VALUES ('universe_refreshed', ?)", (today,))
    gone = dcon.execute("SELECT COUNT(*) FROM universe WHERE active=0").fetchone()[0]
    log(f"[universe] 주식 보통주 {len(items):,}종목 (KOSPI·KOSDAQ), 목록에서 빠진 종목 누적 {gone}")
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
    """KOSPI 지수 일봉 날짜 = 거래일. since(YYYY-MM-DD)까지 과거로."""
    have = dcon.execute("SELECT MIN(date), MAX(date) FROM trading_day").fetchone()
    stop_at = datetime.fromisoformat(_kst(since, "00:00:00"))
    if have[0] and have[0] <= since:  # 과거는 이미 있음 → 최근 것만
        stop_at = datetime.fromisoformat(_kst(have[1], "00:00:00"))
    r = page_candles(client, "/api/v1/market-indicators/KOSPI/candles", {"interval": "1d", "count": 200}, 20,
                     stop_when=lambda t: t < stop_at)
    days = sorted({parse_ts(b["timestamp"]).date().isoformat() for b in r.bars})
    with dcon:
        dcon.executemany("INSERT OR IGNORE INTO trading_day VALUES (?, 'KOSPI_1d')", [(d,) for d in days])
    n = dcon.execute("SELECT COUNT(*) FROM trading_day").fetchone()[0]
    log(f"[trading-day] KOSPI 지수 일봉으로 거래일 갱신 (호출 {r.calls}회, 누적 {n}일)")
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
    return {"end": end, "done": done, "rows": rows, "fails": fails, "calls": client.calls - c0}


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
    for d in days:
        st[ds.minute_status(store_dir, d)].append(d)
    size = sum(p.stat().st_size for p in (store_dir / "minute").rglob("*.sqlite")) if (store_dir / "minute").exists() else 0
    log(f"[status] 종목 목록 {uni[1] or 0:,}종목 (누적 {uni[0]:,})")
    log(f"[status] 일봉 {dmin}~{dmax}, {drows:,}행, 전 거래일({last})까지 안 받은 종목 {lag}")
    log(f"[status] 수급 {inv[0]}~{inv[1]}, {inv[2]:,}행")
    log(f"[status] 분봉 ({first}~{last}, 거래일 {len(days)}일): 완성 {len(st['complete'])}, 진행 중 {len(st['partial'])}, "
        f"없음 {len(st['missing'])}, 파일 합계 {size / 1024 / 1024:,.1f}MB")
    if st["missing"]:
        log(f"  없는 날짜(최근 5): {st['missing'][-5:]}")
    return {"last": last, "minute": {k: len(v) for k, v in st.items()}, "minute_mb": size / 1024 / 1024}
