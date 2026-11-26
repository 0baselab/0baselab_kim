from datetime import date, datetime, timedelta

import httpx
import pytest

from collector import db
from collector.http import ApiClient
from collector.timeutil import KST
from collector.verify import history as H

NOW = datetime(2026, 11, 26, 21, 0, tzinfo=KST)
KEYS = {"KRX_AUTH_KEY": "KRXKEY123456", "KIS_APP_KEY": "KISAPPKEY0001",
        "KIS_APP_SECRET": "KISSECRET00001", "DART_API_KEY": "DARTKEY000001"}
SETTINGS = {"KIS_BASE_URL": "https://kis", "KRX_BASE_URL": "https://krx/svc/apis", "DART_BASE_URL": "https://dart/api",
            "KRX_INDEX_KOSPI_URL": "https://krx/svc/apis/idx/kospi_dd_trd",
            "KRX_INDEX_KOSDAQ_URL": "https://krx/svc/apis/idx/kosdaq_dd_trd"}


def weekdays_before(d: str, n: int, floor: str):
    x = datetime.strptime(d, "%Y%m%d").date()
    out = []
    while len(out) < n:
        s = x.strftime("%Y%m%d")
        if s < floor:
            break
        if x.weekday() < 5:
            out.append(s)
        x -= timedelta(days=1)
    return out


class Fake:
    def __init__(self, kis_floor="20190701", krx_floor="20151201", krx_holes=()):
        self.kis_floor, self.krx_floor, self.krx_holes = kis_floor, krx_floor, set(krx_holes)
        self.calls = {"kis": 0, "krx": 0, "dart": 0}

    def handler(self, req):
        p, q = req.url.path, dict(req.url.params)
        if p == "/oauth2/tokenP":
            return httpx.Response(200, json={"access_token": "TOKENVALUE123456", "access_token_token_expired": "2026-11-27 20:00:00"})
        if p.endswith("investor-trade-by-stock-daily"):
            self.calls["kis"] += 1
            days = weekdays_before(q["FID_INPUT_DATE_1"], 30, self.kis_floor)
            return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok",
                                             "output2": [{"stck_bsop_date": d} for d in days]})
        if "/sto/" in p or "/idx/" in p:
            self.calls["krx"] += 1
            d = q["basDd"]
            ok = d >= self.krx_floor and d not in self.krx_holes
            return httpx.Response(200, json={"OutBlock_1": [{"BAS_DD": d}] if ok else []})
        if p.endswith("fnlttSinglAcntAll.json"):
            self.calls["dart"] += 1
            if q["bsns_year"] == "2015":
                return httpx.Response(200, json={"status": "013", "message": "조회된 데이타가 없습니다."})
            return httpx.Response(200, json={"status": "000", "list": [{"rcept_no": q["bsns_year"] + "0301000001", "thstrm_nm": "제 x 기"}]})
        return httpx.Response(404)

    def factory(self):
        t = httpx.MockTransport(self.handler)
        return lambda name, rate: ApiClient(name, 1000, client=httpx.Client(transport=t), sleep=lambda s: None)


def test_months_ago_and_search():
    assert H.months_ago(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert H.months_ago(date(2026, 11, 26), 120) == date(2016, 11, 26)
    # 89개월 전까지 데이터가 있는 가짜 함수
    low, high, seen = H.search_oldest_month(lambda n: n <= 89, H.KIS_OFFSETS_MONTHS)
    assert (low, high) == (89, 90)
    assert len(seen) <= 8 + 5
    assert H.search_oldest_month(lambda n: True, H.KIS_OFFSETS_MONTHS)[:2] == (120, None)
    assert H.search_oldest_month(lambda n: False, H.KIS_OFFSETS_MONTHS)[:2] == (None, 3)


def test_full_run_without_db(tmp_path):
    f = Fake()
    p = H.run_history(now=NOW, settings=SETTINGS, keys=KEYS, out_dir=tmp_path / "out",
                      db_path=tmp_path / "data" / "db" / "market.sqlite", http_factory=f.factory(), out=lambda *_: None)
    text = p.read_text(encoding="utf-8")
    assert p.name == "history_20261126-210000.md"
    assert not (tmp_path / "data").exists()  # 데이터 폴더·DB를 만들지 않음
    # KIS: 2019-07-01부터 데이터 -> 경계 월 2019-07 (3종목 모두)
    assert text.count("2019-07 (약 88개월 전") == 3
    assert "217730 고정 종목 사용" in text
    # KRX: 2015-12-01 이후만 -> 10년 전(2016-11)까지 있고 15년 전은 없음
    assert "20161125 (값이 나온 가장 오래된 요청일)" in text
    assert "거래일 근사(평일)" in text
    # DART: 2015 없음, 2018부터
    assert "2018년 사업보고서" in text
    total = sum(f.calls.values())
    assert total <= H.MAX_CALLS and f"총 호출: {total}회" in text
    for v in list(KEYS.values()) + ["TOKENVALUE123456"]:
        assert v not in text


def test_krx_retry_on_empty_weekday(tmp_path):
    f = Fake(krx_holes={"20251126", "20251125"})  # 1년 전 근처 평일이 휴장이라 가정
    p = H.run_history(now=NOW, settings=SETTINGS, keys=KEYS, out_dir=tmp_path, db_path=tmp_path / "none.sqlite",
                      http_factory=f.factory(), out=lambda *_: None)
    text = p.read_text(encoding="utf-8")
    assert "재시도 2" in text and "| 20251124 |" in text


def test_before_1540_skips_kis(tmp_path):
    f = Fake()
    msgs = []
    p = H.run_history(now=NOW.replace(hour=10), settings=SETTINGS, keys=KEYS, out_dir=tmp_path,
                      db_path=tmp_path / "none.sqlite", http_factory=f.factory(), out=msgs.append)
    assert f.calls["kis"] == 0
    assert any("15:40 이후" in m for m in msgs)
    assert "15:40 이후 실행 필요" in p.read_text(encoding="utf-8")


def test_with_db_uses_calendar_and_picks_smallcap_readonly(tmp_path):
    path = tmp_path / "market.sqlite"
    con = db.connect(path)
    db.init_db(con)
    con.execute("INSERT INTO trading_calendar VALUES('20261125',1,'x','t',NULL),('20261126',1,'x','t',NULL)")
    for i in range(20):
        code = f"9{i:05d}"
        con.execute("INSERT INTO stock_master(code, market) VALUES(?, 'KOSDAQ')", (code,))
        con.execute("INSERT INTO universe_daily(date, code, in_universe) VALUES('20261125', ?, 1)", (code,))
        con.execute("INSERT INTO price_daily(date, code, mktcap_krw) VALUES('20261125', ?, ?)", (code, (i + 1) * 10**9))
    con.close()
    before = path.stat().st_mtime_ns
    code, why = H.pick_smallcap(H.open_db_readonly(path))
    assert code == "900001" and "DB 자동 선택" in why  # 하위 10%(2개) 중 가운데
    f = Fake()
    H.run_history(now=NOW, settings=SETTINGS, keys=KEYS, out_dir=tmp_path / "out", db_path=path,
                  http_factory=f.factory(), out=lambda *_: None)
    assert path.stat().st_mtime_ns == before  # DB에 쓰지 않음
