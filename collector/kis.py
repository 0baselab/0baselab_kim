"""KIS Open API 클라이언트 (조회 전용).

필드·URL·TR ID는 docs/kis_api_reference.xlsx 기준.
주문·정정·취소·잔고·계좌 API는 ALLOWED_TR_IDS에 없으므로 호출할 수 없다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .http import ApiClient, ApiError
from .secrets import register_sensitive
from .timeutil import KST, now_kst

TOKEN_PATH = "/oauth2/tokenP"

INVESTOR_BY_STOCK = ("FHPTJ04160001", "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily")
HOLIDAY = ("CTCA0903R", "/uapi/domestic-stock/v1/quotations/chk-holiday")
STOCK_INFO = ("CTPF1002R", "/uapi/domestic-stock/v1/quotations/search-stock-info")
MARKET_INVESTOR = ("FHPTJ04040000", "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market")
ADJ_PRICE = ("FHKST03010100", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice")  # 국내주식기간별시세
INDEX_DAILY = ("FHPUP02120000", "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price")  # 국내업종 일자별지수

# 시세·조회 API만 허용 (CLAUDE.md 절대 규칙)
ALLOWED_TR_IDS = {INVESTOR_BY_STOCK[0], HOLIDAY[0], STOCK_INFO[0], INDEX_DAILY[0], MARKET_INVESTOR[0], ADJ_PRICE[0]}

RATE_LIMIT_CODE = "EGW00201"
RATE_LIMIT_RETRIES = 3


class KisAuthError(Exception):
    pass


@dataclass
class RateLimitEvent:
    at: str
    tr_id: str
    recent_rate: int  # 직전 1초 호출 수
    rate_before: float  # 설정 속도(초당)
    rate_after: float  # 반으로 줄인 속도(초당)


@dataclass
class KisResult:
    body: dict
    tr_cont: str  # 응답 헤더 tr_cont (M/F면 다음 데이터 있음)


@dataclass
class KisClient:
    api: ApiClient
    base_url: str
    app_key: str
    app_secret: str
    token_cache: Path
    now: Callable[[], datetime] = now_kst
    rate_limit_events: list[RateLimitEvent] = field(default_factory=list)
    _token: str | None = None

    # --- 토큰: 데이터 폴더 system/에 만료 시각과 함께 캐시 (명세서 2.3) ---
    def _load_cached_token(self) -> str | None:
        if not self.token_cache.exists():
            return None
        try:
            data = json.loads(self.token_cache.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(data["expires_at"])
        except (ValueError, KeyError, json.JSONDecodeError):
            return None
        if (expires - self.now()).total_seconds() > 600:
            return data["access_token"]
        return None

    def token(self) -> str:
        if self._token:
            return self._token
        cached = self._load_cached_token()
        if cached:
            self._token = cached
            register_sensitive(cached)
            return cached
        resp = self.api.request("POST", self.base_url + TOKEN_PATH, json={
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        })
        try:
            body = resp.json()
        except ValueError:
            body = {}
        tok = body.get("access_token")
        if resp.status_code != 200 or not tok:
            # 응답 본문에는 키가 없지만, 안전을 위해 오류 코드·메시지만 전달
            raise KisAuthError(
                f"KIS 토큰 발급 실패 (HTTP {resp.status_code}, "
                f"{body.get('error_code', '')} {body.get('error_description', '')}). "
                "KIS_APP_KEY / KIS_APP_SECRET을 확인하세요.")
        register_sensitive(tok)
        expired_str = body.get("access_token_token_expired")  # "YYYY-MM-DD HH:MM:SS"
        if expired_str:
            expires = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        else:
            expires = datetime.fromtimestamp(self.now().timestamp() + int(body.get("expires_in", 0)), KST)
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache.write_text(json.dumps(
            {"access_token": tok, "expires_at": expires.isoformat()}), encoding="utf-8")
        self._token = tok
        return tok

    def get(self, api: tuple[str, str], params: dict, tr_cont: str = "") -> KisResult:
        tr_id, path = api
        if tr_id not in ALLOWED_TR_IDS:
            raise ValueError(f"허용되지 않은 TR ID: {tr_id}")
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.token()}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
                "tr_cont": tr_cont,
            }
            rate_before = self.api.limiter.recent_rate()
            resp = self.api.request("GET", self.base_url + path, params=params, headers=headers)
            try:
                body = resp.json()
            except ValueError:
                raise ApiError(f"KIS {tr_id} 응답이 JSON이 아님 (HTTP {resp.status_code})")
            if body.get("msg_cd") == RATE_LIMIT_CODE:
                # 한도 초과: 속도를 반으로 줄이고 1초 쉰 뒤 재시도. 줄인 속도는 이번 실행 끝까지 유지.
                old = self.api.limiter.per_sec
                self.api.limiter.set_rate(old / 2)
                self.rate_limit_events.append(RateLimitEvent(
                    at=self.now().strftime("%H:%M:%S"), tr_id=tr_id, recent_rate=rate_before,
                    rate_before=old, rate_after=self.api.limiter.per_sec))
                if attempt < RATE_LIMIT_RETRIES:
                    self.api.sleep(1.0)
                    continue
            if resp.status_code in (401, 403):
                raise KisAuthError(f"KIS 인증 실패 (HTTP {resp.status_code}, {body.get('msg1', '')})")
            return KisResult(body=body, tr_cont=(resp.headers.get("tr_cont") or "").strip())
        raise ApiError("도달 불가")


def investor_params(code: str, date: str) -> dict:
    """종목별 투자자매매동향 요청 값 (명세서 4.2, 고정)."""
    return {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": date,
        "FID_ORG_ADJ_PRC": "0",
        "FID_ETC_CLS_CODE": "1",
    }


def index_daily_params(index_code: str, date: str) -> dict:
    """국내업종 일자별지수 요청 값 (kis_api_reference.xlsx: 시장구분 U, 코스피 0001, 코스닥 1001, D 일별)."""
    return {
        "FID_PERIOD_DIV_CODE": "D",
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": index_code,
        "FID_INPUT_DATE_1": date,
    }


def market_investor_params(index_code: str, market_code: str, date: str) -> dict:
    """시장별 투자자매매동향 (kis_api_reference.xlsx 요청 예시: U, 0001, KSP). 날짜2와 하위코드는 문서대로 같은 값."""
    return {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_INPUT_ISCD": index_code,
        "FID_INPUT_DATE_1": date,
        "FID_INPUT_ISCD_1": market_code,
        "FID_INPUT_DATE_2": date,
        "FID_INPUT_ISCD_2": index_code,
    }


def adj_price_params(code: str, start: str, end: str) -> dict:
    """국내주식기간별시세 (kis_api_reference.xlsx). 이 API는 FID_ORG_ADJ_PRC 0 = 수정주가 (수급 API와 반대). 최대 100건."""
    return {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start,
        "FID_INPUT_DATE_2": end,
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0",
    }


def investor_window_ok(now) -> bool:
    """종목별 투자자매매동향은 00:00~15:40에 OPSQ2001 TIME LIMIT (0.1.1 실측). 15:40~23:59만 조회."""
    hm = now.hour * 60 + now.minute
    return 15 * 60 + 40 <= hm <= 23 * 60 + 59


def holiday_params(date: str) -> dict:
    return {"BASS_DT": date, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}


def stock_info_params(code: str) -> dict:
    return {"PRDT_TYPE_CD": "300", "PDNO": code}


def is_ok(body: dict) -> bool:
    return str(body.get("rt_cd")) == "0"
