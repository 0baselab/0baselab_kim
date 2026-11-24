"""매매 설정. 설치 폴더 config/trade_settings.env로 바꿀 수 있다(없으면 기본값).

한도 기본값은 사용자 결정(2026-11-24): 시드 100만, 1회 20만, 동시 5종목, 하루 손실 5만, 누적 손실 50만.
전략 기본값은 분석 세션 PLAN_minute_stage.md(분봉 결과를 보기 전에 고정한 계획)를 따른다.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import dotenv_values


@dataclass
class TradeSettings:
    # --- 한도 (원) ---
    SEED_KRW: int = 1_000_000
    ORDER_KRW: int = 200_000          # 1회 주문 금액 (이 금액 이하로 살 수 있는 최대 수량)
    MAX_POSITIONS: int = 5
    DAILY_LOSS_KRW: int = 50_000      # 하루 실현 손실이 이 이상이면 그날 신규 진입 중단·전량 청산
    TOTAL_LOSS_KRW: int = 500_000     # 누적 실현 손실(live 기록만)이 이 이상이면 live 해제
    MAX_ORDERS_PER_DAY: int = 40
    MAX_ENTRIES_PER_CODE: int = 1     # 종목당 하루 진입 횟수
    # --- 비용 ---
    BUY_COST_RATE: float = 0.00015    # 매수 수수료 (확인 필요: 실제 값은 commissions API로 확정)
    SELL_COST_RATE: float = 0.00215   # 매도 수수료 + 세금 (확인 필요). 왕복 약 0.23%
    SLIPPAGE_TICKS: int = 1           # 시장가성 체결(돌파 진입·손절·시간 청산)에서 불리하게 미끄러지는 호가 수
    # --- 관심종목 규칙 (분석 W7) ---
    WATCH_MAX: int = 20
    WATCH_VR_MIN: float = 5.0         # 거래량비 = 전일 거래량 / 그 전 20일 평균
    WATCH_RET_MIN: float = 0.10       # 전일 등락률
    WATCH_HIGH20_VR_MIN: float = 3.0  # 20일 신고가 + 거래량비
    WATCH_RET_MAX: float = 0.295      # 전일 이 이상(상한가권)은 제외
    WATCH_MIN_VALUE_KRW: int = 1_000_000_000   # 20일 평균 추정 거래대금
    WATCH_MIN_PRICE: int = 1_000
    # --- 전략: 시가 범위 돌파 (분 단위 t = 봉 시작 시각, 09:00 = 540) ---
    OR_START: int = 540               # 시가 범위 09:00
    OR_END: int = 569                 # ~ 09:29 봉까지
    ENTRY_UNTIL: int = 900            # 이 봉(15:00 시작)까지만 진입
    STOP_PCT: float = 0.03            # 손절: 시가 범위 저가와 진입가×(1−3%) 중 높은 가격
    TAKE_PROFIT_PCT: float = 0.03     # 익절(0이면 없음)
    EXIT_T: int = 919                 # 15:19 봉 종가에 남은 보유 전량 청산

    @classmethod
    def load(cls, path: Path | None) -> "TradeSettings":
        """설정 파일 값으로 덮어쓴다. 기본값이 정수면 정수로, 실수면 실수로 읽는다."""
        s = cls()
        if path and path.exists():
            vals = dotenv_values(path)
            for f in fields(cls):
                v = (vals.get(f.name) or "").strip()
                if v:
                    default = getattr(s, f.name)
                    setattr(s, f.name, int(float(v)) if isinstance(default, int) else float(v))
        return s
