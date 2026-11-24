import sqlite3
from datetime import datetime

from collector.timeutil import KST
from toss import commands, store
from toss.candles import best_alignment, page_candles, session_counts
from toss.client import redact

from .fake import FakeToss, minute_bars

NOW = lambda: datetime(2026, 11, 24, 10, 0, tzinfo=KST)


def test_next_before_paging(tmp_path):
    srv = FakeToss(minute_bars("20261120") + minute_bars("20261119"))
    c, _ = srv.client(tmp_path)
    r = page_candles(c, "/api/v1/candles", {"symbol": "005930", "interval": "1m", "count": 200}, 100)
    assert r.n_bars == 2 * 391 and r.calls == 4 and "nextBefore=null" in r.stop
    assert r.dates == {"2026-11-19", "2026-11-20"}
    befores = [p.get("before") for m, path, p in srv.calls if path == "/api/v1/candles"]
    assert befores[0] is None and all(befores[1:])
    assert r.oldest.strftime("%Y%m%d %H%M") == "20261119 0901"   # 첫 봉 종료 시각


def test_paging_stops_at_call_cap_and_period(tmp_path):
    srv = FakeToss(minute_bars("20261120") + minute_bars("20261119"))
    c, _ = srv.client(tmp_path)
    r = page_candles(c, "/api/v1/candles", {"symbol": "A", "interval": "1m", "count": 100}, 3)
    assert r.calls == 3 and "호출 상한" in r.stop
    limit = datetime(2026, 11, 20, 12, 0, tzinfo=KST)
    r = page_candles(c, "/api/v1/candles", {"symbol": "A", "interval": "1m", "count": 100}, 100,
                     stop_when=lambda t: t < limit)
    assert "기간 상한" in r.stop and r.calls == 3


def test_alignment_detects_end_time_shift():
    toss = minute_bars("20261120")        # 종료 시각: 09:01 봉 = KIS 090000 봉
    kis = [(f"{(9 + (i // 60)):02d}{i % 60:02d}00", 100 + i + 1, 101 + i + 1, 99 + i + 1, 100 + i + 1, 10)
           for i in range(0, 391)]
    best, other = best_alignment(toss, kis)
    assert best["shift_min"] == 1 and best["match_rate"]["close"] == 1.0 and best["matched"] == 391
    assert other["match_rate"]["close"] < 0.1
    assert best["volume_diff_pct"] == 0.0


def test_session_counts_nxt():
    bars = minute_bars("20261120", extra=("08:10", "08:59", "15:45", "19:59"))
    s = session_counts(bars)
    assert s == {"pre_0800_0900": 2, "regular_0900_1530": 391, "after_1531_2000": 2, "other": 0}


def make_danta(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE jobs(date TEXT, code TEXT, reason TEXT, common_check TEXT);
    CREATE TABLE minute_bar(code TEXT, date TEXT, time TEXT, open INTEGER, high INTEGER, low INTEGER,
        close INTEGER, volume INTEGER, acml_value_krw INTEGER, value_krw INTEGER);
    """)
    for k, code in enumerate(["A", "B", "C", "D"]):
        con.execute("INSERT INTO jobs VALUES ('20261120', ?, 'value', 'confirmed')", (code,))
        for i in range(391):
            t = f"{(9 + (i // 60)):02d}{i % 60:02d}00"
            con.execute("INSERT INTO minute_bar VALUES (?, '20261120', ?, ?, ?, ?, ?, 10, ?, NULL)",
                        (code, t, 101 + i, 102 + i, 100 + i, 101 + i, (k + 1) * 1000 * (i + 1)))
    con.commit()
    con.close()


def test_candles_compare(tmp_path):
    make_danta(tmp_path / "danta.sqlite")
    from danta.source import open_ro
    danta = open_ro(tmp_path / "danta.sqlite")
    srv = FakeToss(minute_bars("20261120", start="09:00", end="15:30", extra=("08:30",)) + minute_bars("20261119"))
    c, _ = srv.client(tmp_path)
    con = store.connect(tmp_path / "t.sqlite")
    logs = []
    out = commands.cmd_candles_compare(c, con, danta, logs.append, NOW, "20261120", n=4)
    assert [s["code"] for s in out["symbols"]] == ["D", "C", "A", "B"]     # 상위 2 + 하위 2
    assert out["chosen_shift"] == 1 and out["overall_close_rate"] == 1.0
    assert out["symbols_with_nxt_hours"] == 4
    assert all(s["toss_bars_all"] == 392 for s in out["symbols"])           # 11-19 봉은 빠짐
    assert any("shift 1" in l for l in logs)
    assert store.get_result(con, "candles_compare")[0]["chosen_shift"] == 1


def test_auth_masks_account(tmp_path):
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    con = store.connect(tmp_path / "t.sqlite")
    logs = []
    commands.cmd_auth(c, con, logs.append, NOW)
    text = redact("\n".join(logs))
    assert "12345678901" not in text and "accountSeq (가림)" in text
    assert "12345678901" not in redact("계좌 12345678901")


def test_judge():
    depth = {"m1": {"trading_days": 45}}
    assert commands.judge(depth, {"overall_close_rate": 0.995})[0] == "대체 가능"
    v, why = commands.judge({"m1": {"trading_days": 20}}, {"overall_close_rate": 0.995})
    assert v == "대체 불가" and "미달" in why[0]
    assert commands.judge(depth, {"overall_close_rate": 0.98})[0] == "대체 불가"
    assert commands.judge(None, None)[0] == "판정 불가"


def test_report_writes_markdown(tmp_path):
    con = store.connect(tmp_path / "t.sqlite")
    store.put_result(con, "candles_depth", {"symbol": "005930", "adjusted": [],
                     "m1": {"oldest": "2026-01-02", "trading_days": 200, "calls": 900, "stop": "끝"},
                     "d1": {"oldest": "2000-01-04", "trading_days": 6000, "calls": 30, "stop": "끝"}}, "t")
    store.put_result(con, "candles_compare", {"date": "20261120", "overall_close_rate": 0.999, "chosen_shift": 1,
                                              "symbols_with_nxt_hours": 0, "symbols": []}, "t")
    logs = []
    p = commands.cmd_report(con, logs.append, NOW, tmp_path)
    t = p.read_text(encoding="utf-8")
    assert "대체 가능" in t and "200일" in t
