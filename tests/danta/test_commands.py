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


def make_source(path, open_days=("20261119", "20261120"), universe_days=("20261120",), krx_base=True):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE trading_calendar(date TEXT PRIMARY KEY, is_open INTEGER, source TEXT, fetched_at TEXT, raw_file TEXT);
    CREATE TABLE price_daily(date TEXT, code TEXT, value_krw INTEGER, change_rate_pct REAL, PRIMARY KEY(date,code));
    CREATE TABLE universe_daily(date TEXT, code TEXT, in_universe INTEGER, PRIMARY KEY(date,code));
    CREATE TABLE krx_base_daily(date TEXT, code TEXT, name TEXT, secugrp TEXT, sect_tp TEXT, kind TEXT,
                                PRIMARY KEY(date,code));
    CREATE TABLE stock_master(code TEXT PRIMARY KEY, name TEXT, krx_secugrp TEXT, krx_kind TEXT, krx_sect_tp TEXT);
    """)
    con.executemany("INSERT INTO trading_calendar VALUES (?,?,'t','t',NULL)",
                    [(d, 1) for d in open_days] + [("20261121", 1), ("20251122", 0)])
    for d in open_days:
        rows = [(d, f"{i:06d}", 1000 * (200 - i), float(i)) for i in range(200)]  # 거래대금 상위=앞번호, 등락률 상위=뒷번호
        rows.append((d, "PREF01", 10 ** 9, 30.0))      # 우선주 (보통주 아님)
        rows.append((d, "NOINFO", 10 ** 9, 30.0))      # 종목정보 없음, 끝자리 0 아님
        rows.append((d, "999990", 10 ** 9, 300.0))      # 종목정보 없음, 끝자리 0 → 추정 포함
        rows.append((d, "999980", 1, -5.0))            # 종목정보 없음, 끝자리 0이지만 상위권 아님
        con.executemany("INSERT INTO price_daily VALUES (?,?,?,?)", rows)
        if krx_base:
            con.executemany("INSERT INTO krx_base_daily VALUES (?,?,?,?,?,?)",
                            [(d, f"{i:06d}", "회사", "주권", "", "보통주") for i in range(200)]
                            + [(d, "PREF01", "회사우", "주권", "", "구형우선주")])
    con.executemany("INSERT INTO stock_master VALUES (?,?,?,?,?)",
                    [(f"{i:06d}", "회사", "주권", "보통주", "") for i in range(200)]
                    + [("PREF01", "회사우", "주권", "구형우선주", "")])
    con.execute("INSERT INTO price_daily VALUES ('20261121','TODAY1',1000000000,30.0)")  # 오늘 → 기간 밖
    for d in universe_days:
        con.executemany("INSERT INTO universe_daily VALUES (?,?,?)",
                        [(d, f"{i:06d}", 1) for i in range(200)] + [(d, "PREF01", 0)])
    con.commit()
    con.close()


def _run_jobs(tmp_path, **kw):
    make_source(tmp_path / "m.sqlite", **kw)
    src = source.open_ro(tmp_path / "m.sqlite")
    con = store.connect(tmp_path / "d.sqlite")
    logs = []
    n = commands.cmd_jobs(src, con, logs.append, date(2026, 11, 21), 100, 50)
    days = {r[0]: r[1:] for r in con.execute("SELECT date, method, basis_date, note, n_codes FROM job_days")}
    return con, src, n, days, logs


def test_jobs_readonly_and_filters(tmp_path):
    con, src, n, days, logs = _run_jobs(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        src.execute("CREATE TABLE x(a)")                  # 읽기 전용
    codes = {r[0] for r in con.execute("SELECT code FROM jobs")}
    assert "PREF01" not in codes and "TODAY1" not in codes
    assert "000000" in codes and "000199" in codes
    assert days["20261120"][0] == "universe"
    # 2일 중 1일 빠짐(50%) → 그날 price_daily 보통주
    assert days["20261119"][0] == "price_common"
    note = days["20261119"][2]
    assert "krx_base_daily" in note
    assert "판정 불가 3종목 중 상위권 포함 2 / 끝자리 0: 999990 / 끝자리 0 아님: NOINFO" in note
    got = {r[0]: r[1:] for r in con.execute("SELECT code, reason, common_check FROM jobs WHERE date='20261119'")}
    assert got["999990"] == ("value+change+판정불가", "unknown_code0")
    assert got["NOINFO"] == ("value+판정불가(끝자리0아님)", "unknown_other")
    assert got["000000"][1] == "confirmed"
    assert "999980" not in got                       # 판정 불가지만 상위권 아님
    assert "PREF01" not in got                       # 정보로 우선주 확인 → 제외
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE date='20261120' AND common_check!='confirmed'"
                       ).fetchone()[0] == 0
    assert n == 299                                   # 20261120: 150, 20261119: 149 (999990이 두 목록에 중복)


def _open_days(n):
    from datetime import timedelta
    out, d = [], date(2025, 12, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def test_jobs_few_missing_uses_nearest_past_not_future(tmp_path):
    ds = _open_days(40)
    missing = {ds[0], ds[20]}                        # 2/40 = 5% → 과거 범위 사용
    con, src, n, days, logs = _run_jobs(tmp_path, open_days=ds, universe_days=[d for d in ds if d not in missing])
    assert days[ds[20]][:2] == ("past_universe", ds[19])   # 미래(ds[21])가 아닌 직전 과거
    assert days[ds[0]][0] == "price_common"               # 과거 범위가 없음
    assert "과거 universe 없음" in days[ds[0]][2]
    assert all(v[0] == "universe" for k, v in days.items() if k not in missing)


def test_jobs_many_missing_uses_price_common_with_master_fallback(tmp_path):
    ds = _open_days(40)
    missing = set(ds[10:13])                          # 3/40 = 7.5% → price_common
    con, src, n, days, logs = _run_jobs(tmp_path, open_days=ds, universe_days=[d for d in ds if d not in missing],
                                        krx_base=False)
    for d in missing:
        assert days[d][0] == "price_common" and "stock_master" in days[d][2] and days[d][3] == 149 and "999990" in days[d][2] and "NOINFO" in days[d][2]
    assert any("price_daily 보통주" in l for l in logs)


def test_jobs_overlap_dedup(tmp_path):
    make_source(tmp_path / "m.sqlite")
    src = source.open_ro(tmp_path / "m.sqlite")
    con = store.connect(tmp_path / "d.sqlite")
    commands.cmd_jobs(src, con, lambda m: None, date(2026, 11, 21), 150, 100)
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE reason='value+change' AND date='20261120'").fetchone()[0] == 50


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


def test_old_db_gets_common_check_column(tmp_path):
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE jobs(date TEXT NOT NULL, code TEXT NOT NULL, reason TEXT, PRIMARY KEY(date, code))")
    c.execute("INSERT INTO jobs VALUES ('20261120','005930','value')")
    c.commit(); c.close()
    con = store.connect(p)
    assert con.execute("SELECT common_check FROM jobs").fetchone()[0] == "confirmed"
