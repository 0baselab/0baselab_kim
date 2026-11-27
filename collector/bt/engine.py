"""추세추종 백테스트 엔진 (명세서 17장). 계산만 한다.

하루 순서 (t일):
1. 상장폐지(더 이상 데이터 없음) 보유 종목은 마지막 종가로 청산
2. (나) 장중 손절: 시가 <= 손절가면 시가, 저가 <= 손절가면 손절가로 매도
3. 전날 장 마감 후 정한 매도(종가 판단)를 오늘 시가로 실행
4. 매수 주문 체결: ① 오늘 시가 / ②③④ 지정가(저가 <= 지정가면 체결, 체결가 = min(지정가, 시가)), 최대 5거래일 대기
5. 종가로 평가, 보유 후 최고 종가 갱신
6. 장 마감 후 청산 판단(최고 종가 대비 하락, 종가 < 60일선) -> 다음 날 시가 매도
7. 장 마감 후 진입 판단 -> 다음 날 주문
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .config import Config, tax_pct
from .data import Panel, atr, rolling_max, rolling_mean, rolling_min, rolling_sum, shift

ENTRY_METHODS = {"1": "① 다음 날 시가", "2": "② 20일선 지정가", "3": "③ 전일 종가 - 0.5xATR 지정가", "4": "④ 250일 고가 지정가"}
EXIT_METHODS = {"ga": "(가) 종가 판단 -> 다음 날 시가", "na": "(나) 장중 손절"}
VARIANTS = {"A": "KRX 시세 추세", "B": "A + KIS 수급", "C": "B + DART 재무", "D": "C + 지수 필터"}


@dataclass
class Indicators:
    ma20: np.ndarray
    ma60: np.ndarray
    ma120: np.ndarray
    ma60_lag20: np.ndarray
    pos250: np.ndarray
    hi250: np.ndarray
    atr: np.ndarray
    val20: np.ndarray
    val_prev20: np.ndarray
    flow_sum: np.ndarray
    idx_ma: dict


def indicators(p: Panel, cfg: Config) -> Indicators:
    c = p.close
    hi_c, lo_c = rolling_max(c, 250), rolling_min(c, 250)
    rng = hi_c - lo_c
    with np.errstate(invalid="ignore", divide="ignore"):
        pos = np.where(rng > 0, (c - lo_c) / rng * 100, np.where(rng == 0, 100.0, np.nan))
    ma60 = rolling_mean(c, 60)
    val20 = rolling_mean(p.value, 20)
    idx_ma = {k: rolling_mean(v.reshape(-1, 1), cfg.i("INDEX_MA_DAYS")).ravel() for k, v in p.index_close.items()}
    return Indicators(rolling_mean(c, 20), ma60, rolling_mean(c, 120), shift(ma60, 20), pos.astype(c.dtype),
                      rolling_max(p.high, 250), atr(p.high, p.low, c, cfg.i("ATR_DAYS")), val20, shift(val20, 1),
                      rolling_sum(p.flow_mil, cfg.i("FLOW_DAYS")), idx_ma)


@dataclass
class Coverage:
    first: str | None
    last: str | None
    codes: int
    pairs: int
    listed_pairs: int
    reasons: dict
    big_only_warning: bool


def need_masks(p: Panel, ind: Indicators, variant: str, start_idx: int):
    """(사용 가능 여부, 빠진 이유별 마스크). 종목·날짜 단위로 필요한 데이터가 모두 있어야 사용."""
    T, N = p.close.shape
    listed = ~np.isnan(p.close)
    listed[:start_idx] = False
    price_ok = ~(np.isnan(ind.ma120) | np.isnan(ind.pos250) | np.isnan(ind.ma60_lag20) | np.isnan(ind.val20)
                 | np.isnan(p.mktcap) | np.isnan(ind.val_prev20) | np.isnan(p.open))
    reasons = {"가격 이력 부족(250거래일 미만, 거래정지 구간 포함)": listed & ~price_ok}
    ok = listed & price_ok
    if variant in "BCD":
        flow_ok = ~np.isnan(ind.flow_sum)
        reasons["KIS 수급 없음(아직 안 받았거나 20일 중 빠진 날)"] = ok & ~flow_ok
        ok &= flow_ok
    if variant in "CD":
        fin_ok = ~np.isnan(p.fin_ok[:, :, 0]) & ~np.isnan(p.fin_ok[:, :, 1]) & ~np.isnan(p.fin_ok[:, :, 2])
        reasons["DART 재무 없음(4분기 연속 자료·공시 전)"] = ok & ~fin_ok
        ok &= fin_ok
    if variant == "D":
        idx_ok = np.zeros((T, N), dtype=bool)
        for mk in ("KOSPI", "KOSDAQ"):
            col = p.market == mk
            good = ~np.isnan(p.index_close.get(mk, np.full(T, np.nan))) & ~np.isnan(ind.idx_ma.get(mk, np.full(T, np.nan)))
            idx_ok[:, col] = good[:, None]
        reasons["지수 없음(120일선 계산 전)"] = ok & ~idx_ok
        ok &= idx_ok
    return listed, ok, reasons


def coverage_of(p: Panel, ok: np.ndarray, listed: np.ndarray, reasons: dict, variant: str) -> Coverage:
    rows = np.where(ok.any(axis=1))[0]
    return Coverage(p.dates[rows[0]] if len(rows) else None, p.dates[rows[-1]] if len(rows) else None,
                    int(ok.any(axis=0).sum()), int(ok.sum()), int(listed.sum()),
                    {k: int(v.sum()) for k, v in reasons.items()},
                    variant in "BCD" and not p.kis_complete)


def signals(p: Panel, ind: Indicators, cfg: Config, variant: str, ok: np.ndarray, flow_top: float):
    """장 마감 후 진입 신호 (T, N) bool과 순위 점수."""
    c = p.close
    with np.errstate(invalid="ignore", divide="ignore"):
        filt = ok & (p.mktcap >= cfg.f("MIN_MKTCAP_KRW")) & (ind.val20 >= cfg.f("MIN_VALUE20_KRW"))
        trend = (c > ind.ma60) & (ind.ma60 > ind.ma120) & (ind.ma60 > ind.ma60_lag20) & (ind.pos250 >= cfg.f("POS250_MIN_PCT"))
        value_x = p.value / ind.val_prev20
        sig = filt & trend
        score = value_x.copy()
        if variant in "BCD":
            flow_score = ind.flow_sum * 1e6 / p.mktcap
            fs = np.where(filt, flow_score, np.nan)
            thr = np.full(len(p.dates), np.nan)
            has = ~np.all(np.isnan(fs), axis=1)
            thr[has] = np.nanpercentile(fs[has], 100 - flow_top, axis=1)
            sig &= (flow_score >= thr[:, None]) & (value_x >= cfg.f("VALUE_SPIKE_X"))
            score = flow_score
        if variant in "CD":
            ttm, li, eq = p.fin_ok[:, :, 0], p.fin_ok[:, :, 1], p.fin_ok[:, :, 2]
            sig &= (ttm > 0) & (eq > 0) & (li / eq * 100 < cfg.f("DEBT_RATIO_MAX_PCT"))
        if variant == "D":
            for mk in ("KOSPI", "KOSDAQ"):
                col = p.market == mk
                down = p.index_close[mk] < ind.idx_ma[mk]
                sig[:, col] &= ~down[:, None]
    return sig, score


@dataclass
class Result:
    variant: str
    entry: str
    exit: str
    stop_pct: float
    flow_top: float
    equity: list = field(default_factory=list)  # (date, equity)
    trades: list = field(default_factory=list)
    limit_orders: int = 0
    limit_fills: int = 0
    missed_ret20: list = field(default_factory=list)
    skipped_cash: int = 0
    skipped_theme: int = 0


def simulate(p: Panel, ind: Indicators, cfg: Config, variant: str, sig: np.ndarray, score: np.ndarray,
             entry: str, exit_: str, stop_pct: float, flow_top: float, start_idx: int) -> Result:
    T, N = p.close.shape
    R = Result(variant, entry, exit_, stop_pct, flow_top)
    cash = cfg.f("INITIAL_CASH_KRW")
    slip, bfee, sfee = cfg.f("SLIPPAGE_PCT") / 100, cfg.f("BUY_FEE_PCT") / 100, cfg.f("SELL_FEE_PCT") / 100
    stop = stop_pct / 100
    max_pos, max_theme, wait = cfg.i("MAX_POSITIONS"), cfg.i("MAX_PER_THEME"), cfg.i("LIMIT_WAIT_DAYS")
    pos: dict[int, dict] = {}       # j -> {shares, cost, entry_t, peak, last_close, signal_t}
    orders: dict[int, dict] = {}    # j -> {signal_t, limit, left}
    sells: set[int] = set()         # 다음 날 시가 매도 예약
    last_close = np.full(N, np.nan)
    prev_equity = cash

    def sell(j, t, px, reason):
        nonlocal cash
        q = pos.pop(j)
        execp = px * (1 - slip)
        proceeds = q["shares"] * execp
        cost = proceeds * (sfee + tax_pct(p.dates[t]) / 100)
        cash += proceeds - cost
        pnl = proceeds - cost - q["cost"]
        R.trades.append({"code": p.codes[j], "name": p.name[j], "signal_date": p.dates[q["signal_t"]],
                         "entry_date": p.dates[q["entry_t"]], "entry_price": round(q["entry_px"], 2),
                         "shares": q["shares"], "exit_date": p.dates[t], "exit_price": round(execp, 2),
                         "reason": reason, "pnl_krw": round(pnl), "ret_pct": round(pnl / q["cost"] * 100, 3),
                         "hold_days": t - q["entry_t"]})
        sells.discard(j)

    def themes_of(j):
        return p.themes.get(p.codes[j], [])

    for t in range(start_idx, T):
        o, h, l, c = p.open[t], p.high[t], p.low[t], p.close[t]
        # 1. 상장폐지 청산
        for j in [j for j in pos if p.last_index[j] < t]:
            sell(j, int(p.last_index[j]), pos[j]["last_close"], "상장폐지(마지막 종가)")
        # 2. 장중 손절 (나)
        if exit_ == "na":
            for j in list(pos):
                q = pos[j]
                if q["entry_t"] >= t or np.isnan(o[j]):
                    continue
                sp = q["peak"] * (1 - stop)
                if o[j] <= sp:
                    sell(j, t, float(o[j]), "장중 손절(시가)")
                elif l[j] <= sp:
                    sell(j, t, sp, "장중 손절")
        # 3. 예약 매도 (시가)
        for j in list(sells):
            if j in pos and not np.isnan(o[j]):
                sell(j, t, float(o[j]), pos[j].get("why", "종가 판단"))
        # 4. 매수 체결
        equity_ref = prev_equity
        for j in list(orders):
            od = orders[j]
            if np.isnan(o[j]):
                od["left"] -= 1
            else:
                if entry == "1":
                    px = float(o[j])
                elif l[j] <= od["limit"]:
                    px = min(od["limit"], float(o[j]))
                else:
                    px = None
                if px is not None and not (px > 0):
                    px = None  # 0 이하·NaN 가격으로는 체결하지 않는다
                if px is not None:
                    unit = px * (1 + slip) * (1 + bfee)
                    shares = int(min(equity_ref / max_pos, cash) // unit)
                    if shares <= 0:
                        R.skipped_cash += 1
                    else:
                        cost = shares * px * (1 + slip) * (1 + bfee)
                        cash -= cost
                        pos[j] = {"shares": shares, "cost": cost, "entry_t": t, "peak": float(c[j]) if not np.isnan(c[j]) else px,
                                  "last_close": px, "signal_t": od["signal_t"], "entry_px": px * (1 + slip)}
                        if entry != "1":
                            R.limit_fills += 1
                    del orders[j]
                    continue
                od["left"] -= 1
            if od["left"] <= 0 or entry == "1":
                if entry != "1":  # 놓친 신호: 신호일 종가 대비 20거래일 뒤 수익률 (지정가 진입의 기회비용)
                    st = od["signal_t"]
                    if st + 20 < T and not np.isnan(p.close[st + 20, j]) and not np.isnan(p.close[st, j]):
                        R.missed_ret20.append(float(p.close[st + 20, j] / p.close[st, j] - 1))
                del orders[j]
        # 5. 평가
        for j, q in pos.items():
            if not np.isnan(c[j]):
                q["last_close"] = float(c[j])
                q["peak"] = max(q["peak"], float(c[j]))
        equity = cash + sum(q["shares"] * q["last_close"] for q in pos.values())
        R.equity.append((p.dates[t], equity))
        prev_equity = equity
        # 6. 청산 판단 (종가)
        for j, q in pos.items():
            if np.isnan(c[j]):
                continue
            if exit_ == "ga" and c[j] <= q["peak"] * (1 - stop):
                q["why"] = f"최고 종가 대비 -{stop_pct:g}%"
                sells.add(j)
            elif not np.isnan(ind.ma60[t, j]) and c[j] < ind.ma60[t, j]:
                q["why"] = "종가 < 60일선"
                sells.add(j)
        # 7. 진입 판단
        free = max_pos - len(pos) - len(orders)
        if free <= 0:
            continue
        cand = np.nonzero(sig[t])[0]
        if len(cand) == 0:
            continue
        cand = [int(j) for j in cand if j not in pos and j not in orders]
        cand.sort(key=lambda j: (-(score[t, j] if not np.isnan(score[t, j]) else -1e18), p.codes[j]))
        theme_cnt: dict[str, int] = {}
        for j in list(pos) + list(orders):
            for th in themes_of(j):
                theme_cnt[th] = theme_cnt.get(th, 0) + 1
        for j in cand:
            if free <= 0:
                break
            ths = themes_of(j)
            if any(theme_cnt.get(th, 0) >= max_theme for th in ths):
                R.skipped_theme += 1
                continue
            if entry == "1":
                limit = math.inf
            elif entry == "2":
                limit = float(ind.ma20[t, j])
            elif entry == "3":
                limit = float(c[j] - cfg.f("ATR_MULT") * ind.atr[t, j])
            else:
                limit = float(ind.hi250[t, j])
            if math.isnan(limit):
                continue
            orders[j] = {"signal_t": t, "limit": limit, "left": wait}
            if entry != "1":
                R.limit_orders += 1
            for th in ths:
                theme_cnt[th] = theme_cnt.get(th, 0) + 1
            free -= 1
    # 끝: 남은 보유는 마지막 날 종가로 평가 청산(비용 포함)해 통계에 넣는다. 자산 곡선은 그대로.
    for j in list(pos):
        sell(j, T - 1, pos[j]["last_close"], "기간 끝(평가 청산)")
    return R


# ------------------------------------------------------------------ 지표
def metrics(R: Result, start: str | None = None, end: str | None = None) -> dict:
    eq = [(d, v) for d, v in R.equity if (start is None or d >= start) and (end is None or d <= end)]
    tr = [x for x in R.trades if (start is None or x["entry_date"] >= start) and (end is None or x["entry_date"] <= end)]
    out = {"trades": len(tr)}
    if len(eq) >= 2:
        v0, v1 = eq[0][1], eq[-1][1]
        years = max(len(eq) / 245.0, 1e-9)
        out["cagr_pct"] = ((v1 / v0) ** (1 / years) - 1) * 100 if v0 > 0 and v1 > 0 else None
        peak, mdd = eq[0][1], 0.0
        for _, v in eq:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        out["mdd_pct"] = mdd * 100
        out["total_pct"] = (v1 / v0 - 1) * 100
    wins = [x["ret_pct"] for x in tr if x["ret_pct"] > 0]
    losses = [x["ret_pct"] for x in tr if x["ret_pct"] <= 0]
    out["win_rate_pct"] = len(wins) / len(tr) * 100 if tr else None
    out["avg_win_pct"] = sum(wins) / len(wins) if wins else None
    out["avg_loss_pct"] = sum(losses) / len(losses) if losses else None
    out["avg_hold_days"] = sum(x["hold_days"] for x in tr) / len(tr) if tr else None
    out["limit_fill_pct"] = R.limit_fills / R.limit_orders * 100 if R.limit_orders else None
    out["missed_ret20_pct"] = sum(R.missed_ret20) / len(R.missed_ret20) * 100 if R.missed_ret20 else None
    return out


def yearly(series: list[tuple[str, float]]) -> dict[str, float]:
    out, prev = {}, None
    by_year: dict[str, float] = {}
    first: dict[str, float] = {}
    for d, v in series:
        first.setdefault(d[:4], v)
        by_year[d[:4]] = v
    for y in sorted(by_year):
        base = prev if prev is not None else first[y]
        out[y] = (by_year[y] / base - 1) * 100 if base else None
        prev = by_year[y]
    return out
