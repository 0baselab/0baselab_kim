import sqlite3
from datetime import datetime

from collector.timeutil import KST
from toss import collect, datastore as ds

from .fake_market import FakeMarket

NOW = lambda: datetime(2026, 11, 24, 8, 0, tzinfo=KST)
DAYS = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"]   # 21·22일은 주말(거래일 아님)


def setup(tmp_path, **kw):
    m = FakeMarket(DAYS, {"000001": "KOSPI", "000002": "KOSDAQ", "000003": "KOSDAQ"}, **kw)
    c = m.client(tmp_path)
    store = tmp_path / "toss_data"
    return m, c, ds.connect_daily(store), store


def test_universe_and_trading_days(tmp_path):
    m, c, d, store = setup(tmp_path, list_dates={"000003": "2026-11-20"})
    assert collect.refresh_universe(c, d, lambda x: None, NOW) == 3
    assert d.execute("SELECT list_date FROM universe WHERE code='000003'").fetchone()[0] == "2026-11-20"
    collect.refresh_trading_days(c, d, lambda x: None, "2026-11-01")
    assert collect.trading_days(d, "2026-11-01", "2026-11-30") == DAYS          # 주말은 없음
    assert collect.active_codes(d, "2026-11-19") == ["000001", "000002"]        # 상장 전 종목 제외
    m.symbols.pop("000002")
    collect.refresh_universe(c, d, lambda x: None, NOW)
    assert collect.active_codes(d) == ["000001", "000003"]
    assert d.execute("SELECT active FROM universe WHERE code='000002'").fetchone()[0] == 0


def test_daily_update_initial_and_incremental(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-20")
    r = collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30)
    assert r["end"] == "2026-11-20"
    rows = d.execute("SELECT code, date, close, volume, value_est_krw, value_est_src FROM daily_bar ORDER BY code, date").fetchall()
    assert len(rows) == 9 and rows[0] == ("000001", "2026-11-18", 105, 1000, 105000, "daily_close")
    assert "2026-11-23" not in {r[1] for r in rows}                             # 전 영업일 이후는 받지 않음
    m.prev_day = "2026-11-23"
    m.calls.clear()
    collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30)
    assert d.execute("SELECT COUNT(*) FROM daily_bar WHERE date='2026-11-23'").fetchone()[0] == 3
    befores = [q.get("before") for p, q in m.calls if p == "/api/v1/candles"]
    assert len(befores) == 3                                                    # 종목당 1회만
    m.calls.clear()
    collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30)
    assert not [p for p, q in m.calls if p == "/api/v1/candles"]               # 이미 최신이면 호출 없음


def test_daily_update_with_investor(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23")
    collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30, investor=True)
    rows = d.execute("SELECT code, date, frgn_net_vol, orgn_net_vol, prsn_net_vol, other_net_vol FROM investor_daily"
                     " WHERE code='000001' ORDER BY date").fetchall()
    assert [r[1] for r in rows] == DAYS and rows[0][2:] == (3, -2, None, 1)


def test_minute_update_files_and_filtering(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23", list_dates={"000003": "2026-11-20"})
    collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30)
    logs = []
    r = collect.minute_update(c, d, store, logs.append, NOW, history_days=30)
    assert r["days_done"] == ["2026-11-23", "2026-11-20", "2026-11-19", "2026-11-18"]    # 최근 날짜부터
    for day in DAYS:
        assert ds.minute_status(store, day) == "complete"
    con = sqlite3.connect(ds.minute_path(store, "2026-11-23"))
    n = dict(con.execute("SELECT code, COUNT(*) FROM bar GROUP BY code").fetchall())
    assert n == {"000001": 392, "000002": 392, "000003": 392}          # 정규장 391 + 장후 거래 1, 거래량 0 봉 제외
    ts = {t for (t,) in con.execute("SELECT DISTINCT t FROM bar")}
    assert min(ts) == 540 and 945 in ts and 480 not in ts and 950 not in ts
    assert con.execute("SELECT value FROM file_meta WHERE key='n_codes'").fetchone()[0] == "3"
    old = sqlite3.connect(ds.minute_path(store, "2026-11-19"))
    assert {c for (c,) in old.execute("SELECT code FROM code_done")} == {"000001", "000002"}  # 상장 전 제외
    # 분봉이 완성되면 일봉 추정 거래대금이 분봉 기준으로 바뀜
    src = d.execute("SELECT value_est_src, value_est_krw FROM daily_bar WHERE code='000001' AND date='2026-11-23'").fetchone()
    assert src[0] == "minute" and src[1] == con.execute(
        "SELECT SUM(close*volume) FROM bar WHERE code='000001'").fetchone()[0]
    m.calls.clear()
    r = collect.minute_update(c, d, store, lambda x: None, NOW, history_days=30)
    assert r["days_done"] == [] and not [p for p, q in m.calls if p == "/api/v1/candles"]


def test_minute_update_time_limit_and_resume(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23")
    t = {"now": 0.0}

    def clock():
        t["now"] += 30     # 확인할 때마다 30초 흐름 → 3번째 종목 전에 1분 초과
        return t["now"]
    r = collect.minute_update(c, d, store, lambda x: None, NOW, history_days=30, max_minutes=1, clock=clock)
    assert "실행 시간 제한" in r["stop"]
    assert ds.minute_status(store, "2026-11-23") == "partial"
    con = sqlite3.connect(ds.minute_path(store, "2026-11-23"))
    assert con.execute("SELECT COUNT(*) FROM code_done").fetchone()[0] == 2
    con.close()
    r = collect.minute_update(c, d, store, lambda x: None, NOW, history_days=30)
    assert ds.minute_status(store, "2026-11-23") == "complete"
    con = sqlite3.connect(ds.minute_path(store, "2026-11-23"))
    assert con.execute("SELECT COUNT(*) FROM code_done").fetchone()[0] == 3


def test_minute_failures_given_up_after_retries(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23", fail={"000002"})
    c.retry_429 = 0
    for k in range(3):
        collect.minute_update(c, d, store, lambda x: None, NOW, history_days=30, max_retry=3, only_day="2026-11-23")
        assert ds.minute_status(store, "2026-11-23") == ("complete" if k == 2 else "partial")
    con = sqlite3.connect(ds.minute_path(store, "2026-11-23"))
    assert con.execute("SELECT value FROM file_meta WHERE key='n_given_up'").fetchone()[0] == "1"
    assert con.execute("SELECT fail_count FROM code_fail WHERE code='000002'").fetchone()[0] == 3


def test_status(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23")
    collect.daily_update(c, d, store, lambda x: None, NOW, history_days=30)
    collect.minute_update(c, d, store, lambda x: None, NOW, history_days=30, only_day="2026-11-23")
    logs = []
    s = collect.status(d, store, logs.append, NOW, history_days=30)
    assert s["last"] == "2026-11-23" and s["minute"] == {"complete": 1, "partial": 0, "missing": 3}
    assert any("일봉 2026-11-18~2026-11-23" in l for l in logs)


def _dump(store, day):
    con = sqlite3.connect(ds.minute_path(store, day))
    try:
        return (sorted(con.execute("SELECT * FROM bar")), sorted(r[0] for r in con.execute("SELECT code FROM code_done")))
    finally:
        con.close()


def test_backfill_same_result_as_daywise_with_fewer_calls(tmp_path):
    a_m, a_c, a_d, a_store = setup(tmp_path / "a", prev_day="2026-11-23", list_dates={"000003": "2026-11-20"})
    collect.minute_update(a_c, a_d, a_store, lambda x: None, NOW, history_days=30)
    daywise_calls = len([1 for p, q in a_m.calls if p == "/api/v1/candles"])

    b_m, b_c, b_d, b_store = setup(tmp_path / "b", prev_day="2026-11-23", list_dates={"000003": "2026-11-20"})
    logs = []
    r = collect.minute_backfill(b_c, b_d, b_store, logs.append, NOW, history_days=30)
    back_calls = len([1 for p, q in b_m.calls if p == "/api/v1/candles"])
    assert r["days_done"][0] == "2026-11-23"                       # 최근 날짜를 먼저 완성
    assert sorted(r["days_done"]) == DAYS
    for day in DAYS:
        assert ds.minute_status(b_store, day) == "complete"
        assert _dump(b_store, day) == _dump(a_store, day)           # 날짜 단위 결과와 똑같음
    assert back_calls < daywise_calls
    assert any("구간 1/1" in l for l in logs)


def test_backfill_resume_after_time_limit(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23")
    t = {"now": 0.0}

    def clock():
        t["now"] += 25
        return t["now"]
    r = collect.minute_backfill(c, d, store, lambda x: None, NOW, history_days=30, max_minutes=3, clock=clock)
    assert "실행 시간 제한" in r["stop"] and ds.minute_status(store, "2026-11-23") == "complete"
    assert ds.minute_status(store, "2026-11-18") != "complete"
    r = collect.minute_backfill(c, d, store, lambda x: None, NOW, history_days=30)
    assert all(ds.minute_status(store, day) == "complete" for day in DAYS)


def test_backfill_failure_counts_per_day(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23", fail={"000002"})
    c.retry_429 = 0
    for _ in range(3):
        collect.minute_backfill(c, d, store, lambda x: None, NOW, history_days=30, max_retry=3)
    assert all(ds.minute_status(store, day) == "complete" for day in DAYS)
    con = sqlite3.connect(ds.minute_path(store, "2026-11-18"))
    assert con.execute("SELECT fail_count FROM code_fail WHERE code='000002'").fetchone()[0] == 3
    assert con.execute("SELECT value FROM file_meta WHERE key='n_given_up'").fetchone()[0] == "1"


def test_backfill_chunks_complete_recent_first(tmp_path):
    m, c, d, store = setup(tmp_path, prev_day="2026-11-23")
    t = {"now": 0.0}

    def clock():
        t["now"] += 10
        return t["now"]
    logs = []
    # 구간 크기 1일: 23일(날짜 단위) → 20일 → 19일 → 18일 순서. 시간 제한으로 중간에 멈춤
    r = collect.minute_backfill(c, d, store, logs.append, NOW, history_days=30, chunk_days=1, max_minutes=3, clock=clock)
    assert r["days_done"][:2] == ["2026-11-23", "2026-11-20"]
    assert ds.minute_status(store, "2026-11-18") != "complete" and "실행 시간 제한" in r["stop"]
    assert any("구간 1/3: 2026-11-20~2026-11-20" in l for l in logs)
    r = collect.minute_backfill(c, d, store, lambda x: None, NOW, history_days=30, chunk_days=2)
    assert all(ds.minute_status(store, day) == "complete" for day in DAYS)
