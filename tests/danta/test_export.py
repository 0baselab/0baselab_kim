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
    r = export_subset(src, out, logs.append, since="20250901", max_mb=380, nodate_limit=20)
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
    r = export_subset(src, out, logs.append, since="20250901", max_mb=0.5)
    assert not r["ok"] and not out.exists() and sha(src) == before
    i = next(n for n, l in enumerate(logs) if "큰 테이블부터" in l)
    assert "price_daily" in logs[i + 1]                  # 가장 큰 테이블이 맨 위


def test_bad_since(tmp_path):
    import pytest
    from danta.export import ExportError
    make_src(tmp_path / "m.sqlite")
    with pytest.raises(ExportError):
        export_subset(tmp_path / "m.sqlite", tmp_path / "o.sqlite", lambda m: None, since="2025-09-01")
