from datetime import datetime

from collector.timeutil import KST
from toss import commands, store
from toss.candles import EXPECTED_REG, check_day

from .fake import FakeToss, minute_bars

NOW = lambda: datetime(2026, 11, 24, 21, 0, tzinfo=KST)


def day_with_nxt(day="20261120"):
    """정규장 391봉 + 장전·장후 거래량 0인 채움 봉."""
    bars = minute_bars(day, extra=("08:00", "08:30", "15:31", "19:59"))
    for b in bars:
        if b["closePrice"] == "1":
            b.update(openPrice="491", highPrice="491", lowPrice="491", closePrice="491", volume="0")
    return bars


def daily_for(bars, day="20261120"):
    reg = sorted((b for b in bars if b["volume"] != "0"), key=lambda b: b["timestamp"])
    return {"timestamp": f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+09:00", "openPrice": reg[0]["openPrice"],
            "highPrice": str(max(int(b["highPrice"]) for b in reg)), "lowPrice": str(min(int(b["lowPrice"]) for b in reg)),
            "closePrice": reg[-1]["closePrice"], "volume": str(10 * len(reg)), "currency": "KRW"}


def test_expected_regular_minutes():
    assert len(EXPECTED_REG) == 381 and "1519" in EXPECTED_REG and "1530" in EXPECTED_REG
    assert "1525" not in EXPECTED_REG


def test_check_day_complete_with_filler_bars():
    bars = day_with_nxt()
    c = check_day(bars, daily_for(bars))
    assert c["regular_missing"] == 0 and c["bars"] == 395
    assert c["zero_volume_bars"] == {"pre": 2, "regular": 0, "after": 2}
    assert all(c["match_regular"].values()) and all(c["match_all_sessions"].values())
    assert c["volume"]["regular_vs_daily_pct"] == 0.0 and c["first_start"] == "0800"


def test_check_day_missing_minute_and_close_mismatch():
    bars = [b for b in day_with_nxt() if not b["timestamp"].startswith("2026-11-20T10:31")]  # 10:30 봉 누락
    d = daily_for(bars)
    d["closePrice"] = "999"
    c = check_day(bars, d)
    assert c["regular_missing"] == 1 and c["missing_examples"] == ["1030"]
    assert c["match_regular"]["close"] is False and c["match_regular"]["open"] is True


def test_check_day_without_daily():
    c = check_day(day_with_nxt(), None)
    assert c["daily"] is None and c["match_regular"] is None


def test_cmd_candles_check(tmp_path):
    bars = day_with_nxt() + minute_bars("20261119")
    srv = FakeToss(bars, daily=[daily_for(day_with_nxt())],
                   rankings=[{"symbol": s} for s in ["A", "B", "C", "D", "E", "F"]])
    c, _ = srv.client(tmp_path)
    con = store.connect(tmp_path / "t.sqlite")
    logs = []
    out = commands.cmd_candles_check(c, con, logs.append, NOW, "20261120", n=4)
    assert [x["symbol"] for x in out["symbols"]] == ["A", "B", "E", "F"]     # 상위 2 + 100위 쪽 2
    assert out["regular_completeness"] == 1.0 and out["close_match_regular"] == 4 and out["daily_checked"] == 4
    assert any("KIS 데이터는 쓰지 않음" in l for l in logs)
    assert store.get_result(con, "candles_check")[0]["close_match_regular"] == 4
    out2 = commands.cmd_candles_check(c, con, lambda m: None, NOW, "20261120", symbols=["005930"])
    assert [x["symbol"] for x in out2["symbols"]] == ["005930"]


def test_judge_with_check():
    depth = {"m1": {"trading_days": 729}}
    ok = {"regular_completeness": 1.0, "daily_checked": 10, "close_match_regular": 10}
    v, why = commands.judge(depth, None, ok)
    assert v == "대체 가능 (자체 점검 기준)" and any("KIS 분봉과 같은 값인지는 비교하지 않았" in w for w in why)
    assert commands.judge(depth, None, dict(ok, regular_completeness=0.95))[0] == "대체 불가"
    assert commands.judge(depth, None, dict(ok, close_match_regular=8))[0] == "대체 불가"
    assert commands.judge(depth, None, None)[0] == "판정 불가"
    # KIS 비교가 있으면 그것이 우선
    assert commands.judge(depth, {"overall_close_rate": 0.5}, ok)[0] == "대체 불가"


def test_adjusted_compare_ignores_forming_bar(tmp_path):
    a = minute_bars("20261120")[:200]
    b = [dict(x) for x in a]
    b[0]["closePrice"] = "77777"            # 가장 최근 봉만 두 조회 사이에 바뀜
    srv = FakeToss()
    c, _ = srv.client(tmp_path)
    c.token()
    srv.valid = c._token
    srv.script = [(200, {"result": {"candles": a, "nextBefore": None}}, {}),
                  (200, {"result": {"candles": b, "nextBefore": None}}, {})]
    out = commands._adjusted_compare(c, "005930", "1m", lambda m: None)
    assert out["different"] == 0 and out["common"] == 199


def test_report_uses_check(tmp_path):
    con = store.connect(tmp_path / "t.sqlite")
    store.put_result(con, "candles_depth", {"symbol": "005930", "adjusted": [],
                     "m1": {"oldest": "2023-11-24", "trading_days": 729, "calls": 2612, "stop": "기간 상한"},
                     "d1": {"oldest": "2023-08-09", "trading_days": 800, "calls": 4, "stop": "기간 상한"}}, "t")
    bars = day_with_nxt()
    chk = check_day(bars, daily_for(bars))
    chk["symbol"] = "A"
    store.put_result(con, "candles_check", {"date": "20261120", "symbols": [chk], "regular_completeness": 1.0,
                                            "daily_checked": 1, "close_match_regular": 1, "close_match_all": 1}, "t")
    p = commands.cmd_report(con, lambda m: None, NOW, tmp_path)
    t = p.read_text(encoding="utf-8")
    assert "대체 가능 (자체 점검 기준)" in t and "분봉 자체 점검" in t


def test_check_defaults_to_previous_business_day(tmp_path):
    bars = day_with_nxt() + minute_bars("20261119")
    srv = FakeToss(bars, daily=[daily_for(day_with_nxt())], rankings=[{"symbol": "A"}])
    c, _ = srv.client(tmp_path)
    c.token()
    srv.valid = c._token
    srv.script = [(200, {"result": {"previousBusinessDay": {"date": "2026-11-20"}}}, {})]
    logs = []
    out = commands.cmd_candles_check(c, store.connect(tmp_path / "t.sqlite"), logs.append, NOW, None, ["A"])
    assert out["date"] == "20261120" and any("전 영업일 20261120" in l for l in logs)
