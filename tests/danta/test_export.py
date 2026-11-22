import hashlib
import json
import sqlite3

from danta.export import date_style, export_subset


def make_src(path, big_nodate=10, raw_len=3000):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE price_daily(date TEXT, code TEXT, close REAL, PRIMARY KEY(date, code));
    CREATE TABLE dashed(date TEXT, v INTEGER);
    CREATE TABLE stock_master(code TEXT PRIMARY KEY, name TEXT);
    CREATE TABLE big_nodate(id INTEGER PRIMARY KEY, v TEXT);
    CREATE TABLE raw_store(date TEXT, body TEXT);
    CREATE TABLE small_json(date TEXT, detail_json TEXT);
    CREATE TABLE empty_dated(date TEXT, v INTEGER);
    CREATE INDEX idx_price_code ON price_daily(code);
    """)
    con.executemany("INSERT INTO price_daily VALUES (?,?,?)",
                    [(d, c, 1.0) for d in ("20250829", "20250901", "20250902") for c in ("A", "B")])
    con.executemany("INSERT INTO dashed VALUES (?,?)", [("2025-08-31", 1), ("2025-09-01", 2), ("2025-10-01", 3)])
    con.executemany("INSERT INTO stock_master VALUES (?,?)", [("A", "a"), ("B", "b")])
    con.executemany("INSERT INTO big_nodate(v) VALUES (?)", [("x",)] * big_nodate)
    body = json.dumps({"output": ["y" * raw_len]})
    con.executemany("INSERT INTO raw_store VALUES (?,?)", [("20250901", body)] * 3)
    con.executemany("INSERT INTO small_json VALUES (?,?)", [("20250901", '{"a":1}')] * 3)
    con.commit()
    con.close()


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_date_style():
    assert date_style(["20250901", None]) == "8"
    assert date_style(["2025-09-01"]) == "10"
    assert date_style(["20250901", "2025-09-01"]) is None


def test_export_rules(tmp_path):
    src = tmp_path / "market.sqlite"
    make_src(src, big_nodate=50)
    before = sha(src)
    out = tmp_path / "analysis" / "market_subset.sqlite"
    out.parent.mkdir()
    out.write_text("old")                                 # 이미 있으면 덮어씀
    logs = []
    r = export_subset(src, out, logs.append, since="20250901", max_mb=380, nodate_limit=20, profile="all")
    assert r["ok"] and sha(src) == before                 # 원본 변경 없음
    t = {i["table"]: i for i in r["tables"]}
    o = sqlite3.connect(out)
    assert o.execute("SELECT COUNT(*), MIN(date) FROM price_daily").fetchone() == (4, "20250901")
    assert o.execute("SELECT COUNT(*), MIN(date) FROM dashed").fetchone() == (2, "2025-09-01")
    assert t["price_daily"]["date_style"] == "YYYYMMDD" and t["dashed"]["date_style"] == "YYYY-MM-DD"
    assert o.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0] == 2           # 작은 무날짜: 통째로
    assert "big_nodate" in t and "50" in t["big_nodate"]["skip"]                       # 큰 무날짜: 건너뜀
    assert "body" in t["raw_store"]["skip"]                                            # 원본 JSON: 건너뜀
    assert t["small_json"]["copied"] == 3                                              # 작은 JSON은 복사
    assert t["empty_dated"]["copied"] == 0 and t["empty_dated"]["skip"] is None
    tables = {r[0] for r in o.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "big_nodate" not in tables and "raw_store" not in tables
    assert any("컬럼: date, code, close" in l for l in logs)
    assert any("결과 파일 크기" in l for l in logs)
    assert not (out.parent / "market_subset.sqlite.tmp").exists()


def test_export_fails_when_too_big(tmp_path):
    src = tmp_path / "market.sqlite"
    make_src(src)
    con = sqlite3.connect(src)
    con.executemany("INSERT INTO price_daily VALUES (?,?,?)",
                    [("20251001", f"C{i:06d}", float(i)) for i in range(60000)])
    con.commit(); con.close()
    before = sha(src)
    out = tmp_path / "analysis" / "market_subset.sqlite"
    logs = []
    r = export_subset(src, out, logs.append, since="20250901", max_mb=0.5, profile="all")
    assert not r["ok"] and not out.exists() and sha(src) == before
    i = next(n for n, l in enumerate(logs) if "큰 테이블부터" in l)
    assert "price_daily" in logs[i + 1]                  # 가장 큰 테이블이 맨 위


def test_bad_since(tmp_path):
    import pytest
    from danta.export import ExportError
    make_src(tmp_path / "m.sqlite")
    with pytest.raises(ExportError):
        export_subset(tmp_path / "m.sqlite", tmp_path / "o.sqlite", lambda m: None, since="2025-09-01")



def make_analysis_src(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE price_daily(date TEXT NOT NULL, code TEXT NOT NULL, market TEXT, open REAL, high REAL, low REAL,
        close REAL, change_rate_pct REAL, volume_sh INTEGER, value_krw INTEGER, mktcap_krw INTEGER,
        listed_shares INTEGER, is_provisional INTEGER, source TEXT, fetched_at TEXT, raw_file TEXT,
        PRIMARY KEY (date, code));
    CREATE TABLE universe_daily(date TEXT, code TEXT, in_universe INTEGER, exclude_reason TEXT, is_admin INTEGER,
        is_halted INTEGER, days_since_listing INTEGER, is_new_listing INTEGER, source TEXT, PRIMARY KEY(date, code));
    CREATE TABLE investor_daily(date TEXT, code TEXT, frgn_net_qty INTEGER, orgn_net_qty INTEGER,
        prsn_net_qty INTEGER, frgn_net_amt_mil INTEGER, orgn_net_amt_mil INTEGER, prsn_net_amt_mil INTEGER,
        detail_json TEXT, is_provisional INTEGER, PRIMARY KEY(date, code));
    CREATE TABLE market_investor_daily(date TEXT, market TEXT, frgn_net_qty_unconfirmed INTEGER,
        orgn_net_amt_unconfirmed INTEGER, index_close REAL, detail_json TEXT, is_provisional INTEGER,
        PRIMARY KEY(date, market));
    CREATE TABLE trading_calendar(date TEXT PRIMARY KEY, is_open INTEGER, source TEXT);
    CREATE TABLE stock_master(code TEXT PRIMARY KEY, name TEXT, market TEXT, krx_kind TEXT, raw_file TEXT);
    CREATE TABLE theme_versions(version INTEGER PRIMARY KEY, loaded_at TEXT);
    CREATE TABLE theme_membership(version INTEGER, theme TEXT, code TEXT, PRIMARY KEY(version, theme, code));
    CREATE TABLE dart_fs_raw(rcept_no TEXT, v TEXT);
    """)
    big = json.dumps({"x": "y" * 5000})   # all 프로필이면 이 표 전체가 빠지는 원인
    for d in ("20250829", "20250901", "20250902"):
        for c in ("A", "B", "C", "D"):
            con.execute("INSERT INTO price_daily(date, code, close, source) VALUES (?,?,1,'KRX')", (d, c))
            con.execute("INSERT INTO universe_daily(date, code, in_universe) VALUES (?,?,?)", (d, c, int(c != "D")))
        for c in ("A", "B") if d == "20250901" else ("A", "B", "C"):
            con.execute("INSERT INTO investor_daily VALUES (?,?,1,2,3,4,5,6,?,0)", (d, c, big))
        con.execute("INSERT INTO market_investor_daily VALUES (?,'KOSPI',1,2,2500.5,?,0)", (d, big))
        con.execute("INSERT INTO trading_calendar VALUES (?,1,'t')", (d,))
    con.execute("INSERT INTO stock_master VALUES ('A','a','KOSPI','보통주','f')")
    con.execute("INSERT INTO theme_versions VALUES (1,'t')")
    con.execute("INSERT INTO theme_membership VALUES (1,'반도체','A')")
    con.execute("INSERT INTO dart_fs_raw VALUES ('1','x')")
    con.commit()
    con.close()


def test_analysis_profile(tmp_path):
    src = tmp_path / "market.sqlite"
    make_analysis_src(src)
    before = sha(src)
    out = tmp_path / "analysis" / "market_subset.sqlite"
    logs = []
    r = export_subset(src, out, logs.append, since="20250901")   # 기본 프로필 = analysis
    assert r["ok"] and sha(src) == before
    o = sqlite3.connect(out)
    tables = {x[0] for x in o.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "dart_fs_raw" not in tables and "investor_daily" in tables
    cols = lambda t: [x[1] for x in o.execute(f"PRAGMA table_info({t})")]
    assert cols("investor_daily") == ["date", "code", "frgn_net_qty", "orgn_net_qty", "prsn_net_qty",
                                      "frgn_net_amt_mil", "orgn_net_amt_mil", "prsn_net_amt_mil", "is_provisional"]
    assert cols("market_investor_daily") == ["date", "market", "frgn_net_qty_unconfirmed",
                                             "orgn_net_amt_unconfirmed", "index_close", "is_provisional"]
    assert "source" not in cols("price_daily") and "raw_file" not in cols("stock_master")
    assert o.execute("SELECT COUNT(*), MIN(date) FROM investor_daily").fetchone() == (5, "20250901")
    assert o.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 2
    assert o.execute("SELECT COUNT(*) FROM theme_membership").fetchone()[0] == 1
    # 기본키 유지
    assert [x[1] for x in o.execute("PRAGMA table_info(price_daily)") if x[5]] == ["date", "code"]
    # 원본에 없는 열·표는 경고만
    assert any("경고: stock_master.listed_date 열이 원본에 없어" in l for l in logs)
    assert any("경고: index_daily 표가 원본에 없어" in l for l in logs)
    assert any("investor_daily: " in l and "MB" in l for l in logs)
    # 수급 요약: 날짜별 2, 3 / price_daily 8행 중 5행 / 보통주(6행) 중 5행
    assert any("날짜별 종목 수: 최소 2, 평균 2.5, 최대 3 (2일)" in l for l in logs)
    assert any("수급 있는 비율: 5 / 8 (62.5%)" in l for l in logs)
    assert any("보통주 범위(in_universe=1)만: 5 / 6 (83.3%)" in l for l in logs)


def test_all_profile_still_drops_raw_json_table(tmp_path):
    src = tmp_path / "market.sqlite"
    make_analysis_src(src)
    r = export_subset(src, tmp_path / "o.sqlite", lambda m: None, since="20250901", profile="all")
    t = {i["table"]: i for i in r["tables"]}
    assert "detail_json" in t["investor_daily"]["skip"]
