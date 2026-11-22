import json
from datetime import datetime, timedelta

import httpx
import pytest

from collector.timeutil import KST
from danta.kis import KisAuthError, KisMinuteClient, RateLimitError, redact

NOW = datetime(2026, 11, 21, 10, 0, tzinfo=KST)
OK = {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output2": []}
EGW = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}
EXPIRED = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}


def make(tmp_path, minute_responses, retries=5):
    seq = list(minute_responses)
    log = {"token": 0, "minute": 0, "sleeps": []}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/oauth2/tokenP"):
            log["token"] += 1
            return httpx.Response(200, json={"access_token": f"TOKEN-{log['token']}-abcdefgh",
                                             "access_token_token_expired": "2026-11-22 10:00:00"})
        assert "inquire-time-dailychartprice" in req.url.path
        assert req.headers["tr_id"] == "FHKST03010230"
        log["minute"] += 1
        status, body = seq.pop(0)
        return httpx.Response(status, json=body)

    c = KisMinuteClient("https://x", "APPKEY-123456", "APPSECRET-123456", tmp_path / "kis_token_danta.json",
                        rate=1000, backoff_retries=retries, http=httpx.Client(transport=httpx.MockTransport(handler)),
                        now=lambda: NOW, sleep=lambda s: log["sleeps"].append(s))
    return c, log


def test_rate_limit_backoff_then_success(tmp_path):
    c, log = make(tmp_path, [(500, EGW), (500, EGW), (200, OK)])
    assert c.minute({})["rt_cd"] == "0"
    assert c.stats.rate_limited == 2 and c.stats.calls == 3
    assert [s for s in log["sleeps"] if s >= 1] == [1, 2]   # 지수 백오프


def test_rate_limit_gives_up(tmp_path):
    c, _ = make(tmp_path, [(500, EGW)] * 3, retries=2)
    with pytest.raises(RateLimitError):
        c.minute({})


def test_ratetest_mode_no_retry(tmp_path):
    c, _ = make(tmp_path, [(500, EGW)])
    with pytest.raises(RateLimitError):
        c.minute({}, retry_rate_limit=False)


def test_token_cached_and_reused(tmp_path):
    c, log = make(tmp_path, [(200, OK), (200, OK)])
    c.minute({})
    c2, log2 = make(tmp_path, [(200, OK)])
    c2.minute({})
    assert log["token"] == 1 and log2["token"] == 0
    data = json.loads((tmp_path / "kis_token_danta.json").read_text())
    assert data["expires_at"].startswith("2026-11-22T10:00:00")


def test_no_reissue_until_10min_before_expiry(tmp_path):
    p = tmp_path / "kis_token_danta.json"
    p.write_text(json.dumps({"access_token": "OLD-TOKEN-xxxxxxxx",
                             "expires_at": (NOW + timedelta(minutes=11)).isoformat(),
                             "issued_at": (NOW - timedelta(hours=23)).isoformat()}))
    c, log = make(tmp_path, [(200, OK)])
    c.minute({})
    assert log["token"] == 0
    p.write_text(json.dumps({"access_token": "OLD-TOKEN-xxxxxxxx",
                             "expires_at": (NOW + timedelta(minutes=9)).isoformat(),
                             "issued_at": (NOW - timedelta(seconds=30)).isoformat()}))
    c, log = make(tmp_path, [(200, OK)])
    c.minute({})
    assert log["token"] == 1
    assert 30 in [round(s) for s in log["sleeps"]] or 31 in [round(s) for s in log["sleeps"]]  # 1분 1회 제한 대기


def test_token_error_reissues_once(tmp_path):
    c, log = make(tmp_path, [(500, EXPIRED), (200, OK)])
    assert c.minute({})["rt_cd"] == "0"
    assert log["token"] == 2


def test_token_error_twice_stops(tmp_path):
    c, log = make(tmp_path, [(500, EXPIRED), (500, EXPIRED)])
    with pytest.raises(KisAuthError):
        c.minute({})
    assert log["token"] == 2


def test_redact_hides_keys_and_token(tmp_path):
    c, _ = make(tmp_path, [(200, OK)])
    c.minute({})
    assert "APPKEY" not in redact("key=APPKEY-123456 tok=TOKEN-1-abcdefgh")
    assert "TOKEN-1" not in redact("tok=TOKEN-1-abcdefgh")
