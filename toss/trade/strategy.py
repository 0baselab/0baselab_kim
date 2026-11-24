"""전략: 시가 범위 돌파 (분석 PLAN_minute_stage.md에서 결과를 보기 전에 고정한 규칙).

- 시가 범위(OR): 09:00~09:29 봉(거래량 있는 봉)의 최고가·최저가
- 진입: 09:30 이후 ENTRY_UNTIL 봉까지, 고가가 OR 고가를 넘으면 'OR 고가 + 1호가'에 매수
  (봉 시가가 이미 그 위면 시가). 그 가격이 상한가 이상이면 체결 없음.
- 손절: OR 저가와 진입가×(1−STOP_PCT) 중 높은 가격. 봉 시가가 이미 그 아래면 시가.
- 익절: 진입가×(1+TAKE_PROFIT_PCT)를 고가가 넘으면 그 가격(지정가). 0이면 없음.
- 같은 봉에서 손절·익절이 함께 닿으면 손절로 본다. 진입한 봉에서는 익절을 인정하지 않는다(봉 안의 순서를 모름).
- EXIT_T 봉(15:19) 종가에 남은 보유 청산.
- 진입·손절·시간 청산은 시장가성 체결이라 SLIPPAGE_TICKS만큼 불리하게 체결한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ticks
from .settings import TradeSettings


@dataclass
class Bar:
    t: int
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Signal:
    kind: str          # buy / sell
    price: int
    reason: str        # entry / stop / take_profit / time_exit / loss_limit / eod
    t: int


@dataclass
class CodeState:
    code: str
    prev_close: float | None = None   # 전일 정규장 종가 (상한가 계산용). 없으면 상한가 검사 생략
    or_high: float | None = None
    or_low: float | None = None
    entries: int = 0
    in_pos: bool = False
    entry_px: int = 0
    stop_px: int = 0
    tp_px: int | None = None
    entry_t: int = -1
    last_close: float | None = None
    notes: list = field(default_factory=list)


class OpeningRangeBreakout:
    def __init__(self, s: TradeSettings):
        self.s = s

    def on_bar(self, st: CodeState, b: Bar, can_enter: bool) -> Signal | None:
        s = self.s
        st.last_close = b.close
        if s.OR_START <= b.t <= s.OR_END:
            if b.volume > 0:
                st.or_high = b.high if st.or_high is None else max(st.or_high, b.high)
                st.or_low = b.low if st.or_low is None else min(st.or_low, b.low)
            return None
        if st.in_pos:
            return self._exit(st, b)
        if (can_enter and st.or_high is not None and b.t > s.OR_END and b.t <= s.ENTRY_UNTIL
                and st.entries < s.MAX_ENTRIES_PER_CODE and b.high > st.or_high and b.volume > 0):
            trigger = ticks.up(st.or_high, 1)
            px = ticks.up(max(trigger, b.open), s.SLIPPAGE_TICKS)
            if st.prev_close and px >= ticks.upper_limit(st.prev_close):
                st.notes.append(f"{b.t}:상한가라 매수 불가")
                return None
            if px > b.high:  # 봉 안에서 그 가격까지 가지 않았으면 체결 없음
                return None
            return Signal("buy", px, "entry", b.t)
        return None

    def on_filled_buy(self, st: CodeState, px: int, t: int) -> None:
        s = self.s
        st.in_pos, st.entry_px, st.entry_t = True, px, t
        st.entries += 1
        st.stop_px = max(ticks.ceil_tick(st.or_low), ticks.ceil_tick(px * (1 - s.STOP_PCT)))
        st.tp_px = ticks.ceil_tick(px * (1 + s.TAKE_PROFIT_PCT)) if s.TAKE_PROFIT_PCT > 0 else None

    def check_same_bar_stop(self, st: CodeState, b: Bar) -> Signal | None:
        """진입한 봉에서 손절가까지 내려갔으면 손절 (보수적으로)."""
        if b.low <= st.stop_px:
            return Signal("sell", ticks.down(st.stop_px, self.s.SLIPPAGE_TICKS), "stop", b.t)
        return None

    def _exit(self, st: CodeState, b: Bar) -> Signal | None:
        s = self.s
        if b.low <= st.stop_px:
            px = min(st.stop_px, int(b.open))  # 시가부터 손절가 아래면(갭 하락) 시가에 체결
            return Signal("sell", ticks.down(px, s.SLIPPAGE_TICKS), "stop", b.t)
        if st.tp_px is not None and b.high > st.tp_px:
            return Signal("sell", max(st.tp_px, int(b.open)), "take_profit", b.t)
        if b.t >= s.EXIT_T:
            return Signal("sell", ticks.down(b.close, s.SLIPPAGE_TICKS), "time_exit", b.t)
        return None

    def force_exit(self, st: CodeState, t: int, reason: str) -> Signal | None:
        if not st.in_pos or st.last_close is None:
            return None
        return Signal("sell", ticks.down(st.last_close, self.s.SLIPPAGE_TICKS), reason, t)
