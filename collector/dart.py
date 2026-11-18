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
        body = resp.json()
        if body.get("status") == LIMIT_EXCEEDED:
            raise DartLimitError("DART 하루 호출 한도 초과 (020)")
        return body
