"""Open DART 클라이언트 (명세서 4.3, 9장)."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

from .http import ApiClient, ApiError

# 보고서 코드: 1분기·반기·3분기·사업보고서
REPORT_CODES = {"11013": "1분기", "11012": "반기", "11014": "3분기", "11011": "사업보고서"}
NO_DATA = "013"
LIMIT_EXCEEDED = "020"


class DartLimitError(Exception):
    pass


@dataclass
class DartClient:
    api: ApiClient
    base_url: str
    api_key: str

    def corp_list(self) -> list[dict]:
        """corpCode.xml 전체: corp_code, corp_name, stock_code, modify_date."""
        resp = self.api.request("GET", self.base_url + "/corpCode.xml", params={"crtfc_key": self.api_key})
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile:
            raise ApiError(f"DART corpCode.xml 응답이 ZIP이 아님 (HTTP {resp.status_code}). DART_API_KEY를 확인하세요.")
        root = ET.fromstring(zf.read(zf.namelist()[0]))
        return [{k: (item.findtext(k) or "").strip() for k in ("corp_code", "corp_name", "stock_code", "modify_date")}
                for item in root.iter("list")]

    def filings(self, bgn_de: str, end_de: str, page_no: int, pblntf_ty: str | None = None,
                corp_code: str | None = None) -> dict:
        """공시검색 list.json. 요청 값은 Open DART 개발가이드 기준 (이 개발 환경에서 문서 재확인 불가, 확인 필요)."""
        params = {"crtfc_key": self.api_key, "bgn_de": bgn_de, "end_de": end_de,
                  "page_no": str(page_no), "page_count": "100"}
        if pblntf_ty:
            params["pblntf_ty"] = pblntf_ty
        if corp_code:
            params["corp_code"] = corp_code
        resp = self.api.request("GET", self.base_url + "/list.json", params=params)
        try:
            body = resp.json()
        except ValueError:
            raise ApiError(f"DART list.json 응답이 JSON이 아님 (HTTP {resp.status_code})")
        if body.get("status") == LIMIT_EXCEEDED:
            raise DartLimitError("DART 하루 호출 한도 초과 (020)")
        return body

    def corp_codes(self) -> dict[str, str]:
        """종목코드 -> corp_code"""
        resp = self.api.request("GET", self.base_url + "/corpCode.xml", params={"crtfc_key": self.api_key})
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile:
            raise ApiError(f"DART corpCode.xml 응답이 ZIP이 아님 (HTTP {resp.status_code}). DART_API_KEY를 확인하세요.")
        root = ET.fromstring(zf.read(zf.namelist()[0]))
        out = {}
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if stock:
                out[stock] = (item.findtext("corp_code") or "").strip()
        return out

    def fs_all(self, corp_code: str, year: str, reprt_code: str, fs_div: str) -> dict:
        resp = self.api.request("GET", self.base_url + "/fnlttSinglAcntAll.json", params={
            "crtfc_key": self.api_key, "corp_code": corp_code, "bsns_year": year,
            "reprt_code": reprt_code, "fs_div": fs_div})
        try:
            body = resp.json()
        except ValueError:
            raise ApiError(f"DART fnlttSinglAcntAll 응답이 JSON이 아님 (HTTP {resp.status_code})")
        if body.get("status") == LIMIT_EXCEEDED:
            raise DartLimitError("DART 하루 호출 한도 초과 (020)")
        return body
