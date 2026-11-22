from datetime import datetime

from collector.timeutil import KST
from danta import commands, source, store

from .fake import FakeMinuteServer, day_times
from .test_commands import Client, make_source

DAYS = ("20261118", "20261119", "20261120", "20261124")


def now():
    return datetime(2026, 11, 24, 21, 30, tzinfo=KST)


def setup(tmp_path, open_days=DAYS, theme=True):
    make_source(tmp_path / "m.sqlite", open_days=open_days, universe_days=open_days)
    src = source.open_ro(tmp_path / "m.sqlite")
    con = store.connect(tmp_path / "d.sqlite")
    tf = tmp_path / "themes" / "theme_master.csv"
    if theme:
        tf.parent.mkdir()
        tf.write_text("theme,code,name,basis,added_date,note\n반도체,5930,삼성전자,,2026-01-01,\n"
                      "반도체,000660,하이닉스,,,\n잘못,ABC,x,,,\n", encoding="utf-8-sig")
    srv = FakeMinuteServer({d: day_times() for d in open_days})
    return src, con, tf, srv


def run(src, con, tf, client, lock=None, alive=lambda pid: False, sleeps=None, **kw):
    logs = []
    r = commands.cmd_daily(client, src, con, logs.append, now, tf, lock or tf.parent / "none.lock",
                           sleep=(sleeps.append if sleeps is not None else lambda s: None), alive=alive, **kw)
    return r, logs


def test_only_unplanned_dates(tmp_path):
    src, con, tf, srv = setup(tmp_path)
    # 1118, 1119는 이미 계획·수집된 날짜
    with con:
        con.executemany("INSERT INTO job_days VALUES (?, 'universe', NULL, NULL, 1)", [("20261118",), ("20261119",)])
        con.executemany("INSERT INTO jobs VALUES (?, '000000', 'value', 'confirmed')", [("20261118",), ("20261119",)])
        con.executemany("INSERT INTO done VALUES ('000000', ?, 391, 4, 'x')", [("20261118",), ("20261119",)])
    client = Client(srv)
    r, logs = run(src, con, tf, client)
    assert r["status"] == "done"
    assert r["new_days"] == ["20261120", "20261121", "20261124"]   # 1121: 달력상 개장, 가격 있음
    called = {c["FID_INPUT_DATE_1"] for c in srv.calls}
    assert "20261118" not in called and "20261119" not in called
    assert {"20261120", "20261124"} <= called
    assert store.pending_jobs(con) == []
    assert r["range"] == ("20261118", "20261124")
    assert any("DB 분봉 날짜 범위" in l for l in logs)
    # 다시 실행하면 새 날짜 없음, 호출 0
    srv.calls.clear()
    r, logs = run(src, con, tf, client)
    assert r["new_days"] == [] and srv.calls == []


def test_waits_when_today_daily_missing(tmp_path):
    src, con, tf, srv = setup(tmp_path, open_days=("20261120",))
    # 오늘(1124)이 달력에 없음
    client = Client(srv)
    r, logs = run(src, con, tf, client)
    assert r["status"] == "waiting_data" and client.stats.calls == 0
    assert any("기존 수집기 데이터 대기" in l for l in logs)


def test_waits_when_today_open_but_no_price(tmp_path):
    src, con, tf, srv = setup(tmp_path, open_days=("20261120",))
    import sqlite3
    w = sqlite3.connect(tmp_path / "m.sqlite")
    w.execute("INSERT INTO trading_calendar VALUES ('20261124', 1, 't', 't', NULL)")
    w.commit(); w.close()
    client = Client(srv)
    r, logs = run(src, con, tf, client)
    assert r["status"] == "waiting_data" and client.stats.calls == 0
    assert any("일봉이 기존 DB에 아직 없음" in l for l in logs)


def test_failed_job_retried_then_given_up_after_3(tmp_path):
    src, con, tf, srv = setup(tmp_path)
    client = Client(srv, fail_codes={"000000"})
    for _ in range(3):
        r, logs = run(src, con, tf, client)
    gave = store.given_up(con, 3)
    assert {c for _, c, _, _ in gave} == {"000000"}
    assert all(n == 3 for _, _, n, _ in gave)
    assert store.pending_jobs(con, 3) == []              # 3회 실패 건은 더 시도하지 않음
    assert any("재시도 중단(연속 3회 실패)" in l and "건:" in l for l in logs)
    # 4번째 실행에서는 000000을 호출하지 않음
    client.stats.calls = 0
    run(src, con, tf, client)
    assert client.stats.calls == 0


def test_success_clears_fail_count(tmp_path):
    src, con, tf, srv = setup(tmp_path, open_days=("20261124",))
    run(src, con, tf, Client(srv, fail_codes={"000000"}))
    assert con.execute("SELECT fail_count FROM fail_log WHERE code='000000'").fetchone()[0] == 1
    run(src, con, tf, Client(srv))
    assert con.execute("SELECT COUNT(*) FROM fail_log").fetchone()[0] == 0


def test_theme_snapshot_once_per_day(tmp_path):
    src, con, tf, srv = setup(tmp_path)
    r, logs = run(src, con, tf, Client(srv))
    rows = con.execute("SELECT date, code, theme, added_date FROM theme_snapshot ORDER BY code").fetchall()
    assert rows == [("20261124", "000660", "반도체", None), ("20261124", "005930", "반도체", "2026-01-01")]
    tf.write_text("theme,code\n2차전지,373220\n", encoding="utf-8-sig")   # 같은 날 파일이 바뀌어도
    r, logs = run(src, con, tf, Client(srv))
    assert con.execute("SELECT COUNT(*) FROM theme_snapshot").fetchone()[0] == 2   # 덮어쓰지 않음
    assert any("이미 있어 건너뜀" in l for l in logs)


def test_theme_file_missing_is_skipped(tmp_path):
    src, con, tf, srv = setup(tmp_path, theme=False)
    r, logs = run(src, con, tf, Client(srv))
    assert r["status"] == "done" and any("테마 파일 없음" in l for l in logs)


def test_waits_for_running_collector(tmp_path):
    src, con, tf, srv = setup(tmp_path)
    lock = tmp_path / "collector.lock"
    lock.write_text("4321")
    sleeps = []
    client = Client(srv)
    r, logs = run(src, con, tf, client, lock=lock, alive=lambda pid: True, sleeps=sleeps, wait_minutes=60)
    assert r["status"] == "collector_running" and client.stats.calls == 0
    assert sum(sleeps) == 3600
    # 10분 뒤 끝나는 경우
    state = {"n": 0}

    def alive(pid):
        state["n"] += 1
        return state["n"] <= 10
    sleeps.clear()
    r, logs = run(src, con, tf, client, lock=lock, alive=alive, sleeps=sleeps)
    assert r["status"] == "done" and sum(sleeps) == 600
    assert lock.read_text() == "4321"                    # 잠금 파일은 건드리지 않음
