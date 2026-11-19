import os
from datetime import datetime

import pytest

from collector import app as app_mod
from collector import db, maintenance
from collector import trading_calendar as cal
from collector.collect import run_collect
from collector.lock import AlreadyRunning, Lock
from collector.secrets import save_keys
from collector.status import report
from collector.timeutil import KST
from collector.universe import exclude_reason

from .fake_market import OPEN, FakeMarket

NOW = datetime(2026, 11, 19, 20, 30, tzinfo=KST)
KEYS = {"KRX_AUTH_KEY": "KRXKEY123456", "KIS_APP_KEY": "KISAPPKEY0001",
        "KIS_APP_SECRET": "KISSECRET00001", "DART_API_KEY": "DARTKEY000001"}


@pytest.fixture
def install(tmp_path):
    inst = tmp_path / "install"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "settings.env").write_text(
        f"DATA_DIR={tmp_path / 'data'}\nKRX_RETENTION_DAYS=30\n", encoding="utf-8")
    save_keys(inst, KEYS)
    return inst


def start(install, market, now=NOW):
    return app_mod.start(install=install, now=lambda: now, http_factory=market.factory(), out=lambda *_: None)


def collect(install, market, now=NOW, **kw):
    a = start(install, market, now)
    try:
        return a, run_collect(a, out=lambda *_: None, **kw)
    finally:
        a.close()


def reopen(install):
    from collector.db import connect
    return connect(install.parent / "data" / "db" / "market.sqlite")


def test_initial_daily_load(install):
    m = FakeMarket()
    a, res = collect(install, m)
    con = reopen(install)
    past = [d for d in OPEN if d < "20261119"]
    # 달력: 휴장일은 is_open=0, 주말도 0, 오늘·미래는 휴장일조회
    assert cal.is_open(con, "20261009") is False
    assert cal.is_open(con, "20261114") is False  # 토요일
    assert cal.is_open(con, "20261118") is True
    assert cal.is_open(con, "20261119") is True
    # KRX: 최근 30거래일 x 6종류 모두 success, 오늘은 받지 않음
    targets = past[-30:]
    n_ok = con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND status='success'").fetchone()[0]
    assert n_ok == 30 * 6
    assert con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND unit_key='20261119'").fetchone()[0] == 0
    assert con.execute("SELECT MIN(date) FROM price_daily").fetchone()[0] == targets[0]
    # 영문 섞인 종목코드는 저장, 형식 오류는 건너뜀
    assert con.execute("SELECT COUNT(*) FROM price_daily WHERE code='0041B0'").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM price_daily WHERE code='BAD'").fetchone()[0] == 0
    # '-' 는 NULL (0으로 채우지 않음)
    assert con.execute("SELECT change_rate_pct FROM price_daily LIMIT 1").fetchone()[0] is None
    r = con.execute("SELECT * FROM price_daily WHERE code='005930' AND date='20261118'").fetchone()
    assert (r["close"], r["volume_sh"], r["value_krw"], r["is_provisional"], r["source"]) == (1000, 500, 500000, 0, "KRX")
    assert r["raw_file"].startswith("raw/krx/2026-11/")
    assert con.execute("SELECT close FROM index_daily WHERE series='KOSPI' AND index_name='코스피'").fetchone()[0] == 2500.1
    # 대상 종목 필터 (14.3)
    uni = {r["code"]: r for r in con.execute("SELECT * FROM universe_daily WHERE date='20261118'")}
    assert uni["005930"]["in_universe"] == 1
    assert uni["005935"]["exclude_reason"] == "not_common"
    assert uni["088980"]["exclude_reason"] == "not_stock"
    assert uni["330590"]["exclude_reason"] == "reits"
    assert uni["123450"]["exclude_reason"] == "spac"
    assert uni["123460"]["exclude_reason"] == "spac"  # 이름 규칙
    assert uni["222220"]["in_universe"] == 1 and uni["222220"]["sect_note"] == "관리종목(소속부없음)"
    # 신규 상장: 2026-11-10 상장 -> 11/18은 7거래일째
    assert uni["0041B0"]["days_since_listing"] == 7
    assert uni["0041B0"]["is_new_listing"] == 1
    assert uni["0041B0"]["max_window"] == "5"
    assert uni["0041B0"]["listing_note"] == "상장 7거래일째, 20일 지표 미산정"
    assert any("대상 종목" in l for l in res.lines)
    assert list((install.parent / "data" / "backup").glob("market_*.sqlite"))


def test_rerun_skips_success_and_daily_calls(install):
    m = FakeMarket()
    collect(install, m)
    m.calls.clear()
    m2 = m
    collect(install, m2, now=NOW.replace(minute=45))
    assert not any("/sto/" in c or "/idx/" in c for c in m2.calls)
    assert not any("chk-holiday" in c for c in m2.calls)
    assert not any("index-daily" in c for c in m2.calls)


def test_empty_is_not_success_and_retried(install):
    m = FakeMarket()
    m.krx_empty = {"20261118"}
    collect(install, m)
    con = reopen(install)
    st = {r[0] for r in con.execute("SELECT status FROM collect_log WHERE source='KRX' AND unit_key='20261118'")}
    assert st == {"empty"}
    m.krx_empty = set()
    collect(install, m, now=NOW.replace(minute=50))
    st = {r[0] for r in con.execute("SELECT status FROM collect_log WHERE source='KRX' AND unit_key='20261118'")}
    assert st == {"success"}


def test_empty_streak_counts(install):
    m = FakeMarket()
    m.krx_empty = {"20261118"}
    for minute in (31, 32, 33):
        collect(install, m, now=NOW.replace(minute=minute))
    con = reopen(install)
    assert con.execute("SELECT MIN(empty_streak) FROM collect_log WHERE unit_key='20261118'").fetchone()[0] == 3
    a = start(install, m, NOW.replace(minute=40))
    try:
        assert any("재확인 필요 (빈 결과 3회 연속): 6건" in l for l in report(a))
    finally:
        a.close()


def test_provisional_replaced_and_change_recorded(install):
    m = FakeMarket()
    a = start(install, m)
    a.con.execute("INSERT INTO price_daily(date, code, market, close, volume_sh, value_krw, is_provisional, source)"
                  " VALUES('20261118','005930','KOSPI',1001,400,400000,1,'KIS')")
    a.close()
    collect(install, m)
    con = reopen(install)
    r = con.execute("SELECT * FROM price_daily WHERE code='005930' AND date='20261118'").fetchone()
    assert (r["is_provisional"], r["source"], r["volume_sh"]) == (0, "KRX", 500)
    ch = {(c["col"], c["old"], c["new"]) for c in con.execute("SELECT * FROM value_changes")}
    assert ("volume_sh", "400", "500") in ch and ("close", "1001.0", "1000.0") in ch


def test_interrupt_keeps_finished_units(install):
    m = FakeMarket()
    m.interrupt_after = 10
    a, res = collect(install, m)
    assert res.stopped == "사용자 중단"
    con = reopen(install)
    assert con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND status='success'").fetchone()[0] == 10
    m.interrupt_after = None
    collect(install, m, now=NOW.replace(minute=55))
    assert con.execute("SELECT COUNT(*) FROM collect_log WHERE source='KRX' AND status='success'").fetchone()[0] == 180


def test_initial_load_can_be_declined(install):
    m = FakeMarket()
    collect(install, m, confirm=lambda q: False)
    assert m.krx_calls == 0


def test_krx_auth_failure_stops(install):
    m = FakeMarket()
    m.krx_auth_fail = True
    a, res = collect(install, m)
    assert res.stopped and "인증 실패" in res.stopped
    assert m.krx_calls == 1


def test_range_mode(install):
    m = FakeMarket()
    collect(install, m, mode="range", start="20261102", end="20261106")
    con = reopen(install)
    days = {r[0] for r in con.execute("SELECT DISTINCT unit_key FROM collect_log WHERE source='KRX'")}
    assert days == {"20261102", "20261103", "20261104", "20261105", "20261106"}


def test_exclude_reason_order():
    assert exclude_reason("부동산투자회사", "보통주", None, "x리츠") == "reits"
    assert exclude_reason("주권", "종류주권", None, "x") == "not_common"
    assert exclude_reason("주권", "보통주", "SPAC(소속부없음)", "x") == "spac"
    assert exclude_reason("주권", "보통주", None, "삼성전자") is None


def test_lock(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text("999999999", encoding="utf-8")  # 죽은 프로세스
    with Lock(p):
        assert p.read_text() == str(os.getpid())
        p2 = Lock(p)
        p.write_text(str(os.getppid()), encoding="utf-8")  # 살아 있는 다른 프로세스
        with pytest.raises(AlreadyRunning):
            p2.acquire()


def test_db_version_guard(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    assert db.init_db(con) == 1
    con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(db.DbError):
        db.init_db(con)


def test_backup_and_log_cleanup(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    db.init_db(con)
    for i in range(9):
        maintenance.backup_db(con, tmp_path / "b", NOW.replace(second=i), keep=7)
    assert len(list((tmp_path / "b").glob("*.sqlite"))) == 7
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run_20260701-100000.log").write_text("x")
    (logs / "run_20261118-100000.log").write_text("x")
    assert maintenance.cleanup_logs(logs, NOW, 90) == 1
    assert [p.name for p in logs.iterdir()] == ["run_20261118-100000.log"]
