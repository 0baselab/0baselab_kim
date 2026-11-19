import csv
from datetime import datetime

import pytest

from collector import db
from collector.dart_collect import classify, run
from collector.dart_export import export
from collector.timeutil import KST

from .fake_dart import FakeDart

NOW = datetime(2026, 11, 22, 10, 15, tzinfo=KST)
TARGETS = {"005930", "247540", "123456"}


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "m.sqlite")
    db.init_db(c)
    c.execute("INSERT INTO stock_master(code, name, market) VALUES('005930','삼성전자','KOSPI'),('247540','에코프로비엠','KOSDAQ')")
    return c


def q(con, corp, y, qt, fs="CFS"):
    return con.execute("SELECT * FROM dart_fs_quarter WHERE corp_code=? AND fiscal_year=? AND quarter=? AND fs_div=?",
                       (corp, y, qt, fs)).fetchone()


def test_classify():
    assert classify("분기보고서 (2025.03)") == ("2025", "11013", 1)
    assert classify("[기재정정]반기보고서 (2025.06)") == ("2025", "11012", 2)
    assert classify("사업보고서 (2025.12)") == ("2025", "11011", 4)
    assert classify("사업보고서 (2025.06)") == ("2025", None, None)  # 6월 결산
    assert classify("주요사항보고서(자기주식취득결정)") == (None, None, None)


def test_quarter_conversion_14_4(con, tmp_path):
    f = FakeDart()
    st = run(con, f.client(), tmp_path, lambda: NOW, 18000, TARGETS)
    assert st.fs_ok == 5 and st.skipped_fy == 1 and st.remaining == 0
    q1, q2, q3, q4 = (q(con, "00126380", "2025", i) for i in (1, 2, 3, 4))
    # 손익: 분기 단독 = thstrm_amount, 4분기 = 연간 - 3분기 누적. IS 우선 (CIS 값 1은 무시)
    assert (q1["revenue_krw"], q2["revenue_krw"], q3["revenue_krw"]) == (79140503000000, 74566317000000, 86061747000000)
    assert q4["revenue_krw"] == 333605938000000 - 239768567000000
    assert q4["net_income_krw"] == 45206805000000 - 25565060000000
    # 현금흐름: 이번 누적 - 직전 누적, 1분기 그대로, 4분기 = 연간 - 3분기 누적
    assert (q1["ocf_krw"], q2["ocf_krw"], q3["ocf_krw"], q4["ocf_krw"]) == (
        16580866000000, 33941002000000 - 16580866000000, 56515496000000 - 33941002000000, 85315148000000 - 56515496000000)
    # 재무상태: 잔액 그대로, 없는 계정은 빈 칸 + 사유
    assert q4["assets_krw"] == 500000000000000 and q4["liabilities_krw"] is None
    assert "ifrs-full_Liabilities 없음" in q4["calc_note"] and "dart_OperatingIncomeLoss 없음" in q4["calc_note"]
    assert q4["rcept_dt"] == "20260310"
    # 연결 없는 회사는 013 -> 별도
    assert q(con, "01160363", "2025", 4, "OFS") is not None and q(con, "01160363", "2025", 4, "CFS") is None


def test_rerun_does_not_refetch(con, tmp_path):
    f = FakeDart()
    run(con, f.client(), tmp_path, lambda: NOW, 18000, TARGETS)
    n = f.fs_calls
    run(con, f.client(), tmp_path, lambda: NOW.replace(day=29), 18000, TARGETS)
    assert f.fs_calls == n


def test_daily_budget_stops_and_resumes(con, tmp_path):
    f = FakeDart()
    st = run(con, f.client(), tmp_path, lambda: NOW, 20, TARGETS)
    assert st.stopped and "18000" not in st.stopped and st.remaining > 0
    st2 = run(con, f.client(), tmp_path, lambda: NOW.replace(day=23), 18000, TARGETS)
    assert st2.remaining == 0 and st2.stopped is None


def test_limit_020_stops(con, tmp_path):
    f = FakeDart()
    f.limit_after = 3
    st = run(con, f.client(), tmp_path, lambda: NOW, 18000, TARGETS)
    assert st.stopped and "020" in st.stopped


def test_export_csv_with_ttm(con, tmp_path):
    run(con, FakeDart().client(), tmp_path, lambda: NOW, 18000, TARGETS)
    fin, fil = export(con, tmp_path / "out", NOW)
    assert fin.name == "dart_financials_2025Q1-2025Q4_20261122-101500.csv"
    rows = list(csv.DictReader(fin.open(encoding="utf-8-sig")))
    sam = [r for r in rows if r["code"] == "005930"]
    assert len(sam) == 4
    q4 = sam[-1]
    assert q4["revenue_ttm_krw"] == "333605938000000"  # 4개 분기 합 = 연간
    assert sam[2]["revenue_ttm_krw"] == ""  # 2024 분기 없음 -> 빈 칸
    assert q4["fs_div"] == "CFS" and q4["market"] == "KOSPI"
    eco = [r for r in rows if r["code"] == "247540"]
    assert eco[0]["fs_div"] == "OFS"
    frows = list(csv.DictReader(fil.open(encoding="utf-8-sig")))
    assert {"code", "name", "rcept_dt", "report_nm", "rcept_no"} == set(frows[0])


def test_budget_stop_still_computes_quarters(con, tmp_path):
    f = FakeDart()
    # corpCode 1 + list 호출들 + 재무 몇 건 뒤 한도
    st = run(con, f.client(), tmp_path, lambda: NOW, 20, TARGETS)
    assert st.stopped and st.fs_ok >= 1
    n_q = con.execute("SELECT COUNT(*) FROM dart_fs_quarter").fetchone()[0]
    assert n_q >= 1 and st.quarters == n_q


def test_previously_fetched_but_uncomputed_are_computed(con, tmp_path):
    run(con, FakeDart().client(), tmp_path, lambda: NOW, 18000, TARGETS)
    con.execute("DELETE FROM dart_fs_quarter")  # 0.7.0 이전처럼 계산이 빠진 상태
    f = FakeDart()
    st = run(con, f.client(), tmp_path, lambda: NOW.replace(day=23), 0, TARGETS)  # 한도 0: 호출 없이 바로 멈춤
    assert f.calls == 0 and st.stopped
    assert con.execute("SELECT COUNT(*) FROM dart_fs_quarter").fetchone()[0] == st.quarters > 0


def test_corrections_fetched_once_per_period(con, tmp_path, monkeypatch):
    from . import fake_dart
    extra = [("00126380", "005930", "[기재정정]분기보고서 (2025.09)", "20251201000001", "20251201"),
             ("00126380", "005930", "[기재정정]분기보고서 (2025.09)", "20251215000001", "20251215")]
    monkeypatch.setattr(fake_dart, "FILINGS", fake_dart.FILINGS + extra)
    f = FakeDart()
    st = run(con, f.client(), tmp_path, lambda: NOW, 18000, TARGETS)
    assert f.fs_calls == 6  # 삼성 4기간 + 에코프로 2(CFS 013 -> OFS). 3분기 3건은 한 번만
    assert st.fs_dedup == 2
    for no in ("20251114000001", "20251201000001", "20251215000001"):
        assert db.get_status(con, "DART", "fs", no) == "success"
    # 새 정정 공시가 생기면 그 기간만 다시 받는다
    monkeypatch.setattr(fake_dart, "FILINGS", fake_dart.FILINGS +
                        [("00126380", "005930", "[기재정정]분기보고서 (2025.09)", "20251220000001", "20251220")])
    con.execute("DELETE FROM meta WHERE key='dart_list_until'")  # 목록을 다시 받게
    f2 = FakeDart()
    run(con, f2.client(), tmp_path, lambda: NOW.replace(day=29), 18000, TARGETS)
    assert f2.fs_calls == 1
    assert db.get_status(con, "DART", "fs", "20251220000001") == "success"
