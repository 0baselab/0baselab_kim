"""캔들 페이지 처리와 시각 정렬.

- 응답 result.candles는 최신순(timestamp 내림차순), 최대 200개. 다음 페이지는 result.nextBefore를 before로 그대로 전달.
- 종목 1분봉 timestamp = 봉 종료 시각: [timestamp - 1분, timestamp) 구간을 집계 (문서 Candle.timestamp).
- 지수(market-indicators) 캔들 timestamp = 봉 시작 시각 (문서 MarketIndicatorCandle.timestamp).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from collector.timeutil import KST


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)


def dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


@dataclass
class PageResult:
    bars: list[dict] = field(default_factory=list)  # keep=True일 때만 채움 (최신 → 과거)
    calls: int = 0
    n_bars: int = 0
    newest: datetime | None = None
    oldest: datetime | None = None
    dates: set = field(default_factory=set)         # 봉이 있는 날짜 (YYYY-MM-DD)
    stop: str = ""


def page_candles(client, path: str, params: dict, max_calls: int,
                 stop_when: Callable[[datetime], bool] | None = None, keep: bool = True,
                 on_page: Callable[[PageResult], None] | None = None) -> PageResult:
    """nextBefore로 과거 방향 페이지 처리.

    멈추는 조건: nextBefore=null(끝), 호출 상한, stop_when(가장 오래된 봉 시각)이 참, nextBefore 반복.
    """
    res = PageResult()
    p = dict(params)
    seen = set()
    while True:
        if res.calls >= max_calls:
            res.stop = f"호출 상한 {max_calls}회"
            break
        body = client.get(path, p)
        res.calls += 1
        r = body.get("result") or {}
        bars = r.get("candles") or []
        for b in bars:
            t = parse_ts(b["timestamp"])
            res.dates.add(t.date().isoformat())
            if res.newest is None or t > res.newest:
                res.newest = t
            if res.oldest is None or t < res.oldest:
                res.oldest = t
        res.n_bars += len(bars)
        if keep:
            res.bars.extend(bars)
        if on_page:
            on_page(res)
        nb = r.get("nextBefore")
        if not bars or not nb:
            res.stop = "더 과거 데이터 없음 (nextBefore=null)"
            break
        if nb in seen:
            res.stop = "nextBefore 반복 (중단)"
            break
        seen.add(nb)
        if stop_when and res.oldest and stop_when(res.oldest):
            res.stop = "기간 상한 도달"
            break
        p["before"] = nb
    return res


def kis_key(toss_ts: datetime, shift_min: int) -> str:
    """토스 봉 시각을 KIS minute_bar.time(HHMMSS) 후보로 바꾼다. shift_min=1이면 종료 시각 → 시작 시각."""
    return (toss_ts - timedelta(minutes=shift_min)).strftime("%H%M%S")


REG_START, REG_END = "090000", "153000"


def compare_bars(toss_bars: list[dict], kis_rows: list[tuple], shift_min: int) -> dict:
    """KRX 정규장(09:00~15:30, KIS 시각 기준) 봉끼리 비교. kis_rows: (time, open, high, low, close, volume)."""
    kis = {r[0]: r for r in kis_rows if REG_START <= r[0] <= REG_END}
    toss = {}
    for b in toss_bars:
        k = kis_key(parse_ts(b["timestamp"]), shift_min)
        if REG_START <= k <= REG_END:
            toss[k] = b
    common = sorted(set(kis) & set(toss))
    eq = {"open": 0, "high": 0, "low": 0, "close": 0}
    tv = kv = Decimal(0)
    for k in common:
        t, r = toss[k], kis[k]
        for i, f in ((1, "open"), (2, "high"), (3, "low"), (4, "close")):
            if dec(t.get(f + "Price")) is not None and r[i] is not None and dec(t.get(f + "Price")) == Decimal(r[i]):
                eq[f] += 1
        tv += dec(t.get("volume")) or 0
        kv += Decimal(r[5] or 0)
    n = len(common)
    rate = {f: (eq[f] / n if n else None) for f in eq}
    return {"shift_min": shift_min, "kis_bars": len(kis), "toss_bars": len(toss), "matched": n, "match_rate": rate,
            "toss_volume": str(tv), "kis_volume": str(kv),
            "volume_diff_pct": (float((tv - kv) / kv * 100) if kv else None)}


def best_alignment(toss_bars: list[dict], kis_rows: list[tuple]) -> tuple[dict, dict]:
    """shift 0분(종료 시각 그대로)과 1분(시작 시각으로 변환) 중 종가 일치율이 높은 쪽."""
    a, b = compare_bars(toss_bars, kis_rows, 0), compare_bars(toss_bars, kis_rows, 1)
    score = lambda x: (x["match_rate"]["close"] or 0, x["matched"])
    return (a, b) if score(a) >= score(b) else (b, a)


def session_counts(toss_bars: list[dict]) -> dict:
    """봉 시작 시각 기준 세션별 개수 (NXT 판단용). 종목 1분봉 timestamp는 종료 시각이므로 1분 뺀다."""
    pre = reg = post = other = 0
    for b in toss_bars:
        s = (parse_ts(b["timestamp"]) - timedelta(minutes=1)).strftime("%H%M")
        if "0800" <= s < "0900":
            pre += 1
        elif "0900" <= s <= "1530":
            reg += 1
        elif "1530" < s < "2000":
            post += 1
        else:
            other += 1
    return {"pre_0800_0900": pre, "regular_0900_1530": reg, "after_1531_2000": post, "other": other}


# ---------------------------------------------------------------- 자체 점검 (KIS 없이 토스 1분봉 ↔ 토스 일봉)
def _expected_regular() -> set[str]:
    """KRX 정규장에서 봉이 있어야 하는 분 (봉 시작 HHMM): 09:00~15:19 연속매매 + 15:30 종가 단일가.
    15:20~15:29는 종가 단일가 접수 시간이라 체결이 없을 수 있어 기대값에서 뺀다."""
    out = set()
    for m in range(9 * 60, 15 * 60 + 20):
        out.add(f"{m // 60:02d}{m % 60:02d}")
    out.add("1530")
    return out


EXPECTED_REG = _expected_regular()


def check_day(m1: list[dict], d1: dict | None) -> dict:
    """그날 토스 1분봉(m1, timestamp=종료 시각)을 같은 날 토스 일봉(d1)과 대조한다.

    - 정규장 분봉 누락: EXPECTED_REG 중 봉이 없는 분
    - 거래량 0인 봉 수 (세션별): 거래가 없는 시간을 채운 봉인지 판단용
    - 일봉 시가·고가·저가·종가가 정규장 분봉에서 나온 값과 같은지, 전체 세션 분봉 값과 같은지
    - 거래량 합계: 정규장 분봉 합 / 전체 분봉 합 / 일봉 거래량
    """
    bars = sorted(((parse_ts(b["timestamp"]) - timedelta(minutes=1)).strftime("%H%M"), b) for b in m1)
    present = {s for s, _ in bars}
    reg = [(s, b) for s, b in bars if "0900" <= s <= "1530"]
    zero = {"pre": 0, "regular": 0, "after": 0}
    for s, b in bars:
        if (dec(b.get("volume")) or 0) == 0:
            zero["pre" if s < "0900" else ("regular" if s <= "1530" else "after")] += 1
    vol = lambda xs: sum((dec(b.get("volume")) or 0) for _, b in xs)

    def ohlc(xs):
        if not xs:
            return None
        return {"open": dec(xs[0][1].get("openPrice")), "high": max(dec(b.get("highPrice")) for _, b in xs),
                "low": min(dec(b.get("lowPrice")) for _, b in xs), "close": dec(xs[-1][1].get("closePrice"))}
    traded = lambda xs: [(s, b) for s, b in xs if (dec(b.get("volume")) or 0) > 0]
    r_ohlc, a_ohlc = ohlc(traded(reg)), ohlc(traded(bars))
    d = {k: dec(d1.get(k + "Price")) for k in ("open", "high", "low", "close")} if d1 else None
    eq = lambda x: ({k: (x[k] == d[k]) for k in d} if (x and d) else None)
    missing = sorted(EXPECTED_REG - present)
    dv = dec(d1.get("volume")) if d1 else None
    pct = lambda v: (float((v - dv) / dv * 100) if dv else None)
    return {
        "bars": len(bars), "first_start": bars[0][0] if bars else None, "last_start": bars[-1][0] if bars else None,
        "regular_expected": len(EXPECTED_REG), "regular_missing": len(missing), "missing_examples": missing[:10],
        "auction_1520_1529_bars": sum(1 for s in present if "1520" <= s <= "1529"),
        "zero_volume_bars": zero,
        "daily": {k: (str(v) if v is not None else None) for k, v in d.items()} if d else None,
        "match_regular": eq(r_ohlc), "match_all_sessions": eq(a_ohlc),
        "volume": {"regular": str(vol(reg)), "all": str(vol(bars)), "daily": str(dv) if dv is not None else None,
                   "regular_vs_daily_pct": pct(vol(reg)), "all_vs_daily_pct": pct(vol(bars))},
    }
