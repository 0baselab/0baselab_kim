"""관심종목 생성: 분석 세션의 W7 규칙.

t일 관심종목은 t-1일 장 마감(통합 세션, 20:00)까지의 일봉만으로 고른다 (미래 정보 없음).
- 거래량비 VR = 거래량(t-1) / 평균 거래량(t-21..t-2)
- 전일 등락률 R1 = 종가(t-1) / 종가(t-2) - 1
- 20일 신고가 = 종가(t-1) >= 최고 고가(t-21..t-2)
- 조건: VR >= 5 또는 R1 >= 10% 또는 (20일 신고가 그리고 VR >= 3). R1 >= 29.5%(상한가권)는 제외.
- 표본 조건: t-1까지 21봉 이상, 20일 평균 추정 거래대금(t-20..t-1) >= 10억, 종가(t-1) >= 1,000원,
  t-21..t-1에 거래정지(거래량 0)·분할/병합/권리락(수정계수 변화 > 1%, 없으면 ±30.5% 밖 가격 변화) 없음.
- VR 큰 순서로 WATCH_MAX(20)개.

분석 세션과의 차이: 분석은 이벤트 제외 창을 [t-21, t]로 잡았지만, t일 이벤트는 장 시작 전에 알 수 없으므로
여기서는 [t-21, t-1]만 본다(실전과 같은 조건). replay 결과를 분석 숫자와 비교할 때 이 차이를 감안한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .settings import TradeSettings

WINDOW = 21  # t-21 .. t-1


@dataclass
class WatchItem:
    code: str
    rank: int
    vr: float
    r1: float
    new_high20: bool
    prev_close: float
    value20: float


def _prev_days(dcon, day: str, n: int) -> list[str]:
    return [r[0] for r in dcon.execute(
        "SELECT date FROM trading_day WHERE date < ? ORDER BY date DESC LIMIT ?", (day, n))][::-1]


def build_watchlist(dcon, day: str, s: TradeSettings) -> tuple[list[WatchItem], dict]:
    """day(YYYY-MM-DD)의 관심종목과 집계(제외 사유별 수)."""
    days = _prev_days(dcon, day, WINDOW)
    stats = {"days": len(days), "codes": 0, "excluded": {}}
    if len(days) < WINDOW:
        stats["excluded"]["history_days"] = WINDOW - len(days)
        return [], stats
    q = ",".join("?" * len(days))
    rows: dict[str, dict] = {}
    for code, d, o, h, l, c, v, val in dcon.execute(
            f"SELECT code, date, open, high, low, close, volume, value_est_krw FROM daily_bar WHERE date IN ({q})", days):
        rows.setdefault(code, {})[d] = (o, h, l, c, v, val)
    adj: dict[str, dict] = {}
    try:
        for code, d, f in dcon.execute(f"SELECT code, date, adj_factor FROM adj_daily WHERE date IN ({q})", days):
            adj.setdefault(code, {})[d] = f
    except Exception:  # noqa: BLE001  adj_daily가 없는 옛 DB
        pass
    ex = stats["excluded"]
    bump = lambda k: ex.__setitem__(k, ex.get(k, 0) + 1)
    picked = []
    for code, by in rows.items():
        stats["codes"] += 1
        if any(d not in by for d in days):
            bump("history")
            continue
        bars = [by[d] for d in days]                  # t-21 .. t-1
        o1, h1, l1, c1, v1, _ = bars[-1]
        c2 = bars[-2][3]
        if any(b[4] == 0 for b in bars):
            bump("halt")
            continue
        fs = [adj.get(code, {}).get(d) for d in days]
        if all(f is not None for f in fs):
            if any(abs(b / a - 1) > 0.01 for a, b in zip(fs, fs[1:]) if a):
                bump("corporate_action")
                continue
        elif any(not (0.695 <= x[0] / p[3] <= 1.305 and 0.695 <= x[3] / p[3] <= 1.305)
                 for p, x in zip(bars, bars[1:]) if p[3]):
            bump("price_jump")
            continue
        vals = [b[5] for b in bars[1:]]               # t-20 .. t-1
        value20 = sum(v or 0 for v in vals) / len(vals)
        if value20 < s.WATCH_MIN_VALUE_KRW:
            bump("liquidity")
            continue
        if c1 < s.WATCH_MIN_PRICE:
            bump("penny")
            continue
        vavg = sum(b[4] for b in bars[:-1]) / (len(bars) - 1)   # t-21 .. t-2
        vr = v1 / vavg if vavg else 0.0
        r1 = c1 / c2 - 1 if c2 else 0.0
        hmax = max(b[1] for b in bars[:-1])
        new_high = c1 >= hmax
        if r1 >= s.WATCH_RET_MAX:
            bump("limit_up")
            continue
        if vr >= s.WATCH_VR_MIN or r1 >= s.WATCH_RET_MIN or (new_high and vr >= s.WATCH_HIGH20_VR_MIN):
            picked.append(WatchItem(code, 0, vr, r1, new_high, c1, value20))
        else:
            bump("no_signal")
    picked.sort(key=lambda w: (-w.vr, w.code))
    out = picked[: s.WATCH_MAX]
    for i, w in enumerate(out, 1):
        w.rank = i
    stats["candidates"] = len(picked)
    return out, stats
