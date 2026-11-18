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
    """최근 1초 안의 호출 수를 per_sec 이하로 유지한다."""

    def __init__(self, per_sec: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_sec = max(1, int(per_sec))
        self.clock = clock
        self.sleep = sleep
        self._stamps: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= 1.0:
            self._stamps.popleft()

    def wait(self) -> None:
        now = self.clock()
        self._trim(now)
        if len(self._stamps) >= self.per_sec:
            self.sleep(1.0 - (now - self._stamps[0]))
            now = self.clock()
            self._trim(now)
        self._stamps.append(now)

    def recent_rate(self) -> int:
        """직전 1초 동안 보낸 호출 수."""
        self._trim(self.clock())
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
