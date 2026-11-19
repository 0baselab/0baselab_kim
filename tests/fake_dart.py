"""가짜 Open DART (실측보고서의 삼성전자 2025 값 사용)."""
from __future__ import annotations

import io
import zipfile

import httpx

from collector.http import ApiClient

T = 1_000_000  # 백만원 -> 원 변환 없이 원 단위 그대로 씀
SAMSUNG = {  # reprt_code -> (sj rows)
    "11013": {"rev": (79140503000000, 79140503000000), "ni": (8222878000000, 8222878000000), "ocf": 16580866000000},
    "11012": {"rev": (74566317000000, 153706820000000), "ni": (5116435000000, 13339313000000), "ocf": 33941002000000},
    "11014": {"rev": (86061747000000, 239768567000000), "ni": (12225747000000, 25565060000000), "ocf": 56515496000000},
    "11011": {"rev": (333605938000000, None), "ni": (45206805000000, None), "ocf": 85315148000000},
}
FILINGS = [
    # corp_code, stock_code, report_nm, rcept_no, rcept_dt
    ("00126380", "005930", "분기보고서 (2025.03)", "20250515000001", "20250515"),
    ("00126380", "005930", "반기보고서 (2025.06)", "20250814000001", "20250814"),
    ("00126380", "005930", "분기보고서 (2025.09)", "20251114000001", "20251114"),
    ("00126380", "005930", "사업보고서 (2025.12)", "20260310000001", "20260310"),
    ("01160363", "247540", "사업보고서 (2025.12)", "20260320000001", "20260320"),
    ("00999999", "123456", "사업보고서 (2025.06)", "20250920000001", "20250920"),  # 6월 결산
    ("00999998", "999999", "사업보고서 (2025.12)", "20260321000001", "20260321"),  # 대상 아님
]


def _fs_rows(reprt, rcept_no):
    d = SAMSUNG[reprt]
    rows = [
        {"rcept_no": rcept_no, "sj_div": "IS", "account_id": "ifrs-full_Revenue", "ord": "1", "account_nm": "매출액",
         "thstrm_amount": str(d["rev"][0]), "thstrm_add_amount": "" if d["rev"][1] is None else str(d["rev"][1]), "currency": "KRW"},
        {"rcept_no": rcept_no, "sj_div": "IS", "account_id": "ifrs-full_ProfitLoss", "ord": "5", "account_nm": "당기순이익",
         "thstrm_amount": str(d["ni"][0]), "thstrm_add_amount": "" if d["ni"][1] is None else str(d["ni"][1]), "currency": "KRW"},
        {"rcept_no": rcept_no, "sj_div": "CIS", "account_id": "ifrs-full_ProfitLoss", "ord": "1", "account_nm": "당기순이익",
         "thstrm_amount": "1", "thstrm_add_amount": "1", "currency": "KRW"},  # IS 우선이라 쓰이면 안 됨
        {"rcept_no": rcept_no, "sj_div": "CF", "account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities", "ord": "1",
         "account_nm": "영업활동현금흐름", "thstrm_amount": str(d["ocf"]), "thstrm_add_amount": None, "currency": "KRW"},
        {"rcept_no": rcept_no, "sj_div": "BS", "account_id": "ifrs-full_Assets", "ord": "1", "account_nm": "자산총계",
         "thstrm_amount": "500000000000000", "thstrm_add_amount": None, "currency": "KRW"},
    ]
    return rows


class FakeDart:
    def __init__(self):
        self.fs_calls = 0
        self.list_calls = 0
        self.limit_after: int | None = None
        self.calls = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, q = req.url.path, dict(req.url.params)
        self.calls += 1
        if self.limit_after is not None and self.calls > self.limit_after:
            return httpx.Response(200, json={"status": "020", "message": "사용한도 초과"})
        if p.endswith("corpCode.xml"):
            buf = io.BytesIO()
            items = "".join(f"<list><corp_code>{c}</corp_code><corp_name>n</corp_name><stock_code>{s}</stock_code>"
                            f"<modify_date>20260101</modify_date></list>" for c, s, *_ in FILINGS)
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("CORPCODE.xml", f"<result>{items}</result>")
            return httpx.Response(200, content=buf.getvalue())
        if p.endswith("list.json"):
            self.list_calls += 1
            if q.get("pblntf_ty") != "A":
                return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
            rows = [{"corp_code": c, "corp_name": "n", "stock_code": s, "report_nm": nm, "rcept_no": no, "rcept_dt": dt}
                    for c, s, nm, no, dt in FILINGS if q["bgn_de"] <= dt <= q["end_de"]]
            if not rows:
                return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
            return httpx.Response(200, json={"status": "000", "message": "정상", "page_no": 1, "total_page": 1, "list": rows})
        if p.endswith("fnlttSinglAcntAll.json"):
            self.fs_calls += 1
            corp, reprt, fs = q["corp_code"], q["reprt_code"], q["fs_div"]
            if corp == "01160363" and fs == "CFS":
                return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
            no = next(n for c, s, nm, n, dt in FILINGS if c == corp and
                      {"11013": "03", "11012": "06", "11014": "09", "11011": "12"}[reprt] in nm)
            return httpx.Response(200, json={"status": "000", "message": "정상", "list": _fs_rows(reprt, no)})
        return httpx.Response(404)

    def client(self):
        from collector.dart import DartClient
        api = ApiClient("DART", 1000, client=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda s: None)
        return DartClient(api, "https://opendart.fss.or.kr/api", "DARTKEY000001")
