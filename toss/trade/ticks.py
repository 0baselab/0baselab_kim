"""KRX 호가 단위와 상·하한가.

호가 단위(2023-01-25 개편 이후, 코스피·코스닥 공통): 2천 미만 1원, 5천 미만 5원, 2만 미만 10원, 5만 미만 50원,
20만 미만 100원, 50만 미만 500원, 그 이상 1,000원. (확인 필요: 제도 변경 시 갱신)
가격제한폭 ±30%, 기준가는 전일 정규장 종가.
"""
from __future__ import annotations

import math

_STEPS = [(2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)]


def tick(price: float) -> int:
    for bound, t in _STEPS:
        if price < bound:
            return t
    return 1_000


def floor_tick(price: float) -> int:
    t = tick(price)
    return int(math.floor(price / t) * t)


def ceil_tick(price: float) -> int:
    t = tick(price)
    return int(math.ceil(price / t) * t)


def up(price: float, n: int = 1) -> int:
    p = int(price)
    for _ in range(n):
        p += tick(p)
    return p


def down(price: float, n: int = 1) -> int:
    p = int(price)
    for _ in range(n):
        p -= tick(p - 1)
    return p


def upper_limit(base: float) -> int:
    return floor_tick(base * 1.3)


def lower_limit(base: float) -> int:
    return ceil_tick(base * 0.7)
