import sqlite3
from datetime import date, timedelta

import pytest

from toss import datastore as ds
from toss.trade import engine, store as tstore, ticks
from toss.trade.risk import Account, Risk
from toss.trade.settings import TradeSettings
from toss.trade.strategy import Bar, CodeState, OpeningRangeBreakout
from toss.trade.watchlist import build_watchlist


# ---------------------------------------------------------------- 호가
def test_ticks():
    assert [ticks.tick(p) for p in (1999, 2000, 4999, 5000, 19999, 20000, 49999, 50000, 199999, 200000, 499999, 500000)] \
        == [1, 5, 5, 10, 10, 50, 50, 100, 100, 500, 500, 1000]
    assert ticks.up(1999) == 2000 and ticks.down(2000) == 1999 and ticks.up(10000, 2) == 10020
    assert ticks.upper_limit(10000) == 13000 and ticks.lower_limit(10010) == 7010
    assert ticks.upper_limit(15550) == 20200   # 20,215 → 2만 이상 호가 50원 아래로 내림


# ---------------------------------------------------------------- 전략
def bars_or(day_bars):
    """09:00~09:29 시가 범위: 고가 10,100 저가 9,900"""
    return [Bar(t, 10000, 10100 if t == 545 else 10050, 9900 if t == 550 else 9950, 10000, 100) for t in range(540, 570)]


def run_bars(bars, s=None, prev_close=None):
    s = s or TradeSettings()
    strat = OpeningRangeBreakout(s)
    st = CodeState("A", prev_close=prev_close)
    out = []
    for b in bars:
        was = st.in_pos
        sig = strat.on_bar(st, b, True)
        if sig and sig.kind == "buy":
            strat.on_filled_buy(st, sig.price, sig.t)
            out.append(sig)
            same = strat.check_same_bar_stop(st, b)
            if same:
                st.in_pos = False
                out.append(same)
        elif sig and was:
            st.in_pos = False
            out.append(sig)
    return st, out


def test_breakout_entry_then_take_profit():
    bars = bars_or(None) + [Bar(570, 10050, 10120, 10040, 10110, 100), Bar(571, 10110, 10500, 10100, 10450, 100)]
    st, sig = run_bars(bars)
    assert st.or_high == 10100 and st.or_low == 9900
    assert (sig[0].kind, sig[0].price, sig[0].t) == ("buy", 10120, 570)    # 10,100 + 1호가 + 슬리피지 1호가
    assert st.stop_px == max(9900, ticks.ceil_tick(10120 * 0.97))           # 9,820 vs 9,900 → 9,900
    assert sig[1].reason == "take_profit" and sig[1].price == ticks.ceil_tick(10120 * 1.03)


def test_no_fill_if_price_not_reached_and_same_bar_stop():
    # 고가가 10,110이라 진입가 10,120에 닿지 않음 → 체결 없음
    st, sig = run_bars(bars_or(None) + [Bar(570, 10050, 10110, 10000, 10100, 100)])
    assert sig == []
    # 진입한 봉에서 손절가 아래까지 내려감 → 같은 봉 손절 (익절은 인정 안 함)
    st, sig = run_bars(bars_or(None) + [Bar(570, 10050, 10600, 9800, 9850, 100)])
    assert [x.reason for x in sig] == ["entry", "stop"] and sig[1].price == ticks.down(9900)


def test_stop_and_tp_same_bar_is_stop_and_gap_down():
    bars = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100), Bar(571, 10110, 10600, 9800, 9900, 100)]
    st, sig = run_bars(bars)
    assert sig[1].reason == "stop"
    bars = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100), Bar(571, 9500, 9600, 9400, 9450, 100)]
    st, sig = run_bars(bars)
    assert sig[1].reason == "stop" and sig[1].price == ticks.down(9500)    # 갭 하락은 시가에


def test_time_exit_and_limit_up_and_entry_deadline():
    bars = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100)] + \
        [Bar(t, 10110, 10150, 10100, 10120, 100) for t in range(571, 920)]
    st, sig = run_bars(bars)
    assert sig[-1].reason == "time_exit" and sig[-1].t == 919
    # 전일 종가 7,800 → 상한가 10,140. 진입가 10,120은 가능, 전일 7,780 → 상한가 10,110이면 불가
    st, sig = run_bars(bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100)], prev_close=7780)
    assert sig == [] and any("상한가" in n for n in st.notes)
    s = TradeSettings(ENTRY_UNTIL=600)
    st, sig = run_bars(bars_or(None) + [Bar(601, 10050, 10130, 10040, 10110, 100)], s=s)
    assert sig == []


# ---------------------------------------------------------------- 리스크
def test_risk_sizing_and_limits():
    s = TradeSettings()
    r = Risk(s)
    acc = Account(cash=1_000_000)
    assert r.size(acc, 10120) == (19, "")                       # 20만 원 / 10,120
    assert r.size(acc, 250_000) == (0, "price_above_order_krw")
    acc.open_positions = 5
    assert r.size(acc, 10000)[1] == "max_positions"
    acc = Account(cash=150_000)
    assert r.size(acc, 10000)[0] == 14                          # 현금 부족 → 살 수 있는 만큼
    acc = Account(cash=1_000_000)
    assert r.after_exit(acc, -30_000) is False and r.after_exit(acc, -21_000) is True
    assert acc.halted and r.size(acc, 10000)[1].startswith("halted")
    acc.cum_realized = -500_000
    r.new_day(acc)
    assert acc.disabled and not acc.halted and r.size(acc, 10000)[1].startswith("disabled")


# ---------------------------------------------------------------- 관심종목 + replay
def trading_days(n, start=date(2026, 9, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DAYS = trading_days(25)


def make_store(tmp_path, codes=("A", "B", "C", "D"), surge=("A", "B"), halt=(), split=()):
    store = tmp_path / "toss_data"
    d = ds.connect_daily(store)
    d.executemany("INSERT INTO trading_day VALUES (?, 'x')", [(x,) for x in DAYS])
    rows, adj = [], []
    for code in codes:
        for i, day in enumerate(DAYS[:-1]):
            last = i == len(DAYS) - 2
            vol = 100_000 * (8 if last and code in surge else 1)
            if code in halt and i == 10:
                vol = 0
            close = 10000
            rows.append((code, day, close, close + 50, close - 50, close, vol, close * vol if vol else 0, "daily_close"))
            adj.append((code, day, close * (0.5 if code in split and i < 12 else 1.0), (0.5 if code in split and i < 12 else 1.0), "x"))
    d.executemany("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?)", rows)
    d.executemany("INSERT INTO adj_daily VALUES (?,?,?,?,?)", adj)
    d.commit()
    return store, d


def test_watchlist_rules_and_exclusions(tmp_path):
    store, d = make_store(tmp_path, codes=("A", "B", "C", "D", "E"), surge=("A", "B", "D", "E"), halt=("D",), split=("E",))
    wl, st = build_watchlist(d, DAYS[-1], TradeSettings())
    assert [w.code for w in wl] == ["A", "B"] and wl[0].vr == pytest.approx(8.0)
    assert st["excluded"]["halt"] == 1 and st["excluded"]["corporate_action"] == 1 and st["excluded"]["no_signal"] == 1
    wl, st = build_watchlist(d, DAYS[-1], TradeSettings(WATCH_MAX=1))
    assert [w.code for w in wl] == ["A"]
    wl, st = build_watchlist(d, DAYS[5], TradeSettings())       # 이력 부족
    assert wl == [] and "history_days" in st["excluded"]


def write_minute(store, day, bars_by_code):
    con = ds.connect_minute(store, day)
    for code, bars in bars_by_code.items():
        con.executemany("INSERT INTO bar VALUES (?,?,?,?,?,?,?)", [(code, b.t, b.open, b.high, b.low, b.close, b.volume) for b in bars])
        con.execute("INSERT INTO code_done VALUES (?,?,0,'x')", (code, len(bars)))
    con.execute("INSERT OR REPLACE INTO file_meta VALUES ('complete','1')")
    con.commit()
    con.close()


def test_replay_end_to_end(tmp_path):
    store, d = make_store(tmp_path)
    day, prev = DAYS[-1], DAYS[-2]
    win = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100), Bar(571, 10110, 10500, 10100, 10450, 100)]
    lose = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100), Bar(571, 10110, 10120, 9500, 9600, 100)]
    write_minute(store, day, {"A": win, "B": lose})
    write_minute(store, prev, {"A": [Bar(930, 10000, 10000, 10000, 10000, 5)], "B": [Bar(929, 10000, 10000, 10000, 10000, 5)]})
    tcon = tstore.connect(tmp_path / "trade.sqlite")
    s = TradeSettings()
    tstore.start_run(tcon, "r1", "replay", "now", day, day, vars(s))
    logs = []
    summ = engine.replay(d, store, [day], s, tcon, "r1", logs.append, DAYS)
    trades = tcon.execute("SELECT code, entry_px, exit_px, qty, exit_reason, net_krw FROM trade ORDER BY code").fetchall()
    assert [(t[0], t[4]) for t in trades] == [("A", "take_profit"), ("B", "stop")]
    a, b = trades
    assert a[3] == 19 and a[5] == pytest.approx((a[2] - a[1]) * 19 - a[1] * 19 * s.BUY_COST_RATE - a[2] * 19 * s.SELL_COST_RATE)
    assert summ["trades"] == 2 and tcon.execute("SELECT n_watch FROM day").fetchone()[0] == 2
    r = engine.report(tcon, "r1", logs.append)
    assert r["trades"] == 2 and r["win_rate"] == 0.5
    assert tstore.live_cum_realized(tcon) == 0.0                 # replay는 실전 누적 손실에 안 셈


def test_replay_daily_loss_limit_flattens(tmp_path):
    store, d = make_store(tmp_path)
    day = DAYS[-1]
    hold = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100)] + [Bar(t, 10110, 10150, 10100, 10120, 100) for t in range(571, 920)]
    crash = bars_or(None) + [Bar(570, 10050, 10130, 10040, 10110, 100), Bar(575, 10110, 10120, 9000, 9100, 100)]
    write_minute(store, day, {"A": hold, "B": crash})
    tcon = tstore.connect(tmp_path / "trade.sqlite")
    s = TradeSettings(DAILY_LOSS_KRW=4_000)    # B 손절 손실 약 4,800원(비용 포함) > 한도
    engine.replay(d, store, [day], s, tcon, "r2", lambda m: None, DAYS)
    reasons = dict(tcon.execute("SELECT code, exit_reason FROM trade"))
    assert reasons == {"B": "stop", "A": "loss_limit"}
    assert tcon.execute("SELECT halted FROM day").fetchone()[0].startswith("하루 손실")
