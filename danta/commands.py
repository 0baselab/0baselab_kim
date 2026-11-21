"""CLI 하위 명령 본체. 실행 결과는 log()로 화면과 로그 파일에 함께 남는다."""
from __future__ import annotations

import time
from collections import Counter
from datetime import date as Date, datetime, timedelta
from typing import Callable

from . import source, store
from .fetch import fetch_day, minute_params, minute_values
from .kis import KisAuthError, RateLimitError

Log = Callable[[str], None]


def ymd(d: Date) -> str:
    return d.strftime("%Y%m%d")


def parse_ymd(s: str) -> Date:
    return datetime.strptime(s, "%Y%m%d").date()


def one_year_ago(today: Date) -> Date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:  # 2월 29일
        return today.replace(year=today.year - 1, day=28)


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[2:4])


# ---------------------------------------------------------------- probe
def has_data_near(client, code: str, d: Date, window: int, market: str, pw: str) -> str | None:
    """d부터 과거로 window일 안에서 그날짜 행이 있는 가장 최근 날짜 (공휴일 오판 방지)."""
    for i in range(window):
        day = ymd(d - timedelta(days=i))
        body = client.minute(minute_params(code, day, "153000", market, pw))
        if any(r.get("stck_bsop_date") == day for r in (body.get("output2") or [])):
            return day
    return None


def find_oldest(client, code: str, recent: str, log: Log, market: str = "J", pw: str = "N",
                lookback_days: int = 400, window: int = 5) -> str | None:
    """데이터가 있는 가장 오래된 날짜를 이분 탐색으로 찾는다 (달력일 기준)."""
    hi = parse_ymd(recent)
    lo = hi - timedelta(days=lookback_days)
    found = has_data_near(client, code, lo, window, market, pw)
    if found:
        log(f"  {ymd(lo)} 부근에도 데이터 있음 → 보관 기간이 {lookback_days}일 이상 (더 과거는 조사 안 함)")
        return found
    # 불변식: lo 부근(window일)에는 데이터 없음, hi에는 데이터 있음
    while (hi - lo).days > 1:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        f = has_data_near(client, code, mid, window, market, pw)
        log(f"  {ymd(mid)}: {'있음 (' + f + ')' if f else '없음'}")
        if f:
            hi = parse_ymd(f)
        else:
            lo = mid
    return ymd(hi)


def cmd_probe(client, src_con, log: Log, today: Date, code: str = "005930", day: str | None = None,
              market: str = "J", pw: str = "N", oldest: bool = True) -> dict:
    if not day:
        if src_con is None:
            raise SystemExit("기존 수집기 DB가 없어 최근 거래일을 알 수 없습니다. --date YYYYMMDD로 지정하세요.")
        day = source.last_open_before(src_con, ymd(today))
        if not day:
            raise SystemExit("거래일 달력에 오늘 이전 개장일이 없습니다. --date로 지정하세요.")
    log(f"[probe] 종목 {code}, 날짜 {day}, 시장 {market}, FID_PW_DATA_INCU_YN={pw}")
    c0 = client.stats.calls
    res = fetch_day(client, code, day, market, pw)
    log(f"  봉 개수: {len(res.bars)}")
    log(f"  호출 수: {res.calls}")
    log(f"  다른 날짜 행(버림): {res.other_date_rows}")
    out = {"day": day, "bars": len(res.bars), "calls": res.calls}
    if res.bars:
        times = [b["time"] for b in res.bars]
        log(f"  첫 시각: {times[0]}, 마지막 시각: {times[-1]}")
        gaps = Counter(_minutes(b) - _minutes(a) for a, b in zip(times, times[1:]))
        log("  봉 간격 분포(분:개수): " + ", ".join(f"{k}:{v}" for k, v in sorted(gaps.items())))
        out["gaps"] = dict(gaps)
        acml = [b["acml_value_krw"] for b in res.bars]
        known = [a for a in acml if a is not None]
        monotonic = all(b >= a for a, b in zip(known, known[1:]))
        log(f"  acml_tr_pbmn 단조 증가(누적으로 보임): {'예' if monotonic else '아니오'}"
            f" (값 있음 {len(known)}/{len(acml)})")
        vals = [v for v in minute_values(res.bars) if v is not None]
        sum_min = sum(vals) if vals else None
        last = known[-1] if known else None
        dv = source.daily_value(src_con, day, code) if src_con is not None else None
        log(f"  마지막 봉 누적 거래대금: {last}")
        log(f"  1분 거래대금 하루 합계(계산): {sum_min}")
        log(f"  기존 일봉 DB value_krw: {dv}")
        if dv and sum_min is not None:
            diff = sum_min - dv
            log(f"  차이(분봉 합계 - 일봉): {diff:+,} ({diff / dv * 100:+.3f}%)")
        out.update(monotonic=monotonic, sum_min=sum_min, daily_value=dv)
    if oldest:
        log("[probe] 데이터가 있는 가장 오래된 날짜 탐색 (이분 탐색)")
        o = find_oldest(client, code, day, log, market, pw)
        log(f"  가장 오래된 날짜: {o}")
        out["oldest"] = o
    log(f"[probe] 총 호출 {client.stats.calls - c0}회, EGW00201 {client.stats.rate_limited}회")
    return out


# ---------------------------------------------------------------- ratetest
def cmd_ratetest(client, log: Log, day: str, code: str = "005930", stages=(5, 10, 15, 20, 25, 30),
                 seconds: float = 20, market: str = "J", pw: str = "N",
                 clock: Callable[[], float] = time.monotonic) -> list[dict]:
    client.token()  # 토큰 발급은 측정 시간에 넣지 않는다
    params = minute_params(code, day, "153000", market, pw)
    results = []
    for rate in stages:
        client.set_rate(rate)
        ok = egw = err = 0
        t0 = clock()
        while clock() - t0 < seconds:
            try:
                client.minute(params, retry_rate_limit=False)
                ok += 1
            except RateLimitError:
                egw += 1
            except KisAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                err += 1
                log(f"  오류: {exc}")
        el = clock() - t0
        n = ok + egw + err
        r = {"target": rate, "actual": n / el if el else 0.0, "ok": ok, "egw": egw, "err": err}
        results.append(r)
        log(f"[ratetest] 목표 {rate}/초 → 실제 {r['actual']:.2f}/초, 성공 {ok}, EGW00201 {egw}, 기타 오류 {err}")
        if egw:
            log(f"[ratetest] {rate}/초 단계에서 EGW00201 발생 → 중단")
            break
    return results


# ---------------------------------------------------------------- jobs
def cmd_jobs(src_con, con, log: Log, today: Date, top_value: int = 100, top_change: int = 50) -> int:
    start, end = ymd(one_year_ago(today)), ymd(today - timedelta(days=1))
    days = source.open_days(src_con, start, end)
    log(f"[jobs] 기간 {start}~{end}, 거래일 달력상 개장일 {len(days)}일")
    rows, skipped = [], []
    for d in days:
        if not source.has_universe(src_con, d):
            skipped.append(d)
            continue
        for c, why in source.select_codes(src_con, d, top_value, top_change).items():
            rows.append((d, c, why))
    with con:
        con.execute("DELETE FROM jobs")
        con.executemany("INSERT INTO jobs(date, code, reason) VALUES (?,?,?)", rows)
    log(f"[jobs] 작업 {len(rows)}건 생성 (날짜 {len(days) - len(skipped)}일)")
    if skipped:
        log(f"[jobs] 보통주 범위(universe_daily)가 없어 건너뛴 날짜 {len(skipped)}일: "
            f"{skipped[0]}~{skipped[-1]}")
    return len(rows)


# ---------------------------------------------------------------- backfill
def cmd_backfill(client, con, log: Log, now: Callable[[], datetime], market: str = "J", pw: str = "N",
                 max_calls: int = 0, max_failures: int = 20, compute_value: bool = False,
                 every: int = 50, clock: Callable[[], float] = time.monotonic) -> dict:
    todo = store.pending_jobs(con)
    total_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    log(f"[backfill] 전체 {total_jobs}건 중 남은 {len(todo)}건, 초당 {client.limiter.per_sec:g}건, "
        f"호출 상한 {max_calls or '무제한'}")
    t0, c0, egw0 = clock(), client.stats.calls, client.stats.rate_limited
    done = fails = 0
    stop = ""
    for i, (day, code) in enumerate(todo, 1):
        if max_calls and client.stats.calls - c0 >= max_calls:
            stop = f"호출 상한 {max_calls}회 도달"
            break
        try:
            res = fetch_day(client, code, day, market, pw)
            store.save_day(con, res, now().isoformat(timespec="seconds"), compute_value)
            done += 1
            if not res.bars:
                log(f"  {day} {code}: 봉 0개 (거래정지 등 확인 필요)")
        except KisAuthError as exc:
            stop = f"인증 실패: {exc}"
            break
        except Exception as exc:  # noqa: BLE001
            fails += 1
            log(f"  실패 {day} {code}: {exc} (누적 {fails})")
            if fails >= max_failures:
                stop = f"실패 {fails}건 누적"
                break
        if i % every == 0:
            _progress(log, i, len(todo), client.stats.calls - c0, clock() - t0,
                      client.stats.rate_limited - egw0)
    _progress(log, done + fails, len(todo), client.stats.calls - c0, clock() - t0,
              client.stats.rate_limited - egw0)
    log(f"[backfill] 종료: 완료 {done}, 실패 {fails}" + (f", 중단 사유: {stop}" if stop else ""))
    return {"done": done, "fails": fails, "stop": stop, "calls": client.stats.calls - c0}


def _progress(log: Log, i: int, n: int, calls: int, el: float, egw: int) -> None:
    rate = calls / el if el > 0 else 0.0
    eta = (n - i) * (el / i) if i else 0.0
    log(f"  진행 {i}/{n} ({i / n * 100 if n else 100:.1f}%), 호출 {calls}, 실제 {rate:.2f}/초, "
        f"EGW00201 {egw}, 남은 예상 {timedelta(seconds=int(eta))}")
