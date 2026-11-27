"""백테스트 데이터 읽기 (backtest.sqlite 읽기 전용) + 지표 계산.

- 백필이 돌고 있어도 읽을 수 있게 읽기 전용 URI로 열고, 잠금 파일은 잡지 않는다.
- 가격은 수정계수(adj_factor)를 곱한 수정가격. 원시 가격은 바꾸지 않는다.
- n일 지표는 n거래일 자료가 모두 있을 때만 값이 있다(없으면 NaN = 계산 범위에서 제외).
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

NAN = np.nan


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"백테스트 DB가 없습니다: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con, name) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# ------------------------------------------------------------------ 확보 현황
def coverage(con) -> dict:
    c: dict = {}
    if not _table_exists(con, "bt_daily"):
        return {"error": "bt_daily 표가 없습니다 (backfill krx 전)"}
    r = con.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM bt_daily").fetchone()
    c["krx"] = {"from": r[0], "to": r[1], "codes": r[2] or 0}
    st = con.execute("SELECT COUNT(*), SUM(is_common=1), SUM(priority=1 AND is_common=1) FROM bt_stock").fetchone()
    c["common"] = st[1] or 0
    c["big"] = st[2] or 0
    r = con.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM bt_investor").fetchone()
    done = con.execute("SELECT COUNT(*) FROM bt_job WHERE kind='kis' AND status IN ('success','empty')").fetchone()[0] \
        if _table_exists(con, "bt_job") else 0
    big_done = con.execute("SELECT COUNT(*) FROM bt_job j JOIN bt_stock s ON s.code=j.unit_key WHERE j.kind='kis'"
                           " AND j.status IN ('success','empty') AND s.priority=1").fetchone()[0] \
        if _table_exists(con, "bt_job") else 0
    c["kis"] = {"from": r[0], "to": r[1], "codes": r[2] or 0, "done": done, "big_done": big_done}
    if _table_exists(con, "dart_fs_quarter"):
        r = con.execute("SELECT MIN(fiscal_year), MAX(fiscal_year), COUNT(DISTINCT q.corp_code), COUNT(*) FROM dart_fs_quarter q").fetchone()
        mapped = con.execute("SELECT COUNT(DISTINCT c.stock_code) FROM dart_fs_quarter q JOIN dart_corp c ON c.corp_code=q.corp_code"
                             " WHERE c.stock_code IS NOT NULL").fetchone()[0]
        c["dart"] = {"from": r[0], "to": r[1], "corps": r[2] or 0, "rows": r[3] or 0, "codes": mapped}
    else:
        c["dart"] = {"from": None, "to": None, "corps": 0, "rows": 0, "codes": 0}
    r = con.execute("SELECT MIN(date), MAX(date) FROM bt_index WHERE index_name IN ('코스피','코스닥')").fetchone()
    c["index"] = {"from": r[0], "to": r[1]}
    c["variants"] = runnable(c)
    return c


def runnable(c) -> dict[str, str | None]:
    """변형별로 지금 돌릴 수 있는지. None이면 가능, 문자열이면 불가 이유."""
    out = {"A": None if c["krx"]["codes"] else "KRX 시세 없음"}
    out["B"] = out["A"] or (None if c["kis"]["codes"] else "KIS 수급 없음")
    out["C"] = out["B"] or (None if c["dart"]["codes"] else "DART 재무 없음")
    out["D"] = out["C"] or (None if c["index"]["to"] else "KRX 지수 없음")
    return out


def coverage_lines(c) -> list[str]:
    if "error" in c:
        return [c["error"]]
    k, s, d, ix = c["krx"], c["kis"], c["dart"], c["index"]
    L = [f"KRX 시세: {k['from']}~{k['to']}, 종목 {k['codes']}개 (보통주 {c['common']}개, 대형 {c['big']}개)",
         f"KIS 수급: {s['from'] or '-'}~{s['to'] or '-'}, 종목 {s['codes']}개, 끝난 종목 {s['done']}/{c['common']}"
         f" (대형 {s['big_done']}/{c['big']})",
         f"DART 재무: {d['from'] or '-'}~{d['to'] or '-'}년, {d['corps']}개사 (종목 연결 {d['codes']}개)",
         f"KRX 지수(코스피·코스닥): {ix['from'] or '-'}~{ix['to'] or '-'}"]
    for v, why in c["variants"].items():
        L.append(f"변형 {v}: " + ("실행 가능" if why is None else f"실행 불가 ({why})"))
    return L


# ------------------------------------------------------------------ 패널
@dataclass
class Panel:
    dates: list[str]
    codes: list[str]
    market: np.ndarray            # 코드별 'KOSPI'/'KOSDAQ'
    name: list[str]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    value: np.ndarray             # 거래대금(원, 원시)
    mktcap: np.ndarray            # 시가총액(원)
    flow_mil: np.ndarray          # 외국인+기관 순매수 대금(백만 원), 없으면 NaN
    fin_ok: np.ndarray            # 재무 조건 1/0, 재무 없음 NaN
    index_close: dict = field(default_factory=dict)  # 'KOSPI'/'KOSDAQ' -> (T,)
    last_index: np.ndarray | None = None  # 코드별 마지막 데이터 인덱스 (상장폐지 판단)
    themes: dict = field(default_factory=dict)  # code -> [theme]
    kis_complete: bool = True


DT = np.float32  # 메모리 절약 (10년 x 3,000종목). 누적 합은 float64로 계산한 뒤 저장


def _mat(T, N):
    return np.full((T, N), NAN, dtype=DT)


def load_panel(con, start: str, themes_csv: Path | None = None) -> Panel:
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM bt_daily ORDER BY date")]
    stocks = con.execute("SELECT code, name, market FROM bt_stock WHERE is_common=1 ORDER BY code").fetchall()
    codes = [s["code"] for s in stocks]
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: j for j, c in enumerate(codes)}
    T, N = len(dates), len(codes)
    o, h, l, c, v, m = (_mat(T, N) for _ in range(6))
    for r in con.execute("SELECT date, code, open, high, low, close, value_krw, mktcap_krw, adj_factor FROM bt_daily"):
        j = ci.get(r["code"])
        if j is None:
            continue
        i = di[r["date"]]
        f = r["adj_factor"] or 1.0
        for arr, x in ((o, r["open"]), (h, r["high"]), (l, r["low"]), (c, r["close"])):
            # 0 이하 가격 = 그날 거래 없음(거래정지 등). KRX가 시가·고가·저가를 0으로 주는 경우가 있다.
            arr[i, j] = NAN if x is None or x <= 0 else x * f
        v[i, j] = NAN if r["value_krw"] is None else r["value_krw"]
        m[i, j] = NAN if r["mktcap_krw"] is None else r["mktcap_krw"]
    flow = _mat(T, N)
    for r in con.execute("SELECT date, code, frgn_net_amt_mil, orgn_net_amt_mil FROM bt_investor"):
        j, i = ci.get(r["code"]), di.get(r["date"])
        if j is None or i is None or r["frgn_net_amt_mil"] is None or r["orgn_net_amt_mil"] is None:
            continue
        flow[i, j] = r["frgn_net_amt_mil"] + r["orgn_net_amt_mil"]
    fin = _fin_matrix(con, dates, ci)
    idx = {}
    for series, nm in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        arr = np.full(T, NAN)
        for r in con.execute("SELECT date, close FROM bt_index WHERE series=? AND index_name=?", (series, nm)):
            if r["date"] in di and r["close"] is not None:
                arr[di[r["date"]]] = r["close"]
        idx[series] = arr
    present = ~np.isnan(c)
    last = np.where(present.any(axis=0), T - 1 - np.argmax(present[::-1], axis=0), -1)
    kis_complete = True
    try:
        left = con.execute("SELECT COUNT(*) FROM bt_stock s WHERE s.is_common=1 AND NOT EXISTS (SELECT 1 FROM bt_job j"
                           " WHERE j.kind='kis' AND j.unit_key=s.code AND j.status IN ('success','empty'))").fetchone()[0]
        kis_complete = left == 0
    except sqlite3.Error:
        pass
    return Panel(dates, codes, np.array([s["market"] for s in stocks]), [s["name"] for s in stocks],
                 o, h, l, c, v, m, flow, fin, idx, last, load_themes(themes_csv), kis_complete)


def _fin_matrix(con, dates, ci) -> np.ndarray:
    """C 조건 (최근 4분기 영업이익 합 > 0, 부채비율 < 상한은 run 시점에 적용하므로 여기서는 원값 저장).
    공시일(rcept_dt) 다음 거래일부터 반영: rcept_dt < 그 날짜인 보고서만 사용한다(미래 정보 차단)."""
    T, N = len(dates), len(ci)
    out = np.full((T, N, 3), NAN, dtype=np.float64)  # [ttm_op_income, liabilities, equity]
    if not _table_exists(con, "dart_fs_quarter"):
        return out
    rows = con.execute(
        "SELECT c.stock_code, q.fiscal_year, q.quarter, q.fs_div, q.op_income_krw, q.liabilities_krw, q.equity_krw, q.rcept_dt"
        " FROM dart_fs_quarter q JOIN dart_corp c ON c.corp_code=q.corp_code WHERE c.stock_code IS NOT NULL"
        " ORDER BY c.stock_code, q.fiscal_year, q.quarter").fetchall()
    per: dict[str, dict] = {}
    for r in rows:
        if r["stock_code"] not in ci or not r["rcept_dt"]:
            continue
        key = (int(r["fiscal_year"]) * 4 + int(r["quarter"]) - 1)
        d = per.setdefault(r["stock_code"], {})
        if key not in d or r["fs_div"] == "CFS":  # 연결 우선, 연결·별도는 섞지 않음
            d[key] = r
    import bisect
    for code, qs in per.items():
        j = ci[code]
        events = []  # (사용 가능일 = rcept_dt 다음 날부터, 값)
        keys = sorted(qs)
        for k in keys:
            win = [qs.get(k - n) for n in range(4)]
            if any(w is None for w in win) or len({w["fs_div"] for w in win}) > 1:
                ttm = None
            else:
                ops = [w["op_income_krw"] for w in win]
                ttm = None if None in ops else sum(ops)
            avail = max(w["rcept_dt"] for w in win if w is not None)
            events.append((avail, ttm, qs[k]["liabilities_krw"], qs[k]["equity_krw"]))
        events.sort(key=lambda e: e[0])
        avail = [e[0] for e in events]
        for t, d in enumerate(dates):
            pos = bisect.bisect_left(avail, d) - 1  # rcept_dt < d (공시 다음 거래일부터)
            if pos >= 0:
                _, ttm, li, eq = events[pos]
                out[t, j] = [NAN if ttm is None else ttm, NAN if li is None else li, NAN if eq is None else eq]
    return out


def load_themes(path: Path | None) -> dict[str, list[str]]:
    if path is None or not path.exists():
        return {}
    from ..themes import _read_text, fix_code
    out: dict[str, list[str]] = {}
    for r in csv.DictReader(io.StringIO(_read_text(path))):
        t, c = (r.get("theme") or "").strip(), fix_code((r.get("code") or "").strip())
        if t and c:
            out.setdefault(c, []).append(t)
    return out


# ------------------------------------------------------------------ 지표
def rolling_sum(x: np.ndarray, n: int) -> np.ndarray:
    """앞의 n개(자기 포함)가 모두 있을 때만 합. 없으면 NaN."""
    T = x.shape[0]
    out = np.full(x.shape, NAN, dtype=x.dtype)
    if T < n:
        return out
    z = np.where(np.isnan(x), 0.0, x).astype(np.float64)
    cs = np.cumsum(z, axis=0)
    cn = np.cumsum(np.isnan(x), axis=0)
    cs = np.concatenate([np.zeros((1,) + x.shape[1:]), cs])
    cn = np.concatenate([np.zeros((1,) + x.shape[1:]), cn])
    s = cs[n:] - cs[:-n]
    bad = (cn[n:] - cn[:-n]) > 0
    out[n - 1:] = np.where(bad, NAN, s)
    return out


def rolling_mean(x, n):
    return rolling_sum(x, n) / n


def rolling_max(x, n):
    out = np.full(x.shape, NAN, dtype=x.dtype)
    if x.shape[0] >= n:
        out[n - 1:] = sliding_window_view(x, n, axis=0).max(axis=-1)
    return out


def rolling_min(x, n):
    out = np.full(x.shape, NAN, dtype=x.dtype)
    if x.shape[0] >= n:
        out[n - 1:] = sliding_window_view(x, n, axis=0).min(axis=-1)
    return out


def shift(x, k):
    out = np.full(x.shape, NAN, dtype=x.dtype)
    if k < x.shape[0]:
        out[k:] = x[:-k] if k else x
    return out


def atr(h, l, c, n):
    pc = shift(c, 1)
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    tr[np.isnan(pc) | np.isnan(h) | np.isnan(l)] = NAN
    return rolling_mean(tr, n)
