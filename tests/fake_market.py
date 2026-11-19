"""수집 테스트용 가짜 KIS·KRX 서버 (실제 API 호출 없음).

거래일: 2026-06-01 ~ 2026-11-19 평일 중 HOLIDAYS를 뺀 날. (가짜 데이터의 규칙일 뿐, 프로그램은 평일을 가정하지 않는다)
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from collector.http import ApiClient

HOLIDAYS = {"20261009", "20261003", "20260817"}


def _all_days():
    d = date(2026, 6, 1)
    while d <= date(2026, 12, 31):
        s = d.strftime("%Y%m%d")
        yield s, (d.weekday() < 5 and s not in HOLIDAYS)
        d += timedelta(days=1)


OPEN = [s for s, o in _all_days() if o]

# 종목기본정보 (실측 값 형태)
BASE = {
    "KOSPI": [
        ("005930", "삼성전자", "19750611", "주권", "", "보통주"),
        ("005935", "삼성전자우", "19890925", "주권", "", "구형우선주"),
        ("088980", "맥쿼리인프라", "20060315", "사회간접자본투융자회사", "", "보통주"),
        ("330590", "롯데리츠", "20191030", "부동산투자회사", "", "보통주"),
        ("0041B0", "신규상장", "20261110", "주권", "", "보통주"),
    ],
    "KOSDAQ": [
        ("247540", "에코프로비엠", "20190305", "주권", "우량기업부", "보통주"),
        ("123450", "교보16호스팩", "20250101", "주권", "SPAC(소속부없음)", "보통주"),
        ("123460", "가나스팩", "20250101", "주권", "중견기업부", "보통주"),
        ("222220", "관리종목사", "20100101", "주권", "관리종목(소속부없음)", "보통주"),
        ("BAD", "형식오류", "20100101", "주권", "중견기업부", "보통주"),
    ],
}


class FakeMarket:
    def __init__(self):
        self.calls: list[str] = []
        self.krx_empty: set[str] = set()  # 빈 결과를 줄 날짜
        self.krx_auth_fail = False
        self.interrupt_after: int | None = None  # KRX 호출 n회 뒤 KeyboardInterrupt
        self.krx_calls = 0
        self.krx_until = "20261119"  # 이 날짜 이상은 KRX가 빈 결과 (당일 미제공)
        self.inv_bump: dict[str, int] = {}  # 날짜별로 수급 값에 더할 값 (재조회 변경 시험)
        self.kis_time_limit = False
        self.investor_calls = 0
        self.admin_codes = {"222220"}

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, q = req.url.path, dict(req.url.params)
        self.calls.append(p)
        if p == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "TOKENVALUE123456", "access_token_token_expired": "2026-11-20 20:00:00"})
        if p.endswith("chk-holiday"):
            start = q["BASS_DT"]
            rows = [{"bass_dt": s, "opnd_yn": "Y" if o else "N"} for s, o in _all_days() if s >= start][:24]
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "x", "msg1": "ok", "output": rows})
        if p.endswith("inquire-index-daily-price"):
            d = q["FID_INPUT_DATE_1"]
            days = [s for s in reversed(OPEN) if s <= d][:100]
            base = "2500" if q["FID_INPUT_ISCD"] == "0001" else "800"
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "x", "msg1": "ok", "output1": {},
                                             "output2": [{"stck_bsop_date": s, "bstp_nmix_prpr": base} for s in days]})
        if p.endswith("investor-trade-by-stock-daily"):
            self.investor_calls += 1
            assert q["FID_COND_MRKT_DIV_CODE"] == "J"
            if self.kis_time_limit:
                return httpx.Response(200, json={"rt_cd": "1", "msg_cd": "OPSQ2001", "msg1": "TIME LIMIT 00:00 ~ 15:40"})
            code, d = q["FID_INPUT_ISCD"], q["FID_INPUT_DATE_1"]
            listed = next((l for rows in BASE.values() for c, n, l, *_ in rows if c == code), "19000101")
            days = [s for s in reversed(OPEN) if s <= d and s >= listed][:30]
            rows = [{"stck_bsop_date": s, "stck_oprc": "1000", "stck_hgpr": "1100", "stck_lwpr": "900",
                     "stck_clpr": "1050", "prdy_ctrt": "1.5", "acml_vol": "700", "acml_tr_pbmn": "735000",
                     "frgn_ntby_qty": str(10 + self.inv_bump.get(s, 0)), "orgn_ntby_qty": "-5", "prsn_ntby_qty": "-5",
                     "frgn_ntby_tr_pbmn": "12", "orgn_ntby_tr_pbmn": "-3", "prsn_ntby_tr_pbmn": "-9"} for s in days]
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output1": {}, "output2": rows})
        if p.endswith("search-stock-info"):
            code = q["PDNO"]
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "x", "msg1": "ok", "output": {
                "scty_grp_id_cd": "ST", "stck_kind_cd": "101", "admn_item_yn": "Y" if code in self.admin_codes else "N",
                "tr_stop_yn": "N"}})
        if "/sto/" in p or "/idx/" in p:
            self.krx_calls += 1
            if self.interrupt_after is not None and self.krx_calls > self.interrupt_after:
                raise KeyboardInterrupt
            if self.krx_auth_fail:
                return httpx.Response(401, json={})
            d = q["basDd"]
            if d in self.krx_empty or d not in OPEN or d >= self.krx_until:
                return httpx.Response(200, json={"OutBlock_1": []})
            mk = "KOSPI" if ("stk_" in p or "kospi" in p) else "KOSDAQ"
            if "base_info" in p:
                rows = [{"ISU_SRT_CD": c, "ISU_ABBRV": n, "LIST_DD": l, "SECUGRP_NM": g, "SECT_TP_NM": s,
                         "KIND_STKCERT_TP_NM": k, "LIST_SHRS": "1,000"} for c, n, l, g, s, k in BASE[mk] if l <= d]
            elif "/idx/" in p:
                rows = [{"IDX_NM": "코스피" if mk == "KOSPI" else "코스닥", "IDX_CLSS": "x", "CLSPRC_IDX": "2,500.10",
                         "FLUC_RT": "0.5", "OPNPRC_IDX": "2490", "HGPRC_IDX": "2510", "LWPRC_IDX": "2480",
                         "ACC_TRDVOL": "100", "ACC_TRDVAL": "200", "MKTCAP": "300"}]
            else:
                rows = [{"ISU_CD": c, "ISU_NM": n, "TDD_CLSPRC": "1,000", "FLUC_RT": "-", "TDD_OPNPRC": "990",
                         "TDD_HGPRC": "1,010", "TDD_LWPRC": "980", "ACC_TRDVOL": "500", "ACC_TRDVAL": "500000",
                         "MKTCAP": "1000000", "LIST_SHRS": "1000"} for c, n, l, *_ in BASE[mk] if l <= d]
            return httpx.Response(200, json={"OutBlock_1": rows})
        return httpx.Response(404)

    def factory(self):
        transport = httpx.MockTransport(self.handler)
        return lambda name, rate: ApiClient(name, 1000, client=httpx.Client(transport=transport), sleep=lambda s: None)
