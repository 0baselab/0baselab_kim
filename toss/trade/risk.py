"""리스크 관리: 주문 전에 반드시 거치는 검사 (모든 모드 공통)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .settings import TradeSettings


@dataclass
class Account:
    cash: float                       # 현금 (매수에 쓸 수 있는 돈)
    cum_realized: float = 0.0         # 누적 실현 손익 (이 계좌 기록 기준)
    day_realized: float = 0.0
    orders_today: int = 0
    open_positions: int = 0
    halted: str = ""                  # 그날 매매 중단 사유
    disabled: str = ""                # 누적 한도로 꺼짐
    log: list = field(default_factory=list)


class Risk:
    def __init__(self, s: TradeSettings):
        self.s = s

    def new_day(self, acc: Account) -> None:
        acc.day_realized, acc.orders_today, acc.halted = 0.0, 0, ""
        if acc.cum_realized <= -self.s.TOTAL_LOSS_KRW and not acc.disabled:
            acc.disabled = f"누적 손실 {-acc.cum_realized:,.0f}원 ≥ 한도 {self.s.TOTAL_LOSS_KRW:,}원"

    def size(self, acc: Account, price: int) -> tuple[int, str]:
        """살 수량과 거절 사유. 거절이면 (0, 사유)."""
        s = self.s
        if acc.disabled:
            return 0, "disabled:" + acc.disabled
        if acc.halted:
            return 0, "halted:" + acc.halted
        if acc.open_positions >= s.MAX_POSITIONS:
            return 0, "max_positions"
        if acc.orders_today >= s.MAX_ORDERS_PER_DAY:
            return 0, "max_orders"
        qty = int(s.ORDER_KRW // price)
        if qty < 1:
            return 0, "price_above_order_krw"
        need = qty * price * (1 + s.BUY_COST_RATE)
        if need > acc.cash:
            qty = int(acc.cash // (price * (1 + s.BUY_COST_RATE)))
            if qty < 1:
                return 0, "cash"
        return qty, ""

    def after_exit(self, acc: Account, net: float) -> bool:
        """청산 후 손실 한도 확인. 하루 한도에 닿으면 True(전량 청산 필요)."""
        acc.day_realized += net
        acc.cum_realized += net
        if acc.day_realized <= -self.s.DAILY_LOSS_KRW and not acc.halted:
            acc.halted = f"하루 손실 {-acc.day_realized:,.0f}원 ≥ 한도 {self.s.DAILY_LOSS_KRW:,}원"
            return True
        return False
