import math
import sqlite3
import zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from collector import backfill as B
from collector import db
from collector.bt import config as C
from collector.bt import engine as E
from collector.bt.data import Panel, coverage, load_panel, open_ro
from collector.timeutil import KST

NOW = datetime(2026, 11, 26, 21, 0, tzinfo=KST)


def weekdays(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


DAYS = weekdays(date(2015, 1, 1), 330)


def make_db(tmp_path, investor_codes=("000010", "000020", "000030"), dart=True):
    con = B.open_bt(tmp_path)
    stocks = {"000010": ("상승A", "KOSPI"), "000020": ("상승B", "KOSDAQ"), "000030": ("폐지C", "KOSPI"),
              "000040": ("횡보D", "KOSPI")}
    for code, (name, mk) in stocks.items():
        last = DAYS[300] if code == "000030" else DAYS[-1]
        con.execute("INSERT INTO bt_stock VALUES(?,?,?,?,?,1,'test',1)", (code, name, mk, DAYS[0], last))
    for i, d in enumerate(DAYS):
        for code, (name, mk) in stocks.items():
            if code == "000030" and i > 300:
                continue  # 상장폐지
            px = 1000 + i * 5 if code != "000040" else 1000 + (i % 10)
            con.execute("INSERT INTO bt_daily VALUES(?,?,?,?,NULL,?,?,?,?,0,0,1000,?,?,1000,1.0)",
                        (d, code, mk, name, px - 2, px + 3, px - 6, px, 10_000_000_000, 500_000_000_000))
            if code in investor_codes:
                con.execute("INSERT INTO bt_investor VALUES(?,?,0,0,0,?,?,0)", (d, code, 50 if code == "000010" else 10, 10))
        for mk, nm in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            con.execute("INSERT INTO bt_index VALUES(?,?,?,NULL,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL)", (d, mk, nm, 2000 + i))
    if dart:
        con.execute("INSERT INTO dart_corp VALUES('C10','000010','상승A','20260101','t')")
        for k, (y, q, rd) in enumerate([("2014", 1, "20150515"), ("2014", 2, "20150814"), ("2014", 3, "20151114"),
                                         ("2014", 4, "20160310")]):
            con.execute("INSERT INTO dart_fs_quarter(corp_code, fiscal_year, quarter, fs_div, op_income_krw, liabilities_krw,"
                        " equity_krw, rcept_dt) VALUES('C10',?,?,'CFS',100,50,100,?)", (y, q, rd))
    con.commit()
    return con


def cfg(**over):
    base = C.Config({k: v for k, v, _ in C.DEFAULTS})
    return base.copy(START="20150101", **over)


def tiny_panel(o, h, l, c, value=None, mktcap=None):
    T, N = len(c), len(c[0])
    a = lambda x: np.array(x, dtype=np.float64)
    return Panel([f"2020{1+i//28:02d}{1+i%28:02d}" for i in range(T)], [f"00{j}0{j}0" for j in range(N)],
                 np.array(["KOSPI"] * N), ["s"] * N, a(o), a(h), a(l), a(c),
                 a(value or [[1e10] * N] * T), a(mktcap or [[1e12] * N] * T), np.full((T, N), np.nan),
                 np.full((T, N, 3), np.nan), {"KOSPI": np.full(T, 1.0), "KOSDAQ": np.full(T, 1.0)},
                 np.array([T - 1] * N), {}, True)


def tiny_ind(p, ma20=None, ma60=None):
    T, N = p.close.shape
    z = np.full((T, N), np.nan)
    return E.Indicators(np.full((T, N), ma20 if ma20 is not None else np.nan), np.full((T, N), ma60 or 0.0), z, z, z,
                        np.full((T, N), 1e9), np.full((T, N), 10.0), z, z, z, {})


def test_limit_fill_rule_and_costs():
    # 신호일 t=0, 다음 날 시가 105, 저가 95 -> 지정가 100 체결, 체결가 = min(100, 105) = 100
    p = tiny_panel(o=[[100], [105], [110], [120]], h=[[100], [106], [111], [121]], l=[[100], [95], [109], [119]],
                   c=[[100], [104], [110], [120]])
    ind = tiny_ind(p, ma20=100.0)
    sig = np.array([[True], [False], [False], [False]])
    c = cfg(MAX_POSITIONS="1")
    R = E.simulate(p, ind, c, "A", sig, np.ones((4, 1)), "2", "ga", 15, 20, 0)
    assert R.limit_orders == 1 and R.limit_fills == 1
    cash0 = 10_000_000
    unit = 100 * 1.001 * 1.00015
    shares = int(cash0 // unit)
    cost = shares * unit
    # 마지막 날 자산 = 남은 현금 + 보유 x 종가
    assert math.isclose(R.equity[-1][1], cash0 - cost + shares * 120, rel_tol=1e-9)
    # 시가가 지정가보다 낮으면 시가 체결
    p2 = tiny_panel(o=[[100], [90]], h=[[100], [92]], l=[[100], [88]], c=[[100], [91]])
    R2 = E.simulate(p2, tiny_ind(p2, ma20=100.0), c, "A", np.array([[True], [False]]), np.ones((2, 1)), "2", "ga", 15, 20, 0)
    assert math.isclose(R2.equity[-1][1], cash0 - int(cash0 // (90 * 1.001 * 1.00015)) * 90 * 1.001 * 1.00015
                        + int(cash0 // (90 * 1.001 * 1.00015)) * 91, rel_tol=1e-9)


def test_sell_costs_include_tax_by_year():
    assert C.tax_pct("20201231") == 0.25 and C.tax_pct("20250102") == 0.15 and C.tax_pct("20260105") == 0.20
    # 다음 날 시가 매수 -> 종가 하락 -15% 이하 -> 다음 날 시가 매도
    p = tiny_panel(o=[[100], [100], [100], [80]], h=[[100], [101], [101], [81]], l=[[100], [99], [80], [79]],
                   c=[[100], [100], [84], [80]])
    R = E.simulate(p, tiny_ind(p), cfg(MAX_POSITIONS="1"), "A", np.array([[True], [False], [False], [False]]),
                   np.ones((4, 1)), "1", "ga", 15, 20, 0)
    t = R.trades[0]
    buy_unit = 100 * 1.001 * 1.00015
    shares = int(10_000_000 // buy_unit)
    proceeds = shares * 80 * 0.999
    net = proceeds - proceeds * (0.00015 + C.tax_pct(p.dates[3]) / 100)
    assert t["reason"].startswith("최고 종가 대비") and t["pnl_krw"] == round(net - shares * buy_unit)
    # (나) 장중 손절: 저가가 손절가 이하면 손절가 체결
    R2 = E.simulate(p, tiny_ind(p), cfg(MAX_POSITIONS="1"), "A", np.array([[True], [False], [False], [False]]),
                    np.ones((4, 1)), "1", "na", 15, 20, 0)
    assert R2.trades[0]["reason"] == "장중 손절" and R2.trades[0]["exit_date"] == p.dates[2]
    assert math.isclose(R2.trades[0]["exit_price"], 85 * 0.999, abs_tol=0.01)


def test_future_info_blocked_and_survivorship(tmp_path):
    con = make_db(tmp_path)
    p = load_panel(con, "20150101")
    j = p.codes.index("000010")
    # 4번째 분기 공시일 20160310 -> 그 날까지는 없음, 다음 거래일부터 있음
    i_rcept = p.dates.index("20160310")
    assert np.isnan(p.fin_ok[i_rcept, j, 0]) and p.fin_ok[i_rcept + 1, j, 0] == 400
    # 상장폐지 종목도 패널에 있고, 보유 중이면 마지막 종가로 청산
    k = p.codes.index("000030")
    assert p.last_index[k] == 300
    ind = E.indicators(p, cfg())
    listed, ok, reasons = E.need_masks(p, ind, "A", 0)
    sig = np.zeros_like(ok)
    sig[260, k] = True
    R = E.simulate(p, ind, cfg(), "A", sig, np.ones(ok.shape), "1", "ga", 15, 20, 0)
    assert any(t["code"] == "000030" and t["reason"].startswith("상장폐지") for t in R.trades)


def test_coverage_exclusion_and_big_only_warning(tmp_path):
    con = make_db(tmp_path, investor_codes=("000010",))
    con.execute("INSERT INTO bt_job VALUES('kis','000010','success',1,NULL,NULL,NULL,'t')")
    con.commit()
    p = load_panel(con, "20150101")
    assert p.kis_complete is False
    ind = E.indicators(p, cfg())
    listed, ok, reasons = E.need_masks(p, ind, "B", 0)
    cv = E.coverage_of(p, ok, listed, reasons, "B")
    assert cv.codes == 1 and cv.big_only_warning
    assert reasons["KIS 수급 없음(아직 안 받았거나 20일 중 빠진 날)"].sum() > 0


def test_run_end_to_end_files(tmp_path):
    from collector.bt.run import run_backtest
    make_db(tmp_path).close()
    db_path = B.bt_path(tmp_path)
    out = run_backtest(db_path, cfg(), ["A", "B", "C", "D"], NOW)
    s = (out / "summary.md").read_text(encoding="utf-8")
    assert "## 계산 범위" in s[:400]
    assert s.index("## 계산 범위") < s.index("## 진입·청산 방식 비교")
    for x in ("연환산%", "최대낙폭%", "지정가체결%", "놓친신호20일%", "## 기간 분할", "## 연도별 수익률", "## 민감도", "코스피"):
        assert x in s
    assert (out / "trades.csv").exists() and (out / "equity.csv").exists()
    assert (db_path.parent / "results" / "latest" / "summary.md").exists()


def test_cli_db_path_no_keys(tmp_path, monkeypatch, capsys):
    from collector import paths
    from collector.bt.cli import main
    make_db(tmp_path / "elsewhere").close()
    inst = tmp_path / "install"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "settings.env").write_text(f"DATA_DIR={tmp_path / 'nodata'}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "install_dir", lambda: inst)
    db_file = tmp_path / "elsewhere" / "backtest" / "backtest.sqlite"
    assert main("coverage", None, str(db_file)) == 0
    assert f"BACKTEST_DB_PATH={db_file}" in (inst / "config" / "settings.env").read_text(encoding="utf-8")
    assert main("coverage", None, None) == 0  # 저장된 경로 사용
    assert "변형 A: 실행 가능" in capsys.readouterr().out
    assert not (inst / "secret").exists()  # 키를 묻지 않음
    assert not (tmp_path / "nodata").exists()


def _market(tmp_path):
    m = db.connect(tmp_path / "market.sqlite")
    db.init_db(m)
    last = DAYS[-1]
    nxt = (datetime.strptime(last, "%Y%m%d") + timedelta(days=3)).strftime("%Y%m%d")
    m.execute("INSERT INTO stock_master(code, name, market) VALUES('000010','상승A','KOSPI')")
    m.execute("INSERT INTO price_daily(date, code, market, close, is_provisional, source) VALUES(?, '000010','KOSPI',9999,0,'KRX')", (nxt,))
    m.execute("INSERT INTO price_daily(date, code, market, close, is_provisional, source) VALUES(?, '000020','KOSDAQ',1,1,'KIS')", (nxt,))
    m.execute("INSERT INTO investor_daily(date, code, frgn_net_amt_mil, orgn_net_amt_mil, is_provisional) VALUES(?, '000010', 1, 1, 0)", (nxt,))
    m.execute("INSERT INTO investor_daily(date, code, frgn_net_amt_mil, orgn_net_amt_mil, is_provisional) VALUES(?, '000020', 1, 1, 1)", (nxt,))
    m.execute("INSERT INTO index_daily(date, series, index_name, close, is_provisional, source) VALUES(?, 'KOSPI','코스피',1,0,'KRX')", (nxt,))
    m.commit()
    return m, nxt


def test_sync_confirmed_only_and_skip_existing(tmp_path):
    from collector.bt.sync import sync
    make_db(tmp_path).close()
    m, nxt = _market(tmp_path)
    m.close()
    bt = B.bt_path(tmp_path)
    lines, rep = sync(bt, tmp_path / "market.sqlite", tmp_path / "rep", NOW)
    con = sqlite3.connect(bt)
    assert con.execute("SELECT code FROM bt_daily WHERE date=?", (nxt,)).fetchall() == [("000010",)]  # 잠정값 제외
    assert con.execute("SELECT code FROM bt_investor WHERE date=?", (nxt,)).fetchall() == [("000010",)]
    assert rep.exists() and "일별 시세" in rep.read_text(encoding="utf-8")
    # 다시 돌리면 새로 들어가는 것 없음
    lines2, _ = sync(bt, tmp_path / "market.sqlite", tmp_path / "rep", NOW.replace(minute=5))
    assert "일별 시세: 새 0행" in lines2
    # market.sqlite가 없으면 건너뜀
    lines3, rep3 = sync(bt, tmp_path / "none.sqlite", tmp_path / "rep", NOW)
    assert rep3 is None and "건너뜁니다" in lines3[0]


def test_export_zip_only_db(tmp_path):
    from collector.bt.sync import export
    make_db(tmp_path).close()
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "keys.env").write_text("KIS_APP_KEY=x")
    z = export(B.bt_path(tmp_path), tmp_path / "Desktop", NOW)
    assert z.name == f"backtest_{DAYS[-1]}.zip"
    names = zipfile.ZipFile(z).namelist()
    assert names == ["backtest.sqlite"]
    assert not any("key" in n or ".env" in n for n in names)


def test_zero_price_is_no_trade(tmp_path):
    # 거래정지일: KRX가 시가·고가·저가를 0으로 준다 -> 패널에서 NaN, 체결·나눗셈 오류 없음
    con = make_db(tmp_path)
    d = DAYS[200]
    con.execute("UPDATE bt_daily SET open=0, high=0, low=0 WHERE date=? AND code='000010'", (d,))
    con.commit()
    p = load_panel(con, "20150101")
    i, j = p.dates.index(d), p.codes.index("000010")
    assert np.isnan(p.open[i, j]) and np.isnan(p.low[i, j]) and p.close[i, j] > 0
    # 엔진에 0 시가가 직접 들어와도 ZeroDivisionError 없이 주문만 대기
    p2 = tiny_panel(o=[[100], [0], [100]], h=[[100], [0], [101]], l=[[100], [0], [99]], c=[[100], [100], [100]])
    R = E.simulate(p2, tiny_ind(p2, ma20=100.0), cfg(MAX_POSITIONS="1"), "A", np.array([[True], [False], [False]]),
                   np.ones((3, 1)), "1", "ga", 15, 20, 0)
    assert all(t["entry_date"] != p2.dates[1] for t in R.trades)


# ----------------------------- 0.11.0: A1~A4, 공정 비교 -----------------------------

def test_trend_quality_value():
    # 일간 수익률이 번갈아 +2%, 0% -> 60일 수익률 / (표준편차 x sqrt(60))
    c = [100.0]
    for i in range(80):
        c.append(c[-1] * (1.02 if i % 2 == 0 else 1.0))
    p = tiny_panel(o=[[x] for x in c], h=[[x] for x in c], l=[[x] for x in c], c=[[x] for x in c])
    ind = E.indicators(p, cfg())
    t = 80
    r = np.array([c[i] / c[i - 1] - 1 for i in range(t - 59, t + 1)])
    expect = (c[t] / c[t - 60] - 1) / (r.std(ddof=1) * np.sqrt(60))
    assert math.isclose(ind.trend_quality[t, 0], expect, rel_tol=1e-6)
    assert np.isnan(ind.trend_quality[30, 0])  # 60일 안 됨


def _flat_ind(p, **kw):
    T, N = p.close.shape
    base = dict(ma20=np.full((T, N), 100.0), ma60=np.full((T, N), 90.0), ma120=np.full((T, N), 80.0),
                ma60_lag20=np.full((T, N), 85.0), pos250=np.full((T, N), 95.0), hi250=np.full((T, N), 1e9),
                atr=np.full((T, N), 1.0), val20=np.full((T, N), 1e10), val_prev20=np.full((T, N), 1e10),
                flow_sum=np.full((T, N), np.nan), idx_ma={"KOSPI": np.full(T, 1.0), "KOSDAQ": np.full(T, 1.0)},
                ret20=np.full((T, N), 0.05), trend_quality=np.full((T, N), 1.0))
    base.update(kw)
    return E.Indicators(**base)


def test_overheat_filter_and_index_filter_for_a():
    p = tiny_panel(o=[[100, 100, 100]], h=[[100, 100, 100]], l=[[100, 100, 100]], c=[[110, 130, 110]])
    ind = _flat_ind(p, ret20=np.array([[0.05, 0.05, 0.45]]))
    ok = np.ones((1, 3), dtype=bool)
    sig, *_ = E.signals(p, ind, cfg(), "A", ok, 20)
    assert sig.tolist() == [[True, True, True]]
    sig2, *_ = E.signals(p, ind, cfg(), "A2", ok, 20)
    # 종가 130 >= 20일선 100 x 1.25 -> 제외, 20일 수익률 45% >= 40 -> 제외
    assert sig2.tolist() == [[True, False, False]]
    # A3: 지수 < 120일선이면 신규 매수 중단
    p.index_close = {"KOSPI": np.array([0.5]), "KOSDAQ": np.array([2.0])}
    sig3, *_ = E.signals(p, ind, cfg(), "A3", ok, 20)
    assert sig3.tolist() == [[False, False, False]]
    p.market = np.array(["KOSPI", "KOSDAQ", "KOSDAQ"])
    sig3, *_ = E.signals(p, ind, cfg(), "A3", ok, 20)
    assert sig3.tolist() == [[False, True, True]]
    # 설정으로 A에도 지수 필터
    sigA, *_ = E.signals(p, ind, cfg(INDEX_FILTER_FOR_A="on"), "A", ok, 20)
    assert sigA.tolist() == sig3.tolist()


def test_rank_by_trend_quality():
    p = tiny_panel(o=[[100, 100]] * 2, h=[[100, 100]] * 2, l=[[100, 100]] * 2, c=[[110, 110]] * 2,
                   value=[[1e10, 5e10]] * 2)
    ind = _flat_ind(p, trend_quality=np.array([[3.0, 1.0]] * 2), val_prev20=np.full((2, 2), 1e10))
    ok = np.ones((2, 2), dtype=bool)
    _, sA, _ = E.signals(p, ind, cfg(), "A", ok, 20)
    _, sA1, _ = E.signals(p, ind, cfg(), "A1", ok, 20)
    assert sA[0].tolist() == [1.0, 5.0] and sA1[0].tolist() == [3.0, 1.0]
    sig = np.array([[True, True], [False, False]])
    R = E.simulate(p, ind, cfg(MAX_POSITIONS="1"), "A1", sig, sA1, "1", "ga", 15, 20, 0)
    assert R.trades[0]["code"] == p.codes[0]  # 추세 품질이 높은 종목
    R = E.simulate(p, ind, cfg(MAX_POSITIONS="1"), "A", sig, sA, "1", "ga", 15, 20, 0)
    assert R.trades[0]["code"] == p.codes[1]  # 거래대금 급증 순


def test_same_b_range_and_summary(tmp_path):
    from collector.bt.run import run_backtest
    make_db(tmp_path, investor_codes=("000010",)).close()
    con = open_ro(B.bt_path(tmp_path))
    p = load_panel(con, "20150101")
    ind = E.indicators(p, cfg())
    _, okA, _ = E.need_masks(p, ind, "A", 0)
    _, okB, _ = E.need_masks(p, ind, E.spec("A_sameB", cfg()).mask_base, 0)
    assert okB.any(axis=0).sum() == 1 and okA.any(axis=0).sum() > 1  # A_sameB는 수급 있는 종목만
    sig, *_ = E.signals(p, ind, cfg(), "A_sameB", okB, 20)
    assert not sig[:, [j for j, c in enumerate(p.codes) if c != "000010"]].any()
    out = run_backtest(B.bt_path(tmp_path), cfg(), list(E.ALL_VARIANTS), NOW)
    s = (out / "summary.md").read_text(encoding="utf-8")
    for x in ("거래당기대값%", "## 공정 비교", "| A_sameB |", "| A4_sameB |", "## 손실 상위 10건", "진입 직전 20일 수익률%",
              "변형 A1", "변형 A4"):
        assert x in s, x
    assert "pre_ret20_pct" in (out / "trades.csv").read_text(encoding="utf-8-sig").splitlines()[0]


# ----------------------------- 0.12.0 -----------------------------

def test_universe_excludes_spac_reits_preferred(tmp_path):
    from collector.bt.data import classify, universe_codes
    assert classify("123450", {"하나머스트제2호기업인수목적"}, [], {""}) == (False, "spac")
    assert classify("123450", {"하나머스트2호스팩"}, [], {""}) == (False, "spac")
    assert classify("330590", {"롯데리츠"}, [], {""}) == (False, "reits")
    assert classify("088980", {"맥쿼리인프라"}, [], {""}) == (False, "not_stock_name")
    assert classify("005930", {"삼성전자"}, [], {""}) == (True, "종목명·코드 규칙")
    assert classify("005930", {"삼성전자우"}, [], {""})[0] is False
    assert classify("005935", {"삼성전자"}, [], {""}) == (False, "code_suffix")
    base = [{"name": "가나", "secugrp": "주권", "kind": "종류주권", "sect_tp": ""}]
    assert classify("000100", {"가나"}, base, {""}) == (False, "not_common")
    con = make_db(tmp_path)
    con.execute("INSERT INTO bt_stock VALUES('123450','하나머스트제2호기업인수목적','KOSDAQ','20150101','20160101',1,'t',2)")
    con.execute("INSERT INTO bt_daily(date, code, market, name, close) VALUES('20150102','123450','KOSDAQ','하나머스트제2호기업인수목적',2000)")
    con.commit()
    keep, note = universe_codes(con)
    assert "123450" not in [k[0] for k in keep]
    assert note["before"] == 5 and note["after"] == 4 and note["excluded"] == {"spac": 1}
    p = load_panel(con, "20150101")
    assert "123450" not in p.codes


def test_backdrop_filter_applies_to_all_variants():
    c = cfg()
    assert c.s("BACKDROP_INDEX_FILTER") == "on"
    assert all(E.spec(v, c).index_filter for v in ("A", "A2", "B", "C"))
    off = cfg(BACKDROP_INDEX_FILTER="off")
    assert not E.spec("A", off).index_filter and not E.spec("B", off).index_filter and E.spec("D", off).index_filter
    p = tiny_panel(o=[[100]], h=[[100]], l=[[100]], c=[[110]])
    p.index_close = {"KOSPI": np.array([0.5]), "KOSDAQ": np.array([0.5])}
    ind = _flat_ind(p)
    ok = np.ones((1, 1), dtype=bool)
    assert not E.signals(p, ind, c, "A", ok, 20)[0].any()       # 배경 on: A도 지수 아래면 신규 매수 안 함
    assert E.signals(p, ind, off, "A", ok, 20)[0].all()


def test_excess_return_per_trade():
    p = tiny_panel(o=[[100], [100], [100], [120]], h=[[100], [101], [111], [121]], l=[[100], [99], [99], [119]],
                   c=[[100], [100], [110], [120]])
    p.index_close = {"KOSPI": np.array([1000.0, 1000.0, 1050.0, 1100.0]), "KOSDAQ": np.full(4, 1.0)}
    R = E.simulate(p, _flat_ind(p, ma60=np.full((4, 1), 0.0)), cfg(MAX_POSITIONS="1"), "A",
                   np.array([[True], [False], [False], [False]]), np.ones((4, 1)), "1", "ga", 15, 20, 0)
    t = R.trades[0]  # t=1 시가 매수, 기간 끝(t=3 종가) 평가 청산
    assert t["index_ret_pct"] == round((1100 / 1000 - 1) * 100, 3)
    assert t["excess_pct"] == round(t["ret_pct"] - t["index_ret_pct"], 3)
    m = E.metrics(R)
    assert m["avg_excess_pct"] == t["excess_pct"] and m["excess_win_pct"] == (100.0 if t["excess_pct"] > 0 else 0.0)


def test_signal_dedup_and_quintiles():
    from collector.bt.signals import dedup, quintile_same_day
    sig = np.zeros((50, 2), dtype=bool)
    sig[[0, 5, 21, 22, 45], 0] = True
    sig[[3], 1] = True
    ev = dedup(sig, 20)
    assert [e for e in ev if e[1] == 0] == [(0, 0), (21, 0), (45, 0)]  # 5, 22는 20거래일 안이라 제외
    events = [(0, j) for j in range(10)]
    q = quintile_same_day(events, {(0, j): float(j) for j in range(10)})
    assert [q[(0, j)] for j in range(10)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    q1 = quintile_same_day([(1, 0)], {(1, 0): 3.0})
    assert q1[(1, 0)] == 3  # 그날 신호 1개면 가운데


def test_signals_report(tmp_path):
    from collector.bt.signals import run_signals
    make_db(tmp_path).close()
    out = run_signals(B.bt_path(tmp_path), cfg(), NOW)
    s = (out / "signals.md").read_text(encoding="utf-8")
    for x in ("### 과열", "### 거래대금 급증", "### 추세 품질", "### 수급", "### 재무", "### 시장 배경", "표본 부족", "최상위", "| 개발 |", "| 검증 |"):
        assert x in s, x
    assert (out / "signals.csv").read_text(encoding="utf-8-sig").startswith("date,code,name")


def test_random_comparison_reproducible(tmp_path):
    from collector.bt.run import run_backtest
    make_db(tmp_path).close()
    db_path = B.bt_path(tmp_path)
    a = run_backtest(db_path, cfg(), ["A"], NOW, random_n=5)
    s1 = (a / "summary.md").read_text(encoding="utf-8")
    b = run_backtest(db_path, cfg(), ["A"], NOW.replace(minute=5), random_n=5)
    s2 = (b / "summary.md").read_text(encoding="utf-8")
    pick = lambda s: s[s.index("## 무작위 비교"):s.index("## 민감도")]
    assert "## 무작위 비교" in s1 and pick(s1) == pick(s2)  # 같은 시드 = 같은 결과
    c = run_backtest(db_path, cfg(RANDOM_SEED="7"), ["A"], NOW.replace(minute=6), random_n=5)
    assert "## 배경 설정" in (c / "summary.md").read_text(encoding="utf-8")
