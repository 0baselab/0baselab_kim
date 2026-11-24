"""토스증권 Open API 조회 전용 클라이언트.

절대 규칙:
- ALLOWLIST에 있는 (메서드, 경로)만 호출한다. 그 밖의 경로(주문·조건주문·주문 조회 등)는
  요청을 보내기 전에 NotAllowedError로 실패한다. POST는 토큰 발급(/oauth2/token) 하나뿐이다.
- 토큰은 클라이언트당 1개만 유효하다(재발급 시 이전 토큰 즉시 무효). 파일에 캐시하고 만료 10분 전까지 재사용한다.
  401 token-revoked / expired-token / invalid-token이면 1회만 재발급 후 재시도한다.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx

from collector.http import RateLimiter
from collector.timeutil import KST, now_kst

TOKEN_PATH = "/oauth2/token"
_SYM = r"[A-Za-z0-9.\-]+"
_SECTOR = r"[A-Za-z0-9_\-]+"

# (메서드, 경로 정규식, Rate Limits Group). 그룹은 openapi.json 각 API의 description 표기 기준.
ALLOWLIST: list[tuple[str, str, str]] = [
    ("POST", r"/oauth2/token", "AUTH"),
    ("GET", r"/api/v1/candles", "MARKET_DATA_CHART"),
    ("GET", r"/api/v1/prices", "MARKET_DATA"),
    ("GET", r"/api/v1/orderbook", "MARKET_DATA"),
    ("GET", r"/api/v1/trades", "MARKET_DATA"),
    ("GET", r"/api/v1/price-limits", "MARKET_DATA"),
    ("GET", r"/api/v1/stocks", "STOCK"),
    ("GET", r"/api/v1/stocks/all", "STOCK_ALL"),
    ("GET", rf"/api/v1/stocks/{_SYM}/warnings", "STOCK"),
    ("GET", rf"/api/v1/stocks/{_SYM}/investor-trading", "STOCK_TRADING_TREND"),
    ("GET", rf"/api/v1/stocks/{_SYM}/program-trades", "STOCK_TRADING_TREND"),
    ("GET", r"/api/v1/rankings", "RANKING"),
    ("GET", r"/api/v1/rankings/investor-trading", "RANKING"),
    ("GET", r"/api/v1/market-indicators/prices", "MARKET_INDICATOR"),
    ("GET", rf"/api/v1/market-indicators/{_SYM}/candles", "MARKET_INDICATOR_CHART"),
    ("GET", rf"/api/v1/market-indicators/{_SYM}/investor-trading", "MARKET_INDICATOR"),
    ("GET", r"/api/v1/market-calendar/KR", "MARKET_INFO"),
    ("GET", r"/api/v1/sectors", "SECTOR"),
    ("GET", r"/api/v1/sectors/rankings", "SECTOR_RANKING"),
    ("GET", rf"/api/v1/sectors/(?!rankings$){_SECTOR}", "SECTOR"),
    ("GET", rf"/api/v1/sectors/{_SECTOR}/stocks", "SECTOR"),
    ("GET", r"/api/v1/accounts", "ACCOUNT"),
    ("GET", r"/api/v1/holdings", "ASSET"),
    ("GET", r"/api/v1/buying-power", "ORDER_INFO"),
    ("GET", r"/api/v1/commissions", "ORDER_INFO"),
]
_COMPILED = [(m, re.compile(rf"^{p}$"), g) for m, p, g in ALLOWLIST]

# 문서의 그룹별 초당 한도 (overview.md Rate Limits 표). 실제 호출은 RATE_FACTOR(기본 0.5)를 곱한 속도.
DOC_LIMITS: dict[str, float] = {
    "AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "STOCK": 5, "STOCK_ALL": 1, "STOCK_TRADING_TREND": 10,
    "MARKET_INFO": 3, "MARKET_DATA": 15, "MARKET_DATA_CHART": 20, "RANKING": 5, "MARKET_INDICATOR": 10,
    "MARKET_INDICATOR_CHART": 5, "SECTOR": 5, "SECTOR_RANKING": 5, "ORDER_INFO": 6,
}
TOKEN_ERRORS = {"token-revoked", "expired-token", "invalid-token"}
TOKEN_MARGIN = timedelta(minutes=10)
NET_RETRY_WAITS = (1, 2, 4)


class NotAllowedError(Exception):
    """허용 목록 밖의 호출. 요청을 보내지 않는다."""


class TossApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code} {message}".strip())
        self.status, self.code, self.message = status, code, message


class TossAuthError(TossApiError):
    pass


class IpNotAllowedError(TossApiError):
    pass


# 로그에서 가릴 값 (키·토큰·계좌번호). 메모리에만 둔다.
SENSITIVE: set[str] = set()


def sensitive(v) -> None:
    if v is not None and len(str(v)) >= 6:
        SENSITIVE.add(str(v))


def redact(text: str) -> str:
    for v in sorted(SENSITIVE, key=len, reverse=True):
        text = text.replace(v, "****")
    return text


def group_of(method: str, path: str) -> str:
    """허용된 호출이면 Rate Limits Group, 아니면 NotAllowedError."""
    m = method.upper()
    if "?" in path or "#" in path or ".." in path or "//" in path or "%" in path:
        raise NotAllowedError(f"허용되지 않은 경로 형식: {m} {path}")
    for mm, rx, g in _COMPILED:
        if mm == m and rx.match(path):
            return g
    raise NotAllowedError(f"허용 목록에 없는 호출입니다 (조회 전용): {m} {path}")


def _error_of(resp: httpx.Response) -> tuple[str, str]:
    try:
        body = resp.json()
    except ValueError:
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):  # 공통 envelope {"error": {"code","message"}}
        return err.get("code") or "", err.get("message") or ""
    if isinstance(err, str):   # 토큰 발급은 OAuth2 형식 {"error": "...", "error_description": "..."}
        return err, body.get("error_description") or ""
    return "", ""


@dataclass
class RateRecord:
    group: str
    path: str
    status: int
    limit: str | None
    remaining: str | None
    reset: str | None


@dataclass
class TossClient:
    base_url: str
    client_id: str
    client_secret: str
    token_path: Path
    rate_factor: float = 0.5
    group_rates: dict = field(default_factory=dict)   # 그룹별 초당 호출 수 직접 지정 (예: {"MARKET_DATA_CHART": 5})
    retry_429: int = 5
    http: httpx.Client | None = None
    now: Callable[[], datetime] = now_kst
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    on_rate: Callable[[RateRecord], None] | None = None
    calls: int = 0
    n429: int = 0
    token_issues: int = 0
    _token: str | None = None
    _limiters: dict = field(default_factory=dict)

    def __post_init__(self):
        sensitive(self.client_id)
        sensitive(self.client_secret)
        self.http = self.http or httpx.Client(timeout=20.0)

    def limiter(self, group: str) -> RateLimiter:
        if group not in self._limiters:
            rate = self.group_rates.get(group) or DOC_LIMITS.get(group, 1) * self.rate_factor
            self._limiters[group] = RateLimiter(rate, clock=self.clock, sleep=self.sleep)
        return self._limiters[group]

    def set_group_rate(self, group: str, per_sec: float) -> None:
        self.group_rates[group] = per_sec
        if group in self._limiters:
            self._limiters[group].set_rate(per_sec)

    def _send(self, method: str, path: str, **kw) -> tuple[str, httpx.Response]:
        group = group_of(method, path)  # 허용 목록 검사가 항상 먼저
        last = None
        for i in range(len(NET_RETRY_WAITS) + 1):
            self.limiter(group).wait()
            self.calls += 1
            try:
                resp = self.http.request(method.upper(), self.base_url + path, **kw)
                break
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if i < len(NET_RETRY_WAITS):
                    self.sleep(NET_RETRY_WAITS[i])
        else:
            raise TossApiError(0, "network", type(last).__name__)
        if self.on_rate:
            h = resp.headers
            self.on_rate(RateRecord(group, path, resp.status_code, h.get("X-RateLimit-Limit"),
                                    h.get("X-RateLimit-Remaining"), h.get("X-RateLimit-Reset")))
        return group, resp

    # --- 토큰 ---
    def _read_cache(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        c = self._read_cache()
        if not force and c.get("access_token") and c.get("expires_at"):
            if self.now() < datetime.fromisoformat(c["expires_at"]) - TOKEN_MARGIN:
                self._token = c["access_token"]
                sensitive(self._token)
                return self._token
        return self._issue()

    def _issue(self) -> str:
        _, resp = self._send("POST", TOKEN_PATH, data={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.token_issues += 1
        if resp.status_code == 429:
            raise TossAuthError(429, "rate-limit-exceeded", "토큰 발급 한도 초과")
        try:
            body = resp.json()
        except ValueError:
            body = {}
        tok = body.get("access_token") if isinstance(body, dict) else None
        if resp.status_code != 200 or not tok:
            code, msg = _error_of(resp)
            if resp.status_code == 403:
                raise IpNotAllowedError(403, code or "access_denied", msg)
            raise TossAuthError(resp.status_code, code, msg or "토큰 발급 실패")
        sensitive(tok)
        issued = self.now()
        expires = issued + timedelta(seconds=int(body.get("expires_in") or 0))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({"access_token": tok, "expires_at": expires.isoformat(),
                                               "issued_at": issued.isoformat()}), encoding="utf-8")
        self._token = tok
        return tok

    # --- 조회 ---
    def get(self, path: str, params: dict | None = None, account_seq: int | None = None) -> dict:
        group_of("GET", path)  # 토큰을 쓰기 전에도 허용 목록부터 검사
        reissued = False
        n429 = 0
        while True:
            headers = {"Authorization": f"Bearer {self.token()}"}
            if account_seq is not None:
                headers["X-Tossinvest-Account"] = str(account_seq)
            _, resp = self._send("GET", path, params=params or {}, headers=headers)
            if resp.status_code == 429:
                self.n429 += 1
                if n429 >= self.retry_429:
                    raise TossApiError(429, "rate-limit-exceeded", f"{n429}회 재시도 후에도 한도 초과")
                try:
                    wait = float(resp.headers.get("Retry-After") or 1)
                except ValueError:
                    wait = 1.0
                self.sleep(max(wait, 0.0))
                n429 += 1
                continue
            if resp.status_code == 200:
                return resp.json()
            code, msg = _error_of(resp)
            if resp.status_code == 401 and code in TOKEN_ERRORS and not reissued:
                reissued = True
                self._token = None
                self.token(force=True)
                continue
            if resp.status_code == 401:
                raise TossAuthError(401, code, msg)
            if resp.status_code == 403 and code == "ip-not-allowed":
                raise IpNotAllowedError(403, code, msg)
            raise TossApiError(resp.status_code, code, msg)
