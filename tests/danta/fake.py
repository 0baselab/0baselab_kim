"""가짜 분봉 서버 (실제 API 호출 없음)."""
from __future__ import annotations

from datetime import datetime, timedelta


def day_times(start="090000", end="153000"):
    t = datetime.strptime(start, "%H%M%S")
    e = datetime.strptime(end, "%H%M%S")
    out = []
    while t <= e:
        out.append(t.strftime("%H%M%S"))
        t += timedelta(minutes=1)
    return out


def make_row(date, t, acml):
    return {"stck_bsop_date": date, "stck_cntg_hour": t, "stck_prpr": "100", "stck_oprc": "99",
            "stck_hgpr": "101", "stck_lwpr": "98", "cntg_vol": "10", "acml_tr_pbmn": str(acml)}


class FakeMinuteServer:
    """{date: [times]} 데이터에서 입력 시각부터 과거로 최대 120건 (날짜를 넘어감)."""

    def __init__(self, days: dict[str, list[str]]):
        self.rows = []
        for d in sorted(days):
            acml = 0
            for t in sorted(days[d]):
                acml += 1000
                self.rows.append(make_row(d, t, acml))
        self.rows.sort(key=lambda r: (r["stck_bsop_date"], r["stck_cntg_hour"]), reverse=True)
        self.calls = []

    def respond(self, params):
        self.calls.append(dict(params))
        key = (params["FID_INPUT_DATE_1"], params["FID_INPUT_HOUR_1"])
        sel = [r for r in self.rows if (r["stck_bsop_date"], r["stck_cntg_hour"]) <= key][:120]
        return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상", "output1": {}, "output2": sel}


class FakeClient:
    """fetch_day용: client.minute(params)만 흉내."""

    def __init__(self, server):
        self.server = server

    def minute(self, params, retry_rate_limit=True):
        return self.server.respond(params)
