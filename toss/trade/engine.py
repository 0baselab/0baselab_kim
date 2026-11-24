"""매매 엔진. 1단계에는 replay(저장된 분봉 재생 + 가상 체결)만 있다.

replay 계좌는 시드에서 시작해 날짜를 이어 가며 손익이 쌓인다(누적 손실 한도도 그대로 적용).
하루 흐름: 관심종목 → 09:00부터 1분씩 모든 관심종목 봉을 순서대로 전략에 넣음 → 15:19 청산 → 기록.
같은 분에 여러 종목 신호가 나면 관심종목 순위 순서로 처리한다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .. import datastore as ds
from . import store
from .risk import Account, Risk
from .settings import TradeSettings
from .strategy import Bar, CodeState, OpeningRangeBreakout, Signal
from .watchlist import build_watchlist

Log = Callable[[str], None]


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def load_day_bars(store_dir: Path, day: str, codes: list[str], t_from: int, t_to: int) -> dict[str, list[Bar]]:
    con = _ro(ds.minute_path(store_dir, day))
    try:
        q = ",".join("?" * len(codes))
        out: dict[str, list[Bar]] = {c: [] for c in codes}
        for code, t, o, h, l, c, v in con.execute(
                f"SELECT code, t, open, high, low, close, volume FROM bar WHERE code IN ({q}) AND t BETWEEN ? AND ?"
                " ORDER BY code, t", (*codes, t_from, t_to)):
            out[code].append(Bar(t, o, h, l, c, v))
        return out
    finally:
        con.close()


def prev_regular_close(store_dir: Path, day: str, codes: list[str]) -> dict[str, float]:
    """전일 분봉 파일에서 정규장 종가 = 15:30 이전에 시작한 마지막 거래 봉 종가 (보통주 t=930, ETF 등 t=929).
    그 종목이 받아져 있는(code_done) 경우만 쓴다."""
    if not ds.minute_path(store_dir, day).exists():
        return {}
    con = _ro(ds.minute_path(store_dir, day))
    try:
        q = ",".join("?" * len(codes))
        out = {}
        for code, c in con.execute(
                f"SELECT code, close FROM bar b WHERE code IN ({q}) AND t = (SELECT MAX(t) FROM bar b2 WHERE"
                " b2.code=b.code AND b2.t <= 930 AND b2.volume > 0)"
                " AND code IN (SELECT code FROM code_done)", codes):
            out[code] = c
        return out
    finally:
        con.close()


def _done_codes(store_dir: Path, day: str) -> set:
    p = ds.minute_path(store_dir, day)
    if not p.exists():
        return set()
    con = _ro(p)
    try:
        return {r[0] for r in con.execute("SELECT code FROM code_done")}
    finally:
        con.close()


def replay_ready(dcon, store_dir: Path, day: str, prev_day: str | None, s: TradeSettings) -> bool:
    """재생 가능한 날: 그날 분봉 파일이 완성됐거나, 그날 W7 관심종목이 그날·전날 파일에 모두 받아져 있음.
    (전 종목 완성을 기다리지 않고 우선 수집만으로 재생하기 위함)"""
    if ds.minute_status(store_dir, day) == "complete" and (not prev_day or ds.minute_status(store_dir, prev_day) == "complete"):
        return True
    wl = [w.code for w in build_watchlist(dcon, day, s)[0]]
    if not wl or not prev_day:
        return False
    return set(wl) <= _done_codes(store_dir, day) and set(wl) <= _done_codes(store_dir, prev_day)


def _cost(s: TradeSettings, side: str, px: int, qty: int) -> float:
    return px * qty * (s.BUY_COST_RATE if side == "buy" else s.SELL_COST_RATE)


def replay_day(dcon, store_dir: Path, day: str, prev_day: str | None, s: TradeSettings, acc: Account,
               tcon, run_id: str, log: Log) -> dict:
    risk = Risk(s)
    risk.new_day(acc)
    strat = OpeningRangeBreakout(s)
    wl, wstats = build_watchlist(dcon, day, s)
    with tcon:
        tcon.executemany("INSERT OR REPLACE INTO watch VALUES (?,?,?,?,?,?,?,?,?)",
                         [(run_id, day, w.code, w.rank, w.vr, w.r1, int(w.new_high20), w.prev_close, w.value20) for w in wl])
    res = {"date": day, "watch": len(wl), "trades": [], "rejected": {}, "note": ""}
    if acc.disabled:
        res["note"] = acc.disabled
    if not wl:
        res["note"] = res["note"] or f"관심종목 없음 {wstats.get('excluded')}"
        return res
    codes = [w.code for w in wl]
    bars = load_day_bars(store_dir, day, codes, s.OR_START, s.EXIT_T)
    pclose = prev_regular_close(store_dir, prev_day, codes) if prev_day else {}
    if not pclose:
        res["note"] = "전일 분봉 파일 없음 → 상한가 매수 불가 검사 생략"
    states = {c: CodeState(c, prev_close=pclose.get(c)) for c in codes}
    by_t: dict[int, list[tuple[str, Bar]]] = {}
    for c in codes:  # 순위 순서 유지
        for b in bars[c]:
            by_t.setdefault(b.t, []).append((c, b))
    pos: dict[str, dict] = {}
    seq = [0]

    def fill(code: str, sig: Signal, qty: int) -> None:
        seq[0] += 1
        cost = _cost(s, sig.kind, sig.price, qty)
        with tcon:
            tcon.execute("INSERT INTO fill VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (run_id, day, seq[0], code, sig.kind, sig.t, sig.price, qty, cost, sig.reason))
        if sig.kind == "buy":
            acc.cash -= sig.price * qty + cost
            acc.orders_today += 1
            acc.open_positions += 1
            pos[code] = {"qty": qty, "px": sig.price, "t": sig.t, "cost": cost}
            strat.on_filled_buy(states[code], sig.price, sig.t)
        else:
            p = pos.pop(code)
            acc.cash += sig.price * qty - cost
            acc.orders_today += 1
            acc.open_positions -= 1
            st = states[code]
            st.in_pos = False
            gross = (sig.price - p["px"]) * qty
            net = gross - p["cost"] - cost
            tr = (run_id, day, code, p["t"], p["px"], sig.t, sig.price, qty, gross, p["cost"] + cost,
                  net, net / (p["px"] * qty), sig.reason)
            with tcon:
                tcon.execute("INSERT INTO trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tr)
            res["trades"].append(tr)
            if risk.after_exit(acc, net):
                log(f"  {day} {acc.halted} → 신규 진입 중단, 전량 청산")
                for oc in list(pos):
                    fs = strat.force_exit(states[oc], sig.t, "loss_limit")
                    if fs:
                        fill(oc, fs, pos[oc]["qty"])

    for t in sorted(by_t):
        for code, b in by_t[t]:
            st = states[code]
            was_in = st.in_pos
            sig = strat.on_bar(st, b, can_enter=not acc.halted and not acc.disabled)
            if sig is None:
                continue
            if sig.kind == "buy":
                qty, why = risk.size(acc, sig.price)
                if not qty:
                    res["rejected"][why.split(":")[0]] = res["rejected"].get(why.split(":")[0], 0) + 1
                    continue
                fill(code, sig, qty)
                same = strat.check_same_bar_stop(st, b)
                if same:
                    fill(code, same, pos[code]["qty"])
            elif was_in and code in pos:
                fill(code, sig, pos[code]["qty"])
    for code in list(pos):  # 15:19 봉이 없던 종목(거래정지 등) → 마지막 종가로 청산
        fs = strat.force_exit(states[code], s.EXIT_T, "eod")
        if fs:
            fill(code, fs, pos[code]["qty"])
    return res


def replay(dcon, store_dir: Path, days: list[str], s: TradeSettings, tcon, run_id: str, log: Log,
           all_days: list[str]) -> dict:
    """days: 재생할 날짜(완성된 분봉 파일이 있는 날). all_days: 전 거래일 목록(전일 찾기용)."""
    acc = Account(cash=s.SEED_KRW)
    summary = {"days": [], "trades": 0, "net": 0.0}
    for day in days:
        i = all_days.index(day)
        prev = all_days[i - 1] if i > 0 else None
        r = replay_day(dcon, store_dir, day, prev, s, acc, tcon, run_id, log)
        net = sum(t[10] for t in r["trades"])
        with tcon:
            tcon.execute("INSERT OR REPLACE INTO day VALUES (?,?,?,?,?,?,?,?,?)",
                         (run_id, day, r["watch"], len(r["trades"]), net, acc.cum_realized, acc.halted,
                          json.dumps(r["rejected"], ensure_ascii=False), r["note"]))
        wins = sum(1 for t in r["trades"] if t[10] > 0)
        log(f"  {day}: 관심 {r['watch']}, 거래 {len(r['trades'])} (이익 {wins}), 손익 {net:+,.0f}원, "
            f"누적 {acc.cum_realized:+,.0f}원" + (f", 중단: {acc.halted}" if acc.halted else "")
            + (f", 거절 {r['rejected']}" if r["rejected"] else "") + (f" ({r['note']})" if r["note"] else ""))
        summary["days"].append(day)
        summary["trades"] += len(r["trades"])
        summary["net"] += net
    summary["cum"] = acc.cum_realized
    summary["disabled"] = acc.disabled
    return summary


def report(tcon, run_id: str, log: Log) -> dict:
    rows = tcon.execute("SELECT net_krw, net_pct, exit_reason FROM trade WHERE run_id=?", (run_id,)).fetchall()
    days = tcon.execute("SELECT date, net_krw, cum_krw FROM day WHERE run_id=? ORDER BY date", (run_id,)).fetchall()
    if not rows:
        log("[report] 거래 없음")
        return {"trades": 0}
    n = len(rows)
    wins = [r for r in rows if r[0] > 0]
    pcts = sorted(r[1] for r in rows)
    peak, mdd = 0.0, 0.0
    for _, _, cum in days:
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    by = {}
    for net, _, why in rows:
        x = by.setdefault(why, [0, 0.0])
        x[0] += 1
        x[1] += net
    out = {"trades": n, "win_rate": len(wins) / n, "net_krw": sum(r[0] for r in rows),
           "avg_pct": sum(pcts) / n, "median_pct": pcts[n // 2], "mdd_krw": mdd, "days": len(days),
           "by_reason": by}
    log(f"[report] 거래 {n}건, 승률 {out['win_rate'] * 100:.1f}%, 1건 평균 {out['avg_pct'] * 100:+.2f}% "
        f"(중앙 {out['median_pct'] * 100:+.2f}%), 총손익 {out['net_krw']:+,.0f}원, 최대 낙폭 {mdd:,.0f}원, {len(days)}일")
    for why, (k, net) in sorted(by.items()):
        log(f"    청산 사유 {why}: {k}건, {net:+,.0f}원")
    return out
