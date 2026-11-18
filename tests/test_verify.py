from datetime import datetime

import pytest

from collector import kis as kis_mod
from collector.http import ApiClient
from collector.kis import KisClient
from collector.paths import ensure_verify_dirs
from collector.settings import load_settings
from collector.timeutil import KST
from collector.verify.runner import run_verify

from .fake_api import FakeServer

NOW = datetime(2026, 11, 18, 16, 0, 0, tzinfo=KST)
KEYS = {"KRX_AUTH_KEY": "KRXKEY123456", "KIS_APP_KEY": "KISAPPKEY0001",
        "KIS_APP_SECRET": "KISSECRET00001", "DART_API_KEY": "DARTKEY000001"}


@pytest.fixture
def env(tmp_path):
    from collector.secrets import register_sensitive
    for v in KEYS.values():
        register_sensitive(v)
    settings = load_settings(tmp_path / "install")
    settings["VERIFY_STOCKS"] = "005930,247540"
    settings["VERIFY_DART_STOCKS"] = "005930"
    return settings, ensure_verify_dirs(tmp_path / "data")


def run(env, server, now=NOW, settings=None):
    s, dirs = env
    return run_verify(now=now, settings=settings or s, keys=KEYS, dirs=dirs,
                      http_factory=server.factory(), out=lambda *_: None)


def test_full_run_writes_report_without_secrets(env):
    server = FakeServer()
    report = run(env, server)
    text = report.read_text(encoding="utf-8")
    assert report.name.startswith("실측보고서_20261118-160000")
    for v in list(KEYS.values()) + ["TOKENVALUE123456"]:
        assert v not in text
    assert "키·토큰 문자열이 발견되지 않았습니다" in text
    assert "비교 기준일: 20261117" in text
    assert "응답 행 수: **2일치**" in text
    assert "코스피 200" in text
    assert "SPAC(소속부없음)" in text
    assert "status 013" not in text  # 013이면 OFS로 다시 조회
    assert "20:30" in text
    # DB를 만들지 않음
    assert not list(env[1]["root"].rglob("*.sqlite"))


def test_holiday_called_once_per_day(env):
    server = FakeServer()
    run(env, server)
    run(env, server, now=NOW.replace(hour=18))
    assert server.holiday_calls == 1
    assert server.token_calls == 1  # 토큰 캐시


def test_today_snapshots_accumulate(env):
    server = FakeServer()
    run(env, server)
    report = run(env, server, now=NOW.replace(hour=18))
    text = report.read_text(encoding="utf-8")
    assert "16:00:00" in text and "18:00:00" in text
    assert "같음" in text


def test_rate_limit_event_recorded(env):
    report = run(env, FakeServer(rate_limit_once=True))
    assert "발생 횟수: **1**" in report.read_text(encoding="utf-8")


def test_index_url_empty_means_needs_setting(env):
    s, _ = env
    s = dict(s, KRX_INDEX_KOSPI_URL="", KRX_INDEX_KOSDAQ_URL="")
    text = run(env, FakeServer(), settings=s).read_text(encoding="utf-8")
    assert "설정 필요" in text


def test_auth_failure_stops_and_reports(env):
    text = run(env, FakeServer(kis_token_fail=True)).read_text(encoding="utf-8")
    assert "인증 실패로 중단" in text


def test_disallowed_tr_id_blocked(tmp_path):
    c = KisClient(ApiClient("KIS", 10), "https://x", "a", "b", tmp_path / "t.json")
    with pytest.raises(ValueError):
        c.get(("TTTC0802U", "/uapi/domestic-stock/v1/trading/order-cash"), {})


def test_only_query_tr_ids_allowed():
    assert kis_mod.ALLOWED_TR_IDS == {"FHPTJ04160001", "CTCA0903R", "CTPF1002R", "FHPUP02120000"}


def test_before_1540_skips_investor_items(env):
    server = FakeServer()
    text = run(env, server, now=NOW.replace(hour=15, minute=30)).read_text(encoding="utf-8")
    assert server.investor_calls == 0
    assert "OPSQ2001" in text
    assert "15:40~23:59" in text


def test_item6_records_previous_trading_day(env):
    text = run(env, FakeServer()).read_text(encoding="utf-8")
    assert "| 16:00:00 | 0 | 0 | 20261117 | 1 | 1 |" in text


def test_rate_limit_halves_speed(env):
    server = FakeServer()
    server.stockinfo_rate_limits = 2
    text = run(env, server).read_text(encoding="utf-8")
    assert "발생 횟수: **2**" in text
    assert "| 1,000.00 | 500.00 |" in text and "| 500.00 | 250.00 |" in text


def test_items_10_11_compare_next_day(env):
    server = FakeServer()
    run(env, server, now=NOW.replace(hour=20, minute=30))  # 18일 밤 기록
    server.krx_empty_day = "20261119"
    nxt = NOW.replace(day=19, hour=20, minute=30)
    text = run(env, server, now=nxt).read_text(encoding="utf-8")
    # 10번: 18일 KIS 기록 vs KRX 18일 값 (가짜 서버는 같은 값 -> 차이 0)
    assert "| 20261118 | 005930 | 20:30:00 | 1,000 | 1,000 | 0 |" in text
    # 11번: 18일 KIS 지수 vs KRX 지수 행
    assert "| 20261118 | 코스피 | 2500.0 | IDX_NM=코스피; CLSPRC_IDX=2500 |" in text
