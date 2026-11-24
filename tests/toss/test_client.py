import json
from datetime import datetime, timedelta

import pytest

from collector.timeutil import KST
from toss.client import NotAllowedError, TossApiError, TossAuthError, group_of, redact

from .fake import FakeToss

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=KST)


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/orders"),
    ("GET", "/api/v1/orders"),
    ("GET", "/api/v1/orders/123"),
    ("POST", "/api/v1/orders/1/cancel"),
    ("POST", "/api/v1/orders/1/modify"),
    ("POST", "/api/v1/conditional-orders"),
    ("GET", "/api/v1/conditional-orders"),
    ("DELETE", "/api/v1/conditional-orders/9"),
    ("GET", "/api/v1/sellable-quantity"),
    ("GET", "/api/v1/transactions"),
    ("POST", "/api/v1/candles"),
    ("DELETE", "/api/v1/candles"),
    ("GET", "/api/v1/candles/../orders"),
    ("GET", "/api/v1/stocks/005930/../../orders"),
    ("GET", "/api/v1/candles?x=/orders"),
    ("GET", "/api/v1/stocks/005930%2F..%2Forders/warnings"),
    ("GET", "/api/v1/candles/"),
])
def test_blocked_paths(method, path):
    with pytest.raises(NotAllowedError):
        group_of(method, path)


def test_allowed_groups():
    assert group_of("GET", "/api/v1/candles") == "MARKET_DATA_CHART"
    assert group_of("GET", "/api/v1/stocks/all") == "STOCK_ALL"
    assert group_of("GET", "/api/v1/stocks/0101N0/warnings") == "STOCK"
    assert group_of("GET", "/api/v1/sectors/rankings") == "SECTOR_RANKING"
    assert group_of("GET", "/api/v1/sectors/12") == "SECTOR"
    assert group_of("GET", "/api/v1/market-indicators/KOSPI/candles") == "MARKET_INDICATOR_CHART"
    assert group_of("POST", "/oauth2/token") == "AUTH"


def test_blocked_call_sends_nothing(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    with pytest.raises(NotAllowedError):
        c.get("/api/v1/orders")
    with pytest.raises(NotAllowedError):
        c._send("POST", "/api/v1/orders", json={})
    assert srv.calls == []            # 토큰 발급도, 주문 요청도 나가지 않음


def test_token_cached_and_reused(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.get("/api/v1/accounts")
    c.get("/api/v1/accounts")
    c2, _ = srv.client(tmp_path)
    c2.get("/api/v1/accounts")
    assert srv.issued == 1
    saved = json.loads((tmp_path / "toss_token.json").read_text())
    assert saved["expires_at"].startswith("2026-11-25T10:00:00")


def test_no_reissue_until_10min_before_expiry(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.get("/api/v1/accounts")
    p = tmp_path / "toss_token.json"
    d = json.loads(p.read_text())
    d["expires_at"] = (NOW + timedelta(minutes=11)).isoformat()
    p.write_text(json.dumps(d))
    srv.client(tmp_path)[0].get("/api/v1/accounts")
    assert srv.issued == 1
    d["expires_at"] = (NOW + timedelta(minutes=9)).isoformat()
    p.write_text(json.dumps(d))
    srv.client(tmp_path)[0].get("/api/v1/accounts")
    assert srv.issued == 2


def test_revoked_token_reissued_once(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.get("/api/v1/accounts")
    srv.valid = "SOMEONE-ELSE"                 # 다른 곳에서 재발급되어 무효화됨
    assert c.get("/api/v1/accounts")["result"]
    assert srv.issued == 2


def test_auth_error_after_one_reissue(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.get("/api/v1/accounts")
    srv.script = [(401, {"error": {"code": "expired-token", "message": "x"}}, {}),
                  (401, {"error": {"code": "expired-token", "message": "x"}}, {})]
    with pytest.raises(TossAuthError):
        c.get("/api/v1/accounts")
    assert srv.issued == 2


def test_429_waits_retry_after(tmp_path):
    srv = FakeToss()
    c, sleeps = srv.client(tmp_path)
    c.token()
    srv.script = [(429, {"error": {"code": "rate-limit-exceeded"}}, {"Retry-After": "3"}),
                  (429, {"error": {"code": "rate-limit-exceeded"}}, {"Retry-After": "2"})]
    assert c.get("/api/v1/accounts")["result"]
    assert [s for s in sleeps if s >= 1] == [3.0, 2.0] and c.n429 == 2


def test_429_gives_up(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path, retry_429=2)
    c.token()
    srv.script = [(429, {}, {"Retry-After": "1"})] * 3
    with pytest.raises(TossApiError):
        c.get("/api/v1/accounts")


def test_ip_not_allowed(tmp_path):
    from toss.client import IpNotAllowedError
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.token()
    srv.script = [(403, {"error": {"code": "ip-not-allowed", "message": "x"}}, {})]
    with pytest.raises(IpNotAllowedError):
        c.get("/api/v1/accounts")


def test_rate_headers_recorded_and_secrets_redacted(tmp_path):
    recs = []
    srv = FakeToss()
    c, _ = srv.client(tmp_path, on_rate=recs.append)
    c.get("/api/v1/accounts")
    g = [r for r in recs if r.group == "ACCOUNT"][0]
    assert (g.limit, g.remaining, g.status) == ("20", "19", 200)
    assert "CLIENTID" not in redact("id=CLIENTID-123456 secret=SECRET-1234567 tok=TOKEN-1-abcdefgh")
    assert "TOKEN-1" not in redact("TOKEN-1-abcdefgh")
