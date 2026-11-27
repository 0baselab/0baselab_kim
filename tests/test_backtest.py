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
