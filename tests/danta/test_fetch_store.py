from danta import store
from danta.fetch import fetch_day, minus_one_minute, minute_values

from .fake import FakeClient, FakeMinuteServer, day_times


def test_minus_one_minute():
    assert minus_one_minute("100000") == "095900"
    assert minus_one_minute("153000") == "152900"


def test_full_day_paging_and_other_dates_excluded():
    srv = FakeMinuteServer({"20261119": day_times(), "20261120": day_times()})
    res = fetch_day(FakeClient(srv), "005930", "20261120")
    assert len(res.bars) == 391                     # 09:00~15:30
    assert res.bars[0]["time"] == "090000" and res.bars[-1]["time"] == "153000"
    assert res.calls == 4                           # 120*3=360 → 4번째에서 090000 도달
    assert res.other_date_rows == 120 - 31          # 마지막 페이지의 전날 행은 버림
    assert all(b["time"] <= "153000" for b in res.bars)
    assert [c["FID_INPUT_HOUR_1"] for c in srv.calls] == ["153000", "133000", "113000", "093000"]
    assert srv.calls[0]["FID_FAKE_TICK_INCU_YN"] == ""


def test_stops_when_previous_date_rows_appear():
    # 그날 장이 10:00에 시작(090000 미도달) → 마지막 페이지에 전날 행이 섞임 → 버리고 종료
    srv = FakeMinuteServer({"20261119": day_times(), "20261120": day_times("100000")})
    res = fetch_day(FakeClient(srv), "005930", "20261120")
    assert len(res.bars) == 331
    assert all(b["time"] >= "100000" for b in res.bars)
    assert res.other_date_rows > 0
    assert res.calls == 3


def test_no_rows_for_date():
    srv = FakeMinuteServer({"20261119": day_times()})
    res = fetch_day(FakeClient(srv), "005930", "20261121")  # 휴장일: 전날 행만 옴
    assert res.bars == [] and res.calls == 1


def test_minute_values_from_cumulative():
    bars = [{"acml_value_krw": a} for a in (500, 800, 800, None, 1500, 1200)]
    assert minute_values(bars) == [500, 300, 0, None, 700, None]


def test_save_and_resume(tmp_path):
    con = store.connect(tmp_path / "d.sqlite")
    with con:
        con.executemany("INSERT INTO jobs(date,code,reason) VALUES (?,?,?)",
                        [("20261120", "005930", "value"), ("20261119", "000660", "change")])
    srv = FakeMinuteServer({"20261120": day_times()})
    res = fetch_day(FakeClient(srv), "005930", "20261120")
    store.save_day(con, res, "2026-11-21T10:00:00", compute_value=True)
    assert store.pending_jobs(con) == [("20261119", "000660")]
    total = con.execute("SELECT SUM(value_krw), MAX(acml_value_krw) FROM minute_bar").fetchone()
    assert total[0] == total[1] == 391 * 1000
    store.save_day(con, res, "2026-11-21T10:00:00", compute_value=False)  # 다시 저장해도 중복 없음
    assert con.execute("SELECT COUNT(*), COUNT(value_krw) FROM minute_bar").fetchone() == (391, 0)
    assert store.recalc_values(con) == 1
    assert con.execute("SELECT COUNT(value_krw) FROM minute_bar").fetchone()[0] == 391
