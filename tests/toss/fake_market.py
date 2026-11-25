"""수집기 테스트용 가짜 토스 시장 (실제 API 호출 없음)."""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from collector.timeutil import KST


def iso(day: str, hm: str) -> str:
    return datetime.fromisoformat(f"{day}T{hm}:00+09:00").isoformat()


class FakeMarket:
    def __init__(self, days, symbols, list_dates=None, prev_day=None, fail=None, nxt=None, split=None):
        self.days = sorted(days)                      # YYYY-MM-DD 거래일
        self.symbols = symbols                        # {code: market}
        self.list_dates = list_dates or {}
        self.prev_day = prev_day or self.days[-1]
        self.fail = fail or set()                     # 분봉 조회가 실패할 (code)
        self.nxt = nxt or set()                       # NXT 거래 가능 종목
        self.split = split or {}                      # {code: 날짜} 이 날 2:1 액면분할 (수정주가는 그 전 가격이 절반)
        self.calls = []
        self.token = "TOKEN-xyz-12345678"

    def minute(self, code, day):
        """08:00~19:59 시작 720봉. 장전 거래량 0, 정규장 거래량 10, 장후는 15:45 시작 봉만 거래량 5. 최신순."""
        out = []
        base = datetime.fromisoformat(f"{day}T08:00:00+09:00")
        for i in range(720):
            st = base + timedelta(minutes=i)
            t = st.hour * 60 + st.minute
            v = 10 if 540 <= t <= 930 else (5 if t == 945 else 0)
            p = 100 + (i % 7)
            out.append({"timestamp": (st + timedelta(minutes=1)).isoformat(), "openPrice": str(p), "highPrice": str(p + 1),
                        "lowPrice": str(p - 1), "closePrice": str(p), "volume": str(v), "currency": "KRW"})
        return out[::-1]

    def daily(self, code):
        return [{"timestamp": iso(d, "00:00"), "openPrice": "100", "highPrice": "110", "lowPrice": "90",
                 "closePrice": "105", "volume": "1000", "currency": "KRW"}
                for d in self.days[::-1] if d >= self.list_dates.get(code, "0000")]

    def _page(self, src, params):
        before = params.get("before")
        count = int(params.get("count", 100))
        sel = [b for b in src if before is None or
               datetime.fromisoformat(b["timestamp"]) <= datetime.fromisoformat(before)]
        page = sel[:count]
        nb = (datetime.fromisoformat(page[-1]["timestamp"]) - timedelta(seconds=1)).isoformat() if len(sel) > count else None
        return {"result": {"candles": page, "nextBefore": nb}}

    def handler(self, req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        path = req.url.path
        self.calls.append((path, q))
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": self.token, "token_type": "Bearer", "expires_in": 86400})
        if path == "/api/v1/market-calendar/KR":
            return httpx.Response(200, json={"result": {"previousBusinessDay": {"date": self.prev_day}}})
        if path == "/api/v1/stocks/all":
            assert q.get("securityType") == "STOCK" and q.get("commonShare") == "true"
            res = [{"symbol": c, "name": c, "securityType": "STOCK", "isCommonShare": True}
                   for c, m in self.symbols.items() if m == q["market"]]
            return httpx.Response(200, json={"result": res})
        if path == "/api/v1/stocks":
            return httpx.Response(200, json={"result": [{"symbol": s, "listDate": self.list_dates.get(s, "2000-01-01"),
                                                         "sharesOutstanding": "1000000",
                                                         "koreanMarketDetail": {"nxtSupported": s in self.nxt}}
                                                        for s in q["symbols"].split(",")]})
        if path in ("/api/v1/market-indicators/KOSPI/candles", "/api/v1/market-indicators/KOSDAQ/candles") \
                and q.get("interval") == "1m":
            base = 2500 if "KOSPI" in path else 800
            src = []
            for d in self.days[::-1]:   # 지수 분봉: timestamp = 봉 시작 시각, 09:00~15:30
                b0 = datetime.fromisoformat(f"{d}T09:00:00+09:00")
                src += [{"timestamp": (b0 + timedelta(minutes=i)).isoformat(), "openPrice": str(base), "highPrice": str(base + 1),
                         "lowPrice": str(base - 1), "closePrice": str(base + i % 3), "volume": "1"} for i in range(391)][::-1]
            return httpx.Response(200, json=self._page(src, q))
        if path in ("/api/v1/market-indicators/KOSPI/candles", "/api/v1/market-indicators/KOSDAQ/candles"):
            base = 2500 if "KOSPI" in path else 800
            return httpx.Response(200, json=self._page(
                [{"timestamp": iso(d, "00:00"), "openPrice": str(base), "highPrice": str(base + 10),
                  "lowPrice": str(base - 10), "closePrice": str(base + 5), "volume": "1"} for d in self.days[::-1]], q))
        if path == "/api/v1/candles":
            code = q["symbol"]
            if q["interval"] == "1d":
                bars = self.daily(code)
                if q.get("adjusted") == "true" and code in self.split:
                    bars = [dict(b, closePrice="52.5") if b["timestamp"][:10] < self.split[code] else b for b in bars]
                return httpx.Response(200, json=self._page(bars, q))
            if code in self.fail:
                return httpx.Response(500, json={"error": {"code": "internal-error", "message": "x"}})
            src = []
            for d in self.days[::-1]:
                if d >= self.list_dates.get(code, "0000"):
                    src += self.minute(code, d)
            return httpx.Response(200, json=self._page(src, q))
        if path == "/api/v1/sectors":
            return httpx.Response(200, json={"result": {"sectors": [
                {"sectorId": "10", "name": "IT", "depth": 0, "parentSectorId": None},
                {"sectorId": "100", "name": "반도체", "depth": 1, "parentSectorId": "10"}]}})
        if path.startswith("/api/v1/sectors/") and path.endswith("/stocks"):
            assert q.get("marketCountry") == "KR"
            sid = path.split("/")[4]
            codes = sorted(self.symbols) if sid == "10" else sorted(self.symbols)[:1]
            return httpx.Response(200, json={"result": {"stocks": [
                {"symbol": c, "name": c, "volume": "1", "tradingAmount": "1", "marketCapitalization": "5000000000",
                 "currency": "KRW"} for c in codes]}})
        if path.endswith("/investor-trading"):
            until = q.get("until") or self.days[-1]
            recs = [{"date": d, "foreigner": {"netBuyVolume": "3"}, "institution": {"netBuyVolume": "-2"},
                     "individual": None, "otherCorporation": {"netBuyVolume": "1"}, "updatedAt": "x"}
                    for d in self.days[::-1] if d <= until][: int(q.get("count", 10))]
            nu = None
            if recs and recs[-1]["date"] > self.days[0]:
                nu = (datetime.fromisoformat(recs[-1]["date"]) - timedelta(days=1)).date().isoformat()
            return httpx.Response(200, json={"result": {"records": recs, "nextUntil": nu}})
        return httpx.Response(404, json={"error": {"code": "edge-blocked", "message": path}})

    def client(self, tmp_path):
        from toss.client import TossClient
        return TossClient("https://x", "CLIENTID-123456", "SECRET-1234567", tmp_path / "tok.json", rate_factor=1000,
                          group_rates={"MARKET_DATA_CHART": 1000},
                          http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                          now=lambda: datetime(2026, 11, 24, 8, 0, tzinfo=KST), sleep=lambda s: None)
