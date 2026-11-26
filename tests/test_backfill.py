from datetime import datetime, timedelta

import httpx
import pytest

from collector import backfill as B
from collector import db
from collector.http import ApiClient
from collector.kis import KisClient
from collector.krx import KrxClient
from collector.lock import Lock
from collector.timeutil import KST

SETTINGS = {"KRX_INDEX_KOSPI_URL": "https://krx/svc/apis/idx/kospi_dd_trd",
            "KRX_INDEX_KOSDAQ_URL": "https://krx/svc/apis/idx/kosdaq_dd_trd", "DART_DAILY_LIMIT": "18000"}
NOW = datetime(2026, 11, 26, 20, 0, tzinfo=KST)
HOLIDAY = "20261105"
SPLIT = ("000030", "20261110")  # 1:2 분할: 기준가 500


def wd(a, b):
    return [d for d in B.weekdays(a, b) if d != HOLIDAY]


class Fake:
    def __init__(self, base_history=True):
        self.base_history = base_history
        self.krx_calls = self.kis_calls = 0
        self.interrupt_after = None
        self.days = wd("20261102", "20261113")

    def stocks(self, d):
        out = [("000010", "가나전자", 1000.0, 0.0), ("000015", "가나전자우", 800.0, 0.0)]
        if d <= "20261106":
            out.append(("000020", "폐지기업", 300.0, 0.0))  # 11/06 이후 상장폐지
        if d < SPLIT[1]:
            out.append(("000030", "분할기업", 1000.0, 0.0))
        elif d == SPLIT[1]:
            out.append(("000030", "분할기업", 520.0, 20.0))  # 기준가 500 != 직전 종가 1000
        else:
            out.append(("000030", "분할기업", 520.0, 0.0))
        return out

    def handler(self, req):
        p, q = req.url.path, dict(req.url.params)
        if p == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "TOKENVALUE123456", "access_token_token_expired": "2026-11-27 20:00:00"})
        if "/sto/" in p or "/idx/" in p:
            self.krx_calls += 1
            if self.interrupt_after is not None and self.krx_calls > self.interrupt_after:
                raise KeyboardInterrupt
            d = q["basDd"]
            if d not in self.days:
                return httpx.Response(200, json={"OutBlock_1": []})
            if "base_info" in p:
                if not self.base_history and d != self.days[-1]:
                    return httpx.Response(200, json={"OutBlock_1": []})
                rows = [{"ISU_SRT_CD": c, "ISU_ABBRV": n, "SECUGRP_NM": "주권",
                         "KIND_STKCERT_TP_NM": "구형우선주" if c.endswith("5") else "보통주", "SECT_TP_NM": ""}
                        for c, n, *_ in self.stocks(d)] if "stk_" in p else []
                return httpx.Response(200, json={"OutBlock_1": rows})
            if "/idx/" in p:
                return httpx.Response(200, json={"OutBlock_1": [{"IDX_NM": "코스피", "CLSPRC_IDX": "2500"}]})
            if "ksq_" in p:
                return httpx.Response(200, json={"OutBlock_1": []})
            return httpx.Response(200, json={"OutBlock_1": [
                {"ISU_CD": c, "ISU_NM": n, "TDD_CLSPRC": str(cl), "CMPPREVDD_PRC": str(cmp), "ACC_TRDVAL": "10000000000",
                 "MKTCAP": "300000000000" if c == "000010" else "1000000000", "LIST_SHRS": "100"}
                for c, n, cl, cmp in self.stocks(d)]})
        if p.endswith("investor-trade-by-stock-daily"):
            self.kis_calls += 1
            code, d = q["FID_INPUT_ISCD"], q["FID_INPUT_DATE_1"]
            assert q["FID_COND_MRKT_DIV_CODE"] == "J" and q["FID_ORG_ADJ_PRC"] == "0"
            days = [x for x in reversed(self.days) if x <= d and any(c == code for c, *_ in self.stocks(x))][:3]
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok",
                                             "output2": [{"stck_bsop_date": x, "frgn_ntby_qty": "1", "frgn_ntby_tr_pbmn": "2"} for x in days]})
        return httpx.Response(404)

    def api(self):
        return ApiClient("T", 1000, client=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda s: None)


@pytest.fixture(autouse=True)
def small_range(monkeypatch):
    monkeypatch.setattr(B, "START", "20261102")
    monkeypatch.setattr(B, "KIS_PAGE_DAYS", 3)
    monkeypatch.setattr(B, "BIG_WINDOW", 3)
    monkeypatch.setattr(B, "DELISTED_GAP_DAYS", 3)


def krx_run(tmp_path, f):
    con = B.open_bt(tmp_path)
    rep = B.krx_backfill(con, KrxClient(f.api(), "https://krx/svc/apis", "KRXKEY123456"), SETTINGS,
                         lambda: NOW, lambda *_: None, end="20261113")
    return con, rep


def test_krx_backfill_resume_adj_and_common(tmp_path):
    f = Fake()
    f.interrupt_after = 7
    con, rep = krx_run(tmp_path, f)
    assert rep.stopped and "이어받기" in rep.stopped
    done = con.execute("SELECT COUNT(*) FROM bt_job WHERE kind LIKE 'krx_%'").fetchone()[0]
    assert done == 7
    f.interrupt_after = None
    con, rep = krx_run(tmp_path, f)
    assert rep.stopped is None
    # 휴장일은 빈 응답(성공 아님)
    assert B.job_status(con, "krx_trade_KOSPI", HOLIDAY)[0] == "empty"
    # 상장폐지 종목도 남아 있음
    assert con.execute("SELECT last_date FROM bt_stock WHERE code='000020'").fetchone()[0] == "20261106"
    # 수정계수: 분할 전 0.5, 분할일 이후 1.0
    adj = dict(con.execute("SELECT date, adj_factor FROM bt_daily WHERE code='000030'").fetchall())
    assert adj["20261109"] == 0.5 and adj["20261110"] == 1.0 and adj["20261113"] == 1.0
    ev = con.execute("SELECT * FROM bt_corp_event").fetchall()
    assert [(e["code"], e["date"], e["ratio"]) for e in ev] == [("000030", "20261110", 0.5)]
    # 보통주 판별 (과거 basDd 기본정보 가능 -> monthly)
    st = {r["code"]: r for r in con.execute("SELECT * FROM bt_stock")}
    assert st["000015"]["is_common"] == 0 and st["000010"]["is_common"] == 1
    assert "monthly_base_info" in st["000010"]["common_method"]
    assert st["000010"]["priority"] == 1 and st["000030"]["priority"] == 2
    # 다시 돌리면 받을 것 없음
    n = f.krx_calls
    krx_run(tmp_path, f)
    assert f.krx_calls == n


def test_common_fallback_name_rule(tmp_path):
    f = Fake(base_history=False)
    con, rep = krx_run(tmp_path, f)
    assert con.execute("SELECT value FROM meta WHERE key='bt_common_method'").fetchone()[0] == "current_base_info+name_rule"
    st = {r["code"]: r for r in con.execute("SELECT * FROM bt_stock")}
    assert st["000020"]["is_common"] == 1 and "종목명" in st["000020"]["common_method"]  # 현재 기본정보에 없는 폐지 종목
    assert B.name_rule_common("가나전자우", "000015") is False and B.name_rule_common("가나스팩1호", "000040") is False


def kis(f, tmp_path, now):
    return KisClient(f.api(), "https://kis", "KISAPPKEY0001", "KISSECRET00001", tmp_path / "t.json", now=lambda: now)


def test_kis_window_paging_resume_and_delisted_probe(tmp_path):
    f = Fake()
    con, _ = krx_run(tmp_path, f)
    # 15:40 이전: 호출 없음
    rep = B.kis_backfill(con, kis(f, tmp_path, NOW.replace(hour=10)), lambda: NOW.replace(hour=10), lambda *_: None)
    assert f.kis_calls == 0 and "15:40" in rep.stopped
    # 20:00 시작, 몇 번 부른 뒤 자정을 넘김 -> 저장하고 멈춤
    t = {"n": 0}

    def clock():
        t["n"] += 1
        return NOW if t["n"] < 12 else NOW.replace(day=27, hour=0, minute=1)

    rep = B.kis_backfill(con, kis(f, tmp_path, NOW), clock, lambda *_: None)
    assert "벗어나 멈춤" in rep.stopped
    probe = con.execute("SELECT value FROM meta WHERE key='bt_delisted_probe'").fetchone()[0]
    assert "000020" in probe and "응답 있음" in probe
    partial = con.execute("SELECT COUNT(*) FROM bt_investor").fetchone()[0]
    assert partial > 0
    # 다음 실행에서 이어받아 끝냄
    rep = B.kis_backfill(con, kis(f, tmp_path, NOW), lambda: NOW, lambda *_: None)
    assert rep.stopped is None
    for code, n_days in (("000010", 9), ("000020", 4), ("000030", 9)):
        assert con.execute("SELECT COUNT(*) FROM bt_investor WHERE code=?", (code,)).fetchone()[0] == n_days
    assert con.execute("SELECT COUNT(*) FROM bt_investor WHERE code='000015'").fetchone()[0] == 0  # 우선주 제외
    assert B.kis_remaining_calls(con) == 0


def test_dart_remaining_limit(tmp_path):
    c = db.connect(tmp_path / "market.sqlite")
    db.init_db(c)
    c.execute("INSERT INTO meta VALUES('dart_calls_20261126', '5000')")
    assert B.dart_remaining_limit(c, SETTINGS, "20261126") == (13000, 5000)
    assert B.dart_remaining_limit(None, SETTINGS, "20261126") == (18000, 0)
    assert B.dart_remaining_limit(c, {"DART_DAILY_LIMIT": "3000"}, "20261126") == (0, 5000)


def test_cli_lock_and_status(tmp_path, monkeypatch, capsys):
    from collector import __main__ as M, paths
    inst = tmp_path / "install"
    (inst / "config").mkdir(parents=True)
    (inst / "config" / "settings.env").write_text(f"DATA_DIR={tmp_path / 'data'}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "install_dir", lambda: inst)
    lock = Lock(tmp_path / "data" / "system" / "collector.lock")
    (tmp_path / "data" / "system").mkdir(parents=True)
    import os
    (tmp_path / "data" / "system" / "collector.lock").write_text(str(os.getppid()))  # 다른 프로세스가 실행 중
    assert M.main(["backfill", "status"]) == 1
    assert "다시 실행하세요" in capsys.readouterr().out
    (tmp_path / "data" / "system" / "collector.lock").unlink()
    assert M.main(["backfill", "status"]) == 0
    out = capsys.readouterr().out
    assert "KRX 진행" in out and "KIS 수급" in out and "DART" in out
    assert not (tmp_path / "data" / "db" / "market.sqlite").exists()  # 수집 DB를 만들지 않음


def test_dart_backfill_no_raw_files_and_limit(tmp_path):
    from .fake_dart import FakeDart
    con = B.open_bt(tmp_path)
    for code in ("005930", "247540"):
        con.execute("INSERT INTO bt_stock(code, is_common, priority, first_date, last_date) VALUES(?,1,1,'20160104','20261125')", (code,))
    collect = db.connect(tmp_path / "market.sqlite")
    db.init_db(collect)
    collect.execute("INSERT INTO meta VALUES('dart_calls_20261126', '17990')")  # 정기 수집이 오늘 17,990회 씀
    f = FakeDart()
    rep = B.dart_backfill(con, f.client(), tmp_path, collect, SETTINGS, lambda: NOW, lambda *_: None)
    assert "백필 10회" in rep.lines[0] and rep.stopped  # 남은 10회만 쓰고 멈춤
    assert f.calls <= 10
    collect.execute("UPDATE meta SET value='0' WHERE key='dart_calls_20261126'")
    rep = B.dart_backfill(con, f.client(), tmp_path, collect, SETTINGS, lambda: NOW, lambda *_: None)
    assert con.execute("SELECT COUNT(*) FROM dart_fs_quarter").fetchone()[0] > 0
    assert not (tmp_path / "raw").exists()  # 원본 응답 파일을 남기지 않음
