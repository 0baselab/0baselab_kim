import sqlite3
from datetime import date, datetime

import pytest

from collector.timeutil import KST
from danta import commands, source, store
from danta.kis import KisApiError, RateLimitError

from .fake import FakeClient, FakeMinuteServer, day_times


class Stats:
    calls = 0
    rate_limited = 0


class Limiter:
    per_sec = 10


class Client(FakeClient):
    def __init__(self, server, fail_codes=()):
        super().__init__(server)
        self.stats, self.limiter, self.fail_codes = Stats(), Limiter(), set(fail_codes)

    def minute(self, params, retry_rate_limit=True):
        self.stats.calls += 1
        if params["FID_INPUT_ISCD"] in self.fail_codes:
            raise KisApiError("가짜 오류")
        return super().minute(params)


def now():
    return datetime(2026, 11, 21, 10, 0, tzinfo=KST)


def _jobs(con, pairs):
    with con:
        con.executemany("INSERT INTO jobs(date,code,reason) VALUES (?,?,'v')", pairs)


def test_backfill_oldest_first_and_resume(tmp_path):
    con = store.connect(tmp_path / "d.sqlite")
    _jobs(con, [("20261120", "A"), ("20261119", "B"), ("20261119", "A")])
    srv = FakeMinuteServer({"20261119": day_times(), "20261120": day_times()})
    logs = []
    r = commands.cmd_backfill(Client(srv), con, logs.append, now, max_calls=4)
    assert r["done"] == 1 and "호출 상한" in r["stop"]
    first = con.execute("SELECT code, date FROM done").fetchall()
    assert first == [("A", "20261119")]
    r = commands.cmd_backfill(Client(srv), con, logs.append, now)
    assert r["done"] == 2 and store.pending_jobs(con) == []
    assert any("남은 예상" in l for l in logs)


def test_backfill_stops_after_failures(tmp_path):
    con = store.connect(tmp_path / "d.sqlite")
    _jobs(con, [("20261120", f"X{i:02d}") for i in range(30)])
    srv = FakeMinuteServer({"20261120": day_times()})
    r = commands.cmd_backfill(Client(srv, {f"X{i:02d}" for i in range(30)}), con, lambda m: None, now,
                              max_failures=20)
    assert r["fails"] == 20 and "실패 20건" in r["stop"]
    assert store.pending_jobs(con).__len__() == 30   # 실패는 완료로 기록하지 않음


def make_source(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE trading_calendar(date TEXT PRIMARY KEY, is_open INTEGER, source TEXT, fetched_at TEXT, raw_file TEXT);
    CREATE TABLE price_daily(date TEXT, code TEXT, value_krw INTEGER, change_rate_pct REAL, PRIMARY KEY(date,code));
    CREATE TABLE universe_daily(date TEXT, code TEXT, in_universe INTEGER, PRIMARY KEY(date,code));
    """)
    con.executemany("INSERT INTO trading_calendar VALUES (?,?,'t','t',NULL)",
                    [("20251120", 1), ("20251121", 0), ("20261119", 1), ("20261120", 1), ("20261121", 1)])
    rows = []
    for i in range(200):
        c = f"{i:06d}"
        rows.append(("20261120", c, 1000 * (200 - i), float(i)))  # 거래대금 상위=앞번호, 등락률 상위=뒷번호
    rows.append(("20261120", "PREF01", 10 ** 9, 30.0))           # 보통주 범위 밖
    rows.append(("20261121", "TODAY1", 10 ** 9, 30.0))           # 오늘 → 기간 밖
    con.executemany("INSERT INTO price_daily VALUES (?,?,?,?)", rows)
    con.executemany("INSERT INTO universe_daily VALUES (?,?,?)",
                    [("20261120", f"{i:06d}", 1) for i in range(200)] + [("20261120", "PREF01", 0)])
    con.commit()
    con.close()


def test_jobs_from_readonly_source(tmp_path):
    make_source(tmp_path / "market.sqlite")
    src = source.open_ro(tmp_path / "market.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        src.execute("CREATE TABLE x(a)")                  # 읽기 전용
    con = store.connect(tmp_path / "d.sqlite")
    logs = []
    n = commands.cmd_jobs(src, con, logs.append, date(2026, 11, 21), 100, 50)
    assert n == 150
    rows = con.execute("SELECT date, code, reason FROM jobs").fetchall()
    assert {r[0] for r in rows} == {"20261120"}
    codes = {r[1] for r in rows}
    assert "PREF01" not in codes and "TODAY1" not in codes
    assert "000000" in codes and "000199" in codes
    assert any("건너뛴 날짜" in l for l in logs)           # 20251120, 20261119: universe 없음


def test_jobs_overlap_dedup(tmp_path):
    make_source(tmp_path / "m.sqlite")
    src = source.open_ro(tmp_path / "m.sqlite")
    con = store.connect(tmp_path / "d.sqlite")
    assert commands.cmd_jobs(src, con, lambda m: None, date(2026, 11, 21), 150, 100) == 200
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE reason='value+change'").fetchone()[0] == 50


def test_probe_reports_sum_vs_daily(tmp_path):
    make_source(tmp_path / "m.sqlite")
    src = source.open_ro(tmp_path / "m.sqlite")
    srv = FakeMinuteServer({"20261120": day_times()})
    srv_code = "000000"   # 일봉 value_krw = 200,000
    logs = []
    out = commands.cmd_probe(Client(srv), src, logs.append, date(2026, 11, 21), srv_code, oldest=False)
    assert out["day"] == "20261120" and out["bars"] == 391 and out["monotonic"]
    assert out["sum_min"] == 391_000 and out["daily_value"] == 200_000
    assert any("차이" in l for l in logs) and any("1:390" in l for l in logs)


def test_find_oldest_skips_holidays():
    days = {}
    from datetime import timedelta
    d = date(2025, 11, 25)
    while d <= date(2026, 11, 20):
        if d.weekday() < 5 and not (date(2026, 2, 16) <= d <= date(2026, 2, 18)):
            days[d.strftime("%Y%m%d")] = ["153000"]
        d += timedelta(days=1)
    srv = FakeMinuteServer(days)
    got = commands.find_oldest(Client(srv), "005930", "20261120", lambda m: None)
    assert got == "20251125"
