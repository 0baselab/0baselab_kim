"""백테스트 설정: 설치 폴더 config/backtest.env. 없으면 기본값으로 만든다. 사용자가 바꾼 값은 유지."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULTS: list[tuple[str, str, str]] = [
    ("START", "20160101", "백테스트 시작일"),
    ("INITIAL_CASH_KRW", "10000000", "초기 자금(원)"),
    ("MIN_MKTCAP_KRW", "200000000000", "종목 거르기: 시가총액 하한(원)"),
    ("MIN_VALUE20_KRW", "5000000000", "종목 거르기: 20일 평균 거래대금 하한(원)"),
    ("POS250_MIN_PCT", "80", "진입: 250일 범위 안 위치 하한(%)"),
    ("FLOW_TOP_PCT", "20", "B: 20일 외국인+기관 순매수/시총 상위 비율(%)"),
    ("FLOW_DAYS", "20", "B: 순매수 합산 일수"),
    ("VALUE_SPIKE_X", "1.5", "B: 신호 당일 거래대금 / 직전 20일 평균 하한(배)"),
    ("DEBT_RATIO_MAX_PCT", "200", "C: 부채비율 상한(%)"),
    ("INDEX_MA_DAYS", "120", "D: 지수 이동평균 일수"),
    ("LIMIT_WAIT_DAYS", "5", "지정가 최대 대기 거래일"),
    ("ATR_DAYS", "14", "ATR 일수"),
    ("ATR_MULT", "0.5", "③ 지정가 = 전일 종가 - ATR_MULT x ATR"),
    ("STOP_PCT", "15", "청산: 매수 후 최고 종가 대비 하락률(%)"),
    ("MAX_POSITIONS", "10", "최대 보유 종목 수"),
    ("MAX_PER_THEME", "3", "같은 테마 최대 종목 수"),
    ("BUY_FEE_PCT", "0.015", "매수 수수료(%)"),
    ("SELL_FEE_PCT", "0.015", "매도 수수료(%) (거래세 별도)"),
    ("SLIPPAGE_PCT", "0.1", "슬리피지 편도(%)"),
    ("DEV_END", "20221231", "개발 구간 끝 (이후는 검증 구간)"),
    ("RANK_BY", "value_surge", "A의 신호가 넘칠 때 고르는 순서: value_surge(거래대금 급증 순) | trend_quality(60일 수익률 / 변동성)"),
    ("RANK_TIEBREAK", "value_surge", "B·C·D에서 수급 점수가 같을 때 보조 순서: value_surge | trend_quality"),
    ("OVERHEAT_FILTER", "off", "과열 제외: on이면 최근 20일 수익률 또는 20일선 이격이 기준 이상인 신호 제외"),
    ("OVERHEAT_RET20_PCT", "40", "과열 기준: 최근 20일 수익률(%)"),
    ("OVERHEAT_GAP20_PCT", "25", "과열 기준: 종가가 20일선보다 이만큼(%) 이상 높음"),
    ("INDEX_FILTER_FOR_A", "off", "on이면 A에도 지수 필터(해당 시장 지수 < 120일선이면 신규 매수 중단) 적용"),
    ("SENS_STOP_PCTS", "10,15,20", "민감도: 손절 %"),
    ("SENS_FLOW_TOP_PCTS", "10,20,30", "민감도: 수급 상위 %"),
]

# 증권거래세(코스피는 거래세 + 농어촌특별세 0.15% 합계, 코스닥은 거래세). 시행일 -> 합계 세율(%).
# 출처: 서울경제 2024-12 "내년부터 주식 1억 매도하면 세금 5만 원 더 낸다", 에너지경제 2025-04-30,
#       DS투자증권 2025 증권거래세율 인하 안내 (2021 0.23, 2023 0.20, 2024 0.18, 2025 0.15, 2026 0.20).
#       2019-06-03 이전 0.30 / 이후 0.25는 '2019~2020년 0.05%p 인하' 설명으로만 간접 확인 (확인 필요).
TAX_TABLE = [("20160101", 0.30), ("20190603", 0.25), ("20210101", 0.23), ("20230101", 0.20),
             ("20240101", 0.18), ("20250101", 0.15), ("20260101", 0.20)]


def tax_pct(day: str) -> float:
    rate = TAX_TABLE[0][1]
    for start, r in TAX_TABLE:
        if day >= start:
            rate = r
    return rate


@dataclass
class Config:
    values: dict = field(default_factory=dict)

    def f(self, k) -> float:
        return float(self.values[k])

    def i(self, k) -> int:
        return int(float(self.values[k]))

    def s(self, k) -> str:
        return str(self.values[k])

    def ints(self, k) -> list[int]:
        return [int(x) for x in str(self.values[k]).split(",") if x.strip()]

    def copy(self, **over) -> "Config":
        v = dict(self.values)
        v.update({k: str(x) for k, x in over.items()})
        return Config(v)


def config_path(install: Path) -> Path:
    return install / "config" / "backtest.env"


def load(install: Path) -> Config:
    p = config_path(install)
    p.parent.mkdir(parents=True, exist_ok=True)
    have = dotenv_values(p) if p.exists() else {}
    missing = [d for d in DEFAULTS if d[0] not in have]
    if missing:
        with p.open("a", encoding="utf-8", newline="\n") as f:
            for k, v, desc in missing:
                f.write(f"# {desc}\n{k}={v}\n")
        have = dotenv_values(p)
    vals = {k: v for k, v, _ in DEFAULTS}
    vals.update({k: v for k, v in have.items() if v not in (None, "")})
    return Config(vals)
