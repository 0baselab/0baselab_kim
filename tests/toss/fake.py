"""가짜 토스 서버 (실제 API 호출 없음)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx

from collector.timeutil import KST


def minute_bars(day: str, start="09:00", end="15:30", extra=()):
    """종목 1분봉: timestamp = 봉 종료 시각. 반환은 최신순."""
    d = datetime.strptime(day, "%Y%m%d").replace(tzinfo=KST)
    t = d.replace(hour=int(start[:2]), minute=int(start[3:]))
    e = d.replace(hour=int(end[:2]), minute=int(end[3:]))
    out = []
    i = 0
    while t <= e:
        i += 1
        out.append({"timestamp": (t + timedelta(minutes=1)).isoformat(), "openPrice": str(100 + i),
                    "highPrice": str(101 + i), "lowPrice": str(99 + i), "closePrice": str(100 + i),
                    "volume": "10", "currency": "KRW"})
        t += timedelta(minutes=1)
    for hm in extra:  # NXT 시간대 봉 (봉 시작 시각 HH:MM)
        s = d.replace(hour=int(hm[:2]), minute=int(hm[3:]))
        out.append({"timestamp": (s + timedelta(minutes=1)).isoformat(), "openPrice": "1", "highPrice": "1",
                    "lowPrice": "1", "closePrice": "1", "volume": "5", "currency": "KRW"})
    return sorted(out, key=lambda b: b["timestamp"], reverse=True)


class FakeToss:
    def __init__(self, bars=None, expires_in=86400, daily=None, rankings=None):
        self.bars = bars or []                # 최신순
        self.daily = daily or []              # 1d 봉 (최신순)
        self.rankings = rankings or []
        self.expires_in = expires_in
        self.issued = 0
        self.valid = None
        self.calls = []                       # (method, path, params)
        self.script = []                      # 다음 GET 응답을 강제로 바꿀 때: (status, body, headers)

    def handler(self, req: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        self.calls.append((req.method, req.url.path, params))
        if req.url.path == "/oauth2/token":
            self.issued += 1
            self.valid = f"TOKEN-{self.issued}-abcdefgh"
            return httpx.Response(200, json={"access_token": self.valid, "token_type": "Bearer",
                                             "expires_in": self.expires_in})
        if self.script:
            st, body, hdr = self.script.pop(0)
            return httpx.Response(st, json=body, headers=hdr)
        if req.headers.get("Authorization") != f"Bearer {self.valid}":
            return httpx.Response(401, json={"error": {"code": "token-revoked", "message": "x"}})
        hdr = {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "19", "X-RateLimit-Reset": "0.05"}
        if req.url.path == "/api/v1/candles":
            before = params.get("before")
            count = int(params.get("count", 100))
            src = self.daily if params.get("interval") == "1d" else self.bars
            sel = [b for b in src if before is None or
                   datetime.fromisoformat(b["timestamp"]) <= datetime.fromisoformat(before)]
            page = sel[:count]
            nb = None
            if len(sel) > count:
                nb = (datetime.fromisoformat(page[-1]["timestamp"]) - timedelta(seconds=1)).isoformat()
            return httpx.Response(200, json={"result": {"candles": page, "nextBefore": nb}}, headers=hdr)
        if req.url.path == "/api/v1/rankings":
            return httpx.Response(200, json={"result": {"rankedAt": "x", "rankings": self.rankings}}, headers=hdr)
        if req.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"result": [
                {"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"}]}, headers=hdr)
        return httpx.Response(200, json={"result": {}}, headers=hdr)

    def client(self, tmp_path, **kw):
        from toss.client import TossClient
        sleeps = kw.pop("sleeps", [])
        c = TossClient("https://x", "CLIENTID-123456", "SECRET-1234567", tmp_path / "toss_token.json",
                       rate_factor=1000, http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                       now=kw.pop("now", lambda: datetime(2026, 11, 24, 10, 0, tzinfo=KST)),
                       sleep=sleeps.append, **kw)
        return c, sleeps
