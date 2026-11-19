from datetime import datetime

import pytest
from openpyxl import load_workbook

from collector.briefing import BriefingError, build, default_date
from collector.briefing_writer import export_unclassified_repeats, record_unclassified, write
from collector.timeutil import KST

from .test_collect import NOW, collect, install, kis_settings, reopen  # noqa: F401
from .fake_market import FakeMarket


@pytest.fixture
def filled(install):
    kis_settings(install)
    collect(install, FakeMarket())
    return install


def themes_dir(install):
    return install.parent / "data" / "themes"


def stock(pkg, code):
    return next(s for s in pkg.stocks if s["code"] == code)


def test_today_briefing_with_kis_provisional(filled):
    con = reopen(filled)
    assert default_date(con) == "20261119"
    pkg = build(con, "20261119", themes_dir(filled), NOW)
    s = stock(pkg, "005930")
    assert {x["code"] for x in pkg.stocks} == {"005930", "0041B0", "247540", "222220"}
    assert s["close"] == 1050 and s["ret_5d_pct"] == 5.0
    assert s["gap_ma5_pct"] == round((1050 / 1010 - 1) * 100, 4)
    assert s["value_x_ma5"] == 1.47
    assert s["ret_60d_pct"] is None and "시세 빠짐" in s["missing_note"]
    assert s["frgn_net_qty_5d"] == 50 and s["frgn_net_amt_mil_20d"] == 240
    assert s["frgn_streak"] == 60 and s["orgn_streak"] == -60  # 저장된 60일(2회 x 30일) 모두 순매수
    assert s["investor_is_provisional"] == 1
    assert s["is_admin"] == 0 and stock(pkg, "222220")["is_admin"] == 1
    assert pkg.provisional == {"price": 4, "investor": 4}
    m = {r["market"]: r for r in pkg.market}
    assert m["KOSPI"]["index_close"] == 2500 and m["KOSPI"]["index_is_provisional"] == 1
    assert m["KOSPI"]["up_count"] == 2  # 005930, 0041B0 (+1.5%)
    assert m["KOSDAQ"]["frgn_net_amt_unconfirmed_1d"] == -2000
    assert pkg.themes is None  # CSV 없음 -> 양식만 만들고 테마 시트 생략
    assert (themes_dir(filled) / "theme_master.csv").exists()
    # 판단 단어 없음
    text = str(pkg.stocks) + str(pkg.market)
    for word in ("강세", "약세", "추천", "점수", "rank", "score"):
        assert word not in text


def test_past_date_uses_only_data_until_then(filled):
    con = reopen(filled)
    pkg = build(con, "20261117", themes_dir(filled), NOW)
    s = stock(pkg, "005930")
    assert s["close"] == 1000 and s["ret_5d_pct"] == 0.0
    assert s["investor_is_provisional"] == 0
    assert pkg.provisional["price"] == 0


def test_reproducible(filled):
    con = reopen(filled)
    a = build(con, "20261119", themes_dir(filled), NOW)
    b = build(con, "20261119", themes_dir(filled), NOW.replace(minute=59))
    assert a.stocks == b.stocks and a.market == b.market


def test_not_ready(filled):
    con = reopen(filled)
    with pytest.raises(BriefingError):
        build(con, "20261120", themes_dir(filled), NOW)
    with pytest.raises(BriefingError):
        build(con, "20261114", themes_dir(filled), NOW)  # 토요일


def test_themes_versions_and_problems(filled):
    con = reopen(filled)
    td = themes_dir(filled)
    td.mkdir(parents=True, exist_ok=True)
    csv = td / "theme_master.csv"
    csv.write_text("theme,code,name,basis,added_date,note\n"
                   "HBM,005930,삼성전자,근거,2026-11-20,\n"
                   "HBM,247540,에코프로비엠,근거,2026-11-20,\n"
                   "HBM,005930,중복,근거,2026-11-20,\n"
                   "HBM,5930,앞0빠짐,근거,2026-11-20,\n"
                   "HBM,005935,우선주,근거,2026-11-20,\n"
                   "HBM,999999,없음,근거,2026-11-20,\n"
                   "HBM,000660,,,2026-11-20,\n"
                   "AI,12AB,형식오류,근거,2026-11-20,\n"
                   "AI,5930,앞0빠짐2,근거,2026-11-20,\n", encoding="utf-8-sig")
    pkg = build(con, "20261119", td, NOW)
    assert pkg.theme_version == 1
    t = next(x for x in pkg.themes if x["theme"] == "HBM")
    assert (t["theme"], t["stocks"], t["ret_1d_pct_median"]) == ("HBM", 2, 1.5)
    assert t["frgn_net_amt_mil_1d"] == 24
    probs = {r["detail"] for r in pkg.status if r["kind"] == "테마 CSV 문제"}
    assert probs == {"같은 (theme, code) 중복", "종목코드 형식 오류 (영문 대문자·숫자 6자리)",
                     "분석 대상에서 제외된 종목", "없는 종목코드", "빈 필수 칸: basis"}
    fixes = [r["detail"] for r in pkg.status if r["kind"] == "자동 보정"]
    assert fixes == ["종목코드 5930 -> 005930", "종목코드 5930 -> 005930"]
    assert stock(pkg, "005930")["themes"] == "AI;HBM"  # 보정된 줄은 그대로 반영
    assert all(u["code"] not in ("005930", "247540") for u in pkg.unclassified)
    assert build(con, "20261119", td, NOW).theme_version == 1  # 같은 파일 -> 같은 버전
    csv.write_text(csv.read_text(encoding="utf-8-sig") + "AI,0041B0,신규,근거,2026-11-20,\n", encoding="utf-8-sig")
    assert build(con, "20261119", td, NOW.replace(second=5)).theme_version == 2
    assert len(list((td / "history").glob("theme_master_v*.csv"))) == 2


def test_corp_action_blanks_returns(filled):
    con = reopen(filled)
    con.execute("UPDATE price_daily SET cmpprevdd_prc=500 WHERE code='005930' AND date='20261118'")
    pkg = build(con, "20261118", themes_dir(filled), NOW)
    s = stock(pkg, "005930")
    assert s["corp_action_suspect"] == 1 and s["ret_5d_pct"] is None
    assert "기준가 조정 20261118" in s["missing_note"]
    assert any(r["kind"] == "기업행동 의심" for r in pkg.status)


def test_write_xlsx_json_and_unclassified(filled):
    con = reopen(filled)
    pkg = build(con, "20261119", themes_dir(filled), NOW, top_n=1)
    paths = write(pkg, filled.parent / "data" / "briefing", NOW, with_json=True)
    assert paths[0].name == "briefing_20261119_20261119-203000.xlsx" and paths[1].suffix == ".json"
    wb = load_workbook(paths[0], read_only=True)
    assert wb.sheetnames == ["0_안내", "1_시장", "2_지수시리즈", "4_종목", "5_미분류", "6_데이터상태"]
    assert len(list(wb["4_종목"].iter_rows())) == 5
    assert 1 <= len(pkg.unclassified) <= 2
    record_unclassified(con, pkg)
    p = export_unclassified_repeats(con, filled.parent / "data" / "briefing", NOW)
    assert p.read_text(encoding="utf-8-sig").startswith("code,name,days_listed")


def test_adjusted_price_used_for_corp_action(install):
    kis_settings(install)
    m = FakeMarket()
    m.split_day = {"005930": "20261113"}
    collect(install, m)
    con = reopen(install)
    assert m.adj_calls >= 1
    assert con.execute("SELECT COUNT(*) FROM adj_price_daily WHERE code='005930'").fetchone()[0] > 0
    # 원본 가격은 그대로
    assert con.execute("SELECT close FROM price_daily WHERE code='005930' AND date='20261112'").fetchone()[0] == 1000
    pkg = build(con, "20261118", themes_dir(install), NOW)
    s = stock(pkg, "005930")
    assert s["close"] == 1000  # 원본
    # 수정주가: 11/13 이전 500, 이후 1000 -> 5일 전(11/11) 500 대비 +100%
    assert s["ret_5d_pct"] == 100.0
    assert "수정주가 사용" in s["missing_note"]
    # 다시 수집해도 같은 변경일이면 재조회하지 않음
    n = m.adj_calls
    collect(install, m, now=NOW.replace(minute=50))
    assert m.adj_calls == n


def test_adjusted_price_failure_blanks(install):
    kis_settings(install)
    m = FakeMarket()
    m.split_day = {"005930": "20261113"}
    m.adj_fail = True
    collect(install, m)
    con = reopen(install)
    pkg = build(con, "20261118", themes_dir(install), NOW)
    s = stock(pkg, "005930")
    assert s["ret_5d_pct"] is None
    assert "수정주가 없음" in s["missing_note"] and "기준가 조정 20261113" in s["missing_note"]


def test_listed_shares_change_alone_is_not_corp_action(install):
    kis_settings(install)
    m = FakeMarket()
    m.share_change = {"005930": "20261113"}  # CB 전환·자사주 소각 등
    collect(install, m)
    assert m.adj_calls == 0
    con = reopen(install)
    s = stock(build(con, "20261118", themes_dir(install), NOW), "005930")
    assert s["corp_action_suspect"] == 0 and s["ret_5d_pct"] == 0.0


def test_backfill_cmpprevdd_from_raw(install):
    kis_settings(install)
    m = FakeMarket()
    m.split_day = {"005930": "20261113"}
    collect(install, m)
    con = reopen(install)
    con.execute("UPDATE price_daily SET cmpprevdd_prc=NULL")
    con.execute("DELETE FROM meta WHERE key='backfill_cmpprevdd'")
    con.close()
    a, res = collect(install, m, now=NOW.replace(minute=55))
    assert any("원본에서 채웠습니다" in l for l in res.lines)
    con = reopen(install)
    assert con.execute("SELECT cmpprevdd_prc FROM price_daily WHERE code='005930' AND date='20261113'").fetchone()[0] == 500
