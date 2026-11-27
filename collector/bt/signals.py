"""신호 효과 측정 (backtest signals). 매매 규칙(손절, 종목 수, 진입 방식) 없이 신호 자체의 예측력만 본다.

- 기본 신호: 거르기 통과 + A의 진입 조건(정배열, 60일선 상승, 250일 위치 80% 이상). 지수 필터는 쓰지 않는다
  (시장 배경은 따로 나눠 비교한다).
- 같은 종목은 신호 후 SIGNAL_GAP_DAYS(20)거래일 안의 다음 신호를 뺀다(겹침 방지).
- 수익률: 신호 다음 날 시가 -> h거래일 뒤 종가 (h = 5, 20, 60). 보유 중 상장폐지는 마지막 종가.
- 초과수익: 같은 기간 해당 시장 지수(신호일 종가 -> h거래일 뒤 종가) 수익률을 뺀 값.
- 조건별 구간은 그날 신호 종목 안에서 나눈다(5분위 = 그날 순위 기준).
"""
from __future__ import annotations

import csv
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from ..timeutil import stamp
from . import engine as E
from .config import Config
from .data import load_panel, open_ro, universe_lines

HORIZONS = (5, 20, 60)
MIN_N = 30


def dedup(sig: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """(t, j) 목록. 같은 종목은 앞 신호 후 gap 거래일 안의 신호를 뺀다."""
    out = []
    T, N = sig.shape
    for j in range(N):
        last = -10 ** 9
        for t in np.nonzero(sig[:, j])[0]:
            if t - last > gap:
                out.append((int(t), j))
                last = t
    out.sort()
    return out


def quintile_same_day(events: list[tuple[int, int]], values: dict) -> dict:
    """그날 신호 종목 안에서 값의 순위로 5분위(1=낮음 ~ 5=높음). 값이 없으면 None."""
    by_day: dict[int, list] = {}
    for t, j in events:
        v = values.get((t, j))
        if v is not None and not math.isnan(v):
            by_day.setdefault(t, []).append((v, j))
    q = {}
    for t, lst in by_day.items():
        lst.sort()
        n = len(lst)
        for rank, (v, j) in enumerate(lst):
            q[(t, j)] = min(5, int((rank + 0.5) / n * 5) + 1)
    return q


def stats(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"n": 0}
    mean = sum(xs) / n
    sd = statistics.stdev(xs) if n > 1 else 0.0
    return {"n": n, "mean": mean, "median": statistics.median(xs), "win": sum(1 for x in xs if x > 0) / n * 100,
            "t": mean / (sd / math.sqrt(n)) if sd > 0 else None}


def run_signals(db_path: Path, cfg: Config, now: datetime,
                progress: Callable[[str, float], None] = lambda s, x: None, themes_csv: Path | None = None) -> Path:
    con = open_ro(db_path)
    try:
        progress("데이터 읽는 중", 0.05)
        p = load_panel(con, cfg.s("START"), themes_csv)
    finally:
        con.close()
    start_idx = next((i for i, d in enumerate(p.dates) if d >= cfg.s("START")), len(p.dates))
    progress("지표 계산 중", 0.2)
    ind = E.indicators(p, cfg)
    _, ok, _ = E.need_masks(p, ind, "A", start_idx, need_index=False)
    base_cfg = cfg.copy(BACKDROP_INDEX_FILTER="off", OVERHEAT_FILTER="off", INDEX_FILTER_FOR_A="off")
    sig, _, _ = E.signals(p, ind, base_cfg, "A", ok, 20)
    events = dedup(sig, cfg.i("SIGNAL_GAP_DAYS"))
    progress(f"신호 {len(events)}개 측정 중", 0.5)
    T = len(p.dates)
    rows = []
    with np.errstate(invalid="ignore", divide="ignore"):
        value_x = p.value / ind.val_prev20
        flow = ind.flow_sum * 1e6 / p.mktcap
    for t, j in events:
        if t + 1 >= T or np.isnan(p.open[t + 1, j]):
            continue
        mk = str(p.market[j])
        idx = p.index_close.get(mk)
        r = {"t": t, "j": j, "date": p.dates[t], "code": p.codes[j], "name": p.name[j], "market": mk}
        entry = float(p.open[t + 1, j])
        for h in HORIZONS:
            th = t + h
            if th >= T:
                r[f"ret{h}"] = r[f"ex{h}"] = None
                continue
            px = p.close[th, j]
            if np.isnan(px) and p.last_index[j] < th:  # 그 사이 상장폐지: 마지막 종가
                px = p.close[int(p.last_index[j]), j]
            ret = None if np.isnan(px) else float(px / entry - 1) * 100
            ir = None if idx is None or np.isnan(idx[t]) or np.isnan(idx[th]) else float(idx[th] / idx[t] - 1) * 100
            r[f"ret{h}"], r[f"ex{h}"] = ret, (None if ret is None or ir is None else ret - ir)
        r["overheat"] = bool(ind.ret20[t, j] * 100 >= cfg.f("OVERHEAT_RET20_PCT")
                             or p.close[t, j] >= ind.ma20[t, j] * (1 + cfg.f("OVERHEAT_GAP20_PCT") / 100))
        r["value_x"] = float(value_x[t, j])
        r["trend_quality"] = float(ind.trend_quality[t, j])
        r["flow"] = float(flow[t, j])
        ttm, li, eq = p.fin_ok[t, j]
        r["fin"] = "자료 없음" if np.isnan(ttm) or np.isnan(li) or np.isnan(eq) else (
            "통과" if ttm > 0 and eq > 0 and li / eq * 100 < cfg.f("DEBT_RATIO_MAX_PCT") else "탈락")
        im = ind.idx_ma.get(mk)
        r["backdrop"] = None if idx is None or im is None or np.isnan(idx[t]) or np.isnan(im[t]) else (
            "지수 120일선 위" if idx[t] >= im[t] else "지수 120일선 아래")
        rows.append(r)
    keys = [(r["t"], r["j"]) for r in rows]
    for name in ("value_x", "trend_quality", "flow"):
        q = quintile_same_day(keys, {(r["t"], r["j"]): r[name] for r in rows})
        for r in rows:
            r[f"q_{name}"] = q.get((r["t"], r["j"]))
    out = db_path.parent / "results" / stamp(now)
    out.mkdir(parents=True, exist_ok=True)
    _write(out, p, cfg, rows, len(events))
    progress("완료", 1.0)
    return out


def _f(v, d=2):
    return "" if v is None else f"{v:,.{d}f}"


def _table(rows, group_key, groups, dev_end, label):
    L = [f"### {label}", "", "| 구간 | 기간 | 신호 수 | 5일 평균초과% | 20일 평균초과% | 20일 중앙값% | 20일 초과승률% | 20일 t값 | 60일 평균초과% | 비고 |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    periods = (("전체", lambda d: True), ("개발", lambda d: d <= dev_end), ("검증", lambda d: d > dev_end))
    by = {}
    for gname, pred in groups:
        for pname, pp in periods:
            sel = [r for r in rows if pred(r.get(group_key)) and pp(r["date"])]
            s = {h: stats([r[f"ex{h}"] for r in sel if r.get(f"ex{h}") is not None]) for h in HORIZONS}
            by[(gname, pname)] = s
            n = s[20]["n"]
            L.append(f"| {gname} | {pname} | {n} | {_f(s[5].get('mean'))} | {_f(s[20].get('mean'))} | "
                     f"{_f(s[20].get('median'))} | {_f(s[20].get('win'))} | {_f(s[20].get('t'))} | {_f(s[60].get('mean'))} | "
                     f"{'표본 부족' if n < MIN_N else ''} |")
    if len(groups) >= 2:
        top, bottom = groups[-1][0], groups[0][0]
        for pname, _ in periods:
            diffs = []
            for h in HORIZONS:
                a, b = by[(top, pname)][h].get("mean"), by[(bottom, pname)][h].get("mean")
                diffs.append(_f(None if a is None or b is None else a - b))
            L.append(f"- {pname}: 최상위({top}) - 최하위({bottom}) 평균초과 차이 5일 {diffs[0]} / 20일 {diffs[1]} / 60일 {diffs[2]}")
    return L + [""]


def _write(out: Path, p, cfg: Config, rows: list, n_events: int):
    cols = ["date", "code", "name", "market", "overheat", "value_x", "q_value_x", "trend_quality", "q_trend_quality",
            "flow", "q_flow", "fin", "backdrop"] + [f"{k}{h}" for h in HORIZONS for k in ("ret", "ex")]
    with (out / "signals.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])
    dev = cfg.s("DEV_END")
    q = [(f"{i}분위", (lambda i: lambda v: v == i)(i)) for i in range(1, 6)]
    flow_rows = [r for r in rows if r.get("q_flow") is not None]
    fin_rows = [r for r in rows if r["fin"] != "자료 없음"]
    L = ["# 신호 효과 측정", "",
         "매매 규칙(손절·종목 수·진입 방식) 없이 신호 자체의 예측력만 봅니다. 초과수익 = 신호 다음 날 시가 기준 수익률 - 같은 기간 해당 시장 지수 수익률.",
         "", f"- 기본 신호: 거르기 통과 + 정배열 + 60일선 상승 + 250일 위치 {cfg.s('POS250_MIN_PCT')}% 이상 (지수 필터 없음)",
         f"- 같은 종목 {cfg.s('SIGNAL_GAP_DAYS')}거래일 안 중복 신호 제외: 원 신호 {n_events}개 -> 측정 {len(rows)}개",
         f"- 구간 비교는 그날 신호 종목 안에서 나눔. 신호 {MIN_N}개 미만 구간은 '표본 부족'. 개발 ~{dev} / 검증 그 이후",
         "- " + universe_lines(p.universe_note)[0], ""]
    L += _table(rows, "overheat", [("비과열", lambda v: v is False), ("과열", lambda v: v is True)], dev,
                f"과열 (20일 수익률 ≥ {cfg.s('OVERHEAT_RET20_PCT')}% 또는 20일선 대비 +{cfg.s('OVERHEAT_GAP20_PCT')}% 이상)")
    L += _table(rows, "q_value_x", q, dev, "거래대금 급증 (당일 거래대금 / 직전 20일 평균) 5분위")
    L += _table(rows, "q_trend_quality", q, dev, "추세 품질 (60일 수익률 / 변동성) 5분위")
    fr = (f"{min(r['date'] for r in flow_rows)}~{max(r['date'] for r in flow_rows)}, {len(flow_rows)}개 신호"
          if flow_rows else "자료 없음")
    L += _table(flow_rows, "q_flow", q, dev, f"수급 (20일 외국인+기관 순매수 / 시총) 5분위 - 수급 자료 있는 신호만 ({fr})")
    L += _table(rows, "fin", [("탈락", lambda v: v == "탈락"), ("자료 없음", lambda v: v == "자료 없음"),
                              ("통과", lambda v: v == "통과")], dev,
                f"재무 (C 조건: 4분기 영업이익 합 > 0, 부채비율 < {cfg.s('DEBT_RATIO_MAX_PCT')}%, 자본 > 0) - 재무 자료 있는 신호 {len(fin_rows)}개")
    L += _table(rows, "backdrop", [("지수 120일선 아래", lambda v: v == "지수 120일선 아래"),
                                   ("지수 120일선 위", lambda v: v == "지수 120일선 위")], dev, "시장 배경")
    (out / "signals.md").write_text("\n".join(L) + "\n", encoding="utf-8")
