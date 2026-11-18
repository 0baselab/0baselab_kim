"""호출 조절기와 재시도 (명세서 11.1, 11.2).

요청 헤더(키·토큰 포함)는 어디에도 기록하지 않는다.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

import httpx

RETRY_WAITS = (1, 2, 4)


class RateLimiter:
    """호출 간격을 1/per_sec초 이상으로 고르게 벌린다.

    한 번에 몰아서 보내지 않는다. 실측(0.1.1)에서 초당 6건으로도 EGW00201이 났는데,
    1초 창 안에서 몰아 보내는 방식이었기 때문에 순간 속도는 더 높았을 수 있다.
    """

    def __init__(self, per_sec: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.clock = clock
        self.sleep = sleep
        self.set_rate(per_sec)
        self._last: float | None = None
        self._stamps: deque[float] = deque()

    def set_rate(self, per_sec: float) -> None:
        self.per_sec = max(0.5, float(per_sec))
        self.interval = 1.0 / self.per_sec

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.interval:
            self.sleep(self.interval - (now - self._last))
            now = self.clock()
        self._last = now
        self._stamps.append(now)
        while self._stamps and now - self._stamps[0] >= 1.0:
            self._stamps.popleft()

    def recent_rate(self) -> int:
        """직전 1초 동안 보낸 호출 수."""
        now = self.clock()
        while self._stamps and now - self._stamps[0] >= 1.0:
            self._stamps.popleft()
        return len(self._stamps)


class ApiClient:
    """출처 하나당 하나. 네트워크 오류·타임아웃만 재시도한다."""

    def __init__(self, name: str, per_sec: float, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 20.0):
        self.name = name
        self.sleep = sleep
        self.limiter = RateLimiter(per_sec, sleep=sleep)
        self.client = client or httpx.Client(timeout=timeout)
        self.calls = 0

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for i in range(len(RETRY_WAITS) + 1):
            self.limiter.wait()
            self.calls += 1
            try:
                return self.client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if i < len(RETRY_WAITS):
                    self.sleep(RETRY_WAITS[i])
        raise ApiError(f"{self.name} 네트워크 오류: {type(last_exc).__name__}")


class ApiError(Exception):
    pass
