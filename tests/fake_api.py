"""가짜 KIS·KRX·DART 서버 (httpx.MockTransport). 실제 API를 부르지 않는다."""
from __future__ import annotations

import io
import json
import zipfile

import httpx

from collector.http import ApiClient

TODAY = "20261118"
DAYS = [f"202611{d:02d}" for d in (18, 17, 16, 13, 12, 11, 10, 9, 6, 5, 4, 3, 2)]
CODES = {"005930": ("KOSPI", "삼성전자"), "247540": ("KOSDAQ", "에코프로비엠")}


def kis_row(day, code):
    return {"stck_bsop_date": day, "acml_vol": "1,000", "acml_tr_pbmn": "500000000000",
            "frgn_ntby_qty": "10", "orgn_ntby_qty": "-5", "prsn_ntby_qty": "-5",
            "frgn_ntby_tr_pbmn": "1200", "orgn_ntby_tr_pbmn": "-300", "prsn_ntby_tr_pbmn": "-900"}


class FakeServer:
    def __init__(self, rate_limit_once=False, kis_token_fail=False, index_rows=True):
        self.calls = []
        self.rate_limit_once = rate_limit_once
        self.kis_token_fail = kis_token_fail
        self.index_rows = index_rows
        self.token_calls = 0
        self.holiday_calls = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(url)
        p = req.url.path
        q = dict(req.url.params)
        if p == "/oauth2/tokenP":
            self.token_calls += 1
            if self.kis_token_fail:
                return httpx.Response(403, json={"error_code": "EGW00103", "error_description": "유효하지 않은 AppKey"})
            return httpx.Response(200, json={"access_token": "TOKENVALUE123456", "token_type": "Bearer",
                                             "expires_in": 86400, "access_token_token_expired": "2026-11-19 18:00:00"})
        if p.endswith("chk-holiday"):
            self.holiday_calls += 1
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "OK", "msg1": "ok", "output": [
                {"bass_dt": "20261118", "wday_dvsn_cd": "04", "bzdy_yn": "Y", "tr_day_yn": "Y", "opnd_yn": "Y", "sttl_day_yn": "Y"},
                {"bass_dt": "20261119", "wday_dvsn_cd": "05", "bzdy_yn": "Y", "tr_day_yn": "Y", "opnd_yn": "Y", "sttl_day_yn": "Y"}]})
        if p.endswith("investor-trade-by-stock-daily"):
            assert q["FID_COND_MRKT_DIV_CODE"] == "J"
            if self.rate_limit_once:
                self.rate_limit_once = False
                return httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."})
            d = q["FID_INPUT_DATE_1"]
            days = [x for x in DAYS if x <= d][:5]
            cont = "M" if req.headers.get("tr_cont") == "" and days else ""
            return httpx.Response(200, headers={"tr_cont": cont},
                                  json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output1": {},
                                        "output2": [kis_row(x, q["FID_INPUT_ISCD"]) for x in days]})
        if p.endswith("search-stock-info"):
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "x", "msg1": "ok", "output": {
                "scty_grp_id_cd": "ST", "stck_kind_cd": "101", "mket_id_cd": "STK", "prdt_abrv_name": "x"}})
        if "/sto/" in p:
            assert req.headers.get("AUTH_KEY")
            mk = "KOSPI" if "stk_" in p else "KOSDAQ"
            if "base_info" in p:
                return httpx.Response(200, json={"OutBlock_1": [
                    {"ISU_SRT_CD": "005930", "ISU_ABBRV": "삼성전자", "SECUGRP_NM": "주권", "SECT_TP_NM": "", "KIND_STKCERT_TP_NM": "보통주"},
                    {"ISU_SRT_CD": "123450", "ISU_ABBRV": "가나스팩1호", "SECUGRP_NM": "주권", "SECT_TP_NM": "SPAC(소속부없음)", "KIND_STKCERT_TP_NM": "보통주"},
                    {"ISU_SRT_CD": "005935", "ISU_ABBRV": "삼성전자우", "SECUGRP_NM": "주권", "SECT_TP_NM": "", "KIND_STKCERT_TP_NM": "구형우선주"}]})
            if q["basDd"] == TODAY:
                return httpx.Response(200, json={"OutBlock_1": []})
            return httpx.Response(200, json={"OutBlock_1": [
                {"ISU_CD": c, "ISU_NM": n, "ACC_TRDVOL": "1000", "ACC_TRDVAL": "500000000000"}
                for c, (m, n) in CODES.items() if m == mk]})
        if "/idx/" in p:
            rows = [{"IDX_NM": "코스피", "CLSPRC_IDX": "2500"}, {"IDX_NM": "코스피 200", "CLSPRC_IDX": "330"}] \
                if "kospi" in p else [{"IDX_NM": "코스닥", "CLSPRC_IDX": "800"}]
            return httpx.Response(200, json={"OutBlock_1": rows if self.index_rows else []})
        if p.endswith("corpCode.xml"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("CORPCODE.xml", "<result><list><corp_code>00126380</corp_code>"
                            "<stock_code>005930</stock_code></list></result>")
            return httpx.Response(200, content=buf.getvalue())
        if p.endswith("fnlttSinglAcntAll.json"):
            if q["fs_div"] == "CFS" and q["reprt_code"] == "11011":
                return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
            return httpx.Response(200, json={"status": "000", "message": "정상", "list": [
                {"sj_div": "CIS", "account_id": "ifrs-full_Revenue", "thstrm_nm": "제 1기", "thstrm_amount": "100", "thstrm_add_amount": "300"}]})
        return httpx.Response(404)

    def factory(self):
        transport = httpx.MockTransport(self.handler)
        return lambda name, rate: ApiClient(name, 1000, client=httpx.Client(transport=transport), sleep=lambda s: None)
