"""KRX Open API 클라이언트.

인증 헤더 이름(AUTH_KEY)과 응답 배열 키(OutBlock_1)는 명세서 4.1이 인용한
공개 클라이언트 코드 기준이며, 0단계 실측으로 확인한다 (확인 필요).
"""
from __future__ import annotations

from dataclasses import dataclass

from .http import ApiClient, ApiError

AUTH_HEADER = "AUTH_KEY"
ROWS_KEY = "OutBlock_1"

TRADE_PATHS = {"KOSPI": "/sto/stk_bydd_trd", "KOSDAQ": "/sto/ksq_bydd_trd"}
BASE_INFO_PATHS = {"KOSPI": "/sto/stk_isu_base_info", "KOSDAQ": "/sto/ksq_isu_base_info"}


@dataclass
class KrxClient:
    api: ApiClient
    base_url: str
    auth_key: str

    def get_url(self, url: str, bas_dd: str) -> dict:
        resp = self.api.request("GET", url, params={"basDd": bas_dd},
                                headers={AUTH_HEADER: self.auth_key})
        if resp.status_code in (401, 403):
            raise ApiError(f"KRX 인증 실패 (HTTP {resp.status_code}). KRX_AUTH_KEY 또는 서비스 이용 승인을 확인하세요.")
        try:
            return resp.json()
        except ValueError:
            raise ApiError(f"KRX 응답이 JSON이 아님 (HTTP {resp.status_code})")

    def get(self, path: str, bas_dd: str) -> dict:
        return self.get_url(self.base_url.rstrip("/") + path, bas_dd)


def rows(body: dict) -> list[dict]:
    r = body.get(ROWS_KEY)
    return r if isinstance(r, list) else []


def short_code(row: dict) -> str | None:
    """행에서 6자리 종목코드를 찾는다. 필드명이 확정되지 않아 후보를 순서대로 본다."""
    for key in ("ISU_SRT_CD", "ISU_CD"):
        v = str(row.get(key) or "").strip()
        if len(v) == 6:
            return v
        if len(v) == 12 and v.startswith("KR"):  # 표준코드 KR7005930003 -> 005930
            return v[3:9]
    return None


def name_of(row: dict) -> str:
    return str(row.get("ISU_ABBRV") or row.get("ISU_NM") or "")
