"""KIS 분봉 조회 클라이언트 (시세 조회 전용).

허용 TR ID는 주식일별분봉조회(FHKST03010230) 하나뿐이다. 주문·계좌 API는 호출할 수 없다.
필드·URL·TR ID는 docs/kis_api_reference.xlsx '주식일별분봉조회' 시트 기준.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx

from collector.http import RateLimiter
from collector.timeutil import KST, now_kst

TOKEN_PATH = "/oauth2/tokenP"
MINUTE = ("FHKST03010230", "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice")
ALLOWED_TR_IDS = {MINUTE[0]}

RATE_LIMIT_CODE = "EGW00201"
# 토큰 만료·무효 코드. 공식 문서 엑셀에 없어 '확인 필요'. HTTP 401/403도 토큰 오류로 본다.
TOKEN_ERROR_CODES = {"EGW00121", "EGW00123"}
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)
NET_RETRY_WAITS = (1, 2, 4)


class KisAuthError(Exception):
    pass


class RateLimitError(Exception):
    pass


class KisApiError(Exception):
    pass


# 로그에서 가릴 값 (키·토큰). 메모리에만 둔다.
SENSITIVE: set[str] = set()


def redact(text: str) -> str:
    for v in sorted(SENSITIVE, key=len, reverse=True):
        text = text.replace(v, "****")
    return text


def _sensitive(v: str | None) -> None:
    if v and len(v) >= 8:
        SENSITIVE.add(v)


@dataclass
class Stats:
    calls: int = 0          # 분봉 API 호출 수 (재시도 포함, 토큰 발급 제외)
    ok: int = 0
    rate_limited: int = 0   # EGW00201 응답 수
    token_issues: int = 0


@dataclass
class KisMinuteClient:
    base_url: str
    app_key: str
    app_secret: str
    token_path: Path
    rate: float = 10.0
    backoff_retries: int = 5
    backoff_base: float = 1.0
    min_issue_gap: float = 61.0
    http: httpx.Client | None = None
    now: Callable[[], datetime] = now_kst
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    stats: Stats = field(default_factory=Stats)
    _token: str | None = None

    def __post_init__(self):
        _sensitive(self.app_key)
        _sensitive(self.app_secret)
        self.http = self.http or httpx.Client(timeout=20.0)
        self.limiter = RateLimiter(self.rate, clock=self.clock, sleep=self.sleep)

    def set_rate(self, per_sec: float) -> None:
        self.limiter.set_rate(per_sec)

    # --- 토큰: 전용 파일에 캐시. 만료 10분 전까지는 재발급하지 않는다 ---
    def _read_cache(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def token(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        cache = self._read_cache()
        if not force and cache.get("access_token") and cache.get("expires_at"):
            exp = datetime.fromisoformat(cache["expires_at"])
            if self.now() < exp - TOKEN_REFRESH_MARGIN:
                self._token = cache["access_token"]
                _sensitive(self._token)
                return self._token
        # 발급 1분 1회 제한: 직전 발급 후 min_issue_gap초가 안 지났으면 기다린다
        if cache.get("issued_at"):
            gap = (self.now() - datetime.fromisoformat(cache["issued_at"])).total_seconds()
            if gap < self.min_issue_gap:
                self.sleep(self.min_issue_gap - gap)
        return self._issue()

    def _issue(self) -> str:
        resp = self._send("POST", self.base_url + TOKEN_PATH, json={
            "grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret})
        self.stats.token_issues += 1
        try:
            body = resp.json()
        except ValueError:
            body = {}
        tok = body.get("access_token")
        if resp.status_code != 200 or not tok:
            raise KisAuthError(f"KIS 토큰 발급 실패 (HTTP {resp.status_code}, "
                               f"{body.get('error_code', '')} {body.get('error_description', '')})")
        _sensitive(tok)
        issued = self.now()
        exp_str = body.get("access_token_token_expired")  # "YYYY-MM-DD HH:MM:SS"
        if exp_str:
            expires = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        else:
            expires = issued + timedelta(seconds=int(body.get("expires_in") or 0))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({
            "access_token": tok, "expires_at": expires.isoformat(), "issued_at": issued.isoformat()}),
            encoding="utf-8")
        self._token = tok
        return tok

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for i in range(len(NET_RETRY_WAITS) + 1):
            try:
                return self.http.request(method, url, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if i < len(NET_RETRY_WAITS):
                    self.sleep(NET_RETRY_WAITS[i])
        raise KisApiError(f"네트워크 오류: {type(last).__name__}")

    # --- 분봉 조회 ---
    def minute(self, params: dict, retry_rate_limit: bool = True) -> dict:
        """output2를 포함한 응답 본문. 오류면 예외.

        - EGW00201: 지수 백오프(1,2,4,...초) 후 재시도. retry_rate_limit=False면 바로 RateLimitError.
        - 토큰 오류: 1회만 재발급 후 재시도. 그래도 실패하면 KisAuthError.
        """
        tr_id, path = MINUTE
        assert tr_id in ALLOWED_TR_IDS
        backoff = 0
        token_retried = False
        while True:
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token()}",
                "appkey": self.app_key, "appsecret": self.app_secret,
                "tr_id": tr_id, "custtype": "P",
            }
            self.limiter.wait()
            self.stats.calls += 1
            resp = self._send("GET", self.base_url + path, params=params, headers=headers)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            code = body.get("msg_cd") or ""
            if code == RATE_LIMIT_CODE:
                self.stats.rate_limited += 1
                if not retry_rate_limit or backoff >= self.backoff_retries:
                    raise RateLimitError(f"{RATE_LIMIT_CODE} 초당 호출 초과")
                self.sleep(self.backoff_base * (2 ** backoff))
                backoff += 1
                continue
            if resp.status_code in (401, 403) or code in TOKEN_ERROR_CODES:
                if token_retried:
                    raise KisAuthError(f"KIS 인증 실패 (HTTP {resp.status_code}, {code} {body.get('msg1', '')})")
                token_retried = True
                self._token = None
                self.token(force=True)
                continue
            if resp.status_code != 200 or body.get("rt_cd") != "0":
                raise KisApiError(f"HTTP {resp.status_code} {code} {body.get('msg1', '')}".strip())
            self.stats.ok += 1
            return body
