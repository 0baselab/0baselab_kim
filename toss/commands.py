"""측정 명령 본체. 결과는 log()로 화면·로그 파일에 남기고, 요약은 results 표에 저장해 report에서 쓴다."""
from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from datetime import date as Date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import store
from .candles import best_alignment, check_day, compare_bars, page_candles, parse_ts, session_counts
from .client import TossClient, sensitive

Log = Callable[[str], None]
CANDLES = "/api/v1/candles"


def _iso(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def _now_s(now: Callable[[], datetime]) -> str:
    return now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- auth
def cmd_auth(client: TossClient, con, log: Log, now) -> dict:
    client.token()
    log(f"[auth] 토큰 준비 완료 (이번 실행 발급 {client.token_issues}회, 0회면 캐시 재사용)")
    accounts = client.get("/api/v1/accounts").get("result") or []
    for a in accounts:  # 계좌번호·accountSeq는 화면·로그에 쓰지 않는다
        sensitive(a.get("accountNo"))
    log(f"[auth] 계좌 {len(accounts)}개")
    for i, a in enumerate(accounts, 1):
        log(f"  {i}. 유형 {a.get('accountType')}, accountSeq (가림), 계좌번호 (가림)")
    out = {"accounts": len(accounts), "types": [a.get("accountType") for a in accounts]}
    store.put_result(con, "auth", out, _now_s(now))
    return out


# ---------------------------------------------------------------- candles-depth
def _depth(client, path: str, params: dict, log: Log, now, max_calls: int, max_years: float, label: str) -> dict:
    limit = now() - timedelta(days=365 * max_years)
    t0 = time.monotonic()

    def prog(r):
        if r.calls % 100 == 0:
            log(f"  {label}: 호출 {r.calls}, 가장 오래된 봉 {r.oldest}")
    r = page_candles(client, path, params, max_calls, stop_when=lambda t: t < limit, keep=False, on_page=prog)
    el = time.monotonic() - t0
    out = {"oldest": r.oldest.isoformat() if r.oldest else None, "newest": r.newest.isoformat() if r.newest else None,
           "bars": r.n_bars, "calls": r.calls, "seconds": round(el, 1), "trading_days": len(r.dates),
           "stop": r.stop}
    log(f"[{label}] 가장 오래된 봉 {out['oldest']}, 최신 {out['newest']}")
    log(f"[{label}] 봉 {r.n_bars:,}개, 날짜 {len(r.dates)}일, 호출 {r.calls}회, {el:.1f}초, 멈춘 이유: {r.stop}")
    return out


def _adjusted_compare(client, symbol: str, interval: str, log: Log) -> dict:
    a = (client.get(CANDLES, {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "true"})
         .get("result") or {}).get("candles") or []
    b = (client.get(CANDLES, {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "false"})
         .get("result") or {}).get("candles") or []
    bm = {x["timestamp"]: x for x in b}
    # 가장 최근 봉은 장중에 계속 바뀌므로(두 번 조회 사이에 값이 달라짐) 비교에서 뺀다
    newest = max([x["timestamp"] for x in a] + [x["timestamp"] for x in b], default=None)
    common = [x for x in a if x["timestamp"] in bm and x["timestamp"] != newest]
    diff = [x for x in common if any(x.get(f) != bm[x["timestamp"]].get(f)
                                     for f in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume"))]
    out = {"interval": interval, "adjusted_true": len(a), "adjusted_false": len(b), "common": len(common),
           "different": len(diff),
           "example": ({"timestamp": diff[0]["timestamp"], "true": diff[0].get("closePrice"),
                        "false": bm[diff[0]["timestamp"]].get("closePrice")} if diff else None)}
    log(f"[adjusted {interval}] true {len(a)}봉 / false {len(b)}봉, 같은 시각 {len(common)}봉 중 값이 다른 봉 {len(diff)}"
        + (f" (예: {out['example']})" if diff else ""))
    return out


def cmd_candles_depth(client, con, log: Log, now, symbol: str = "005930", max_calls: int = 3000,
                      max_years: float = 3) -> dict:
    log(f"[candles-depth] 종목 {symbol}, 상한: 호출 {max_calls}회 또는 {max_years:g}년 전")
    out = {"symbol": symbol}
    for iv, key in (("1m", "m1"), ("1d", "d1")):
        out[key] = _depth(client, CANDLES, {"symbol": symbol, "interval": iv, "count": 200, "adjusted": "true"},
                          log, now, max_calls, max_years, f"{iv} {symbol}")
    out["adjusted"] = [_adjusted_compare(client, symbol, "1m", log), _adjusted_compare(client, symbol, "1d", log)]
    store.put_result(con, "candles_depth", out, _now_s(now))
    return out


# ---------------------------------------------------------------- candles-compare
def pick_codes(danta: sqlite3.Connection, day: str, n: int) -> list[tuple[str, str, int]]:
    """그날 jobs 중 분봉 누적 거래대금 상위 n/2 + 하위 n/2. (code, 구분, 누적거래대금)"""
    rows = danta.execute(
        "SELECT j.code, MAX(m.acml_value_krw) v FROM jobs j JOIN minute_bar m ON m.code=j.code AND m.date=j.date"
        " WHERE j.date=? GROUP BY j.code HAVING v > 0 ORDER BY v DESC, j.code", (day,)).fetchall()
    k = max(1, n // 2)
    top = [(c, "상위", v) for c, v in rows[:k]]
    low = [(c, "하위", v) for c, v in rows[::-1][:k] if c not in {t[0] for t in top}]
    return top + low


def toss_day_bars(client, symbol: str, day: str, max_calls: int = 20) -> list[dict]:
    """그날(KST) 1분봉 전부. 봉 시작 시각(종료 - 1분) 기준으로 그날인 봉만."""
    start = datetime.fromisoformat(_iso(day) + "T00:00:00+09:00")
    r = page_candles(client, CANDLES, {"symbol": symbol, "interval": "1m", "count": 200, "adjusted": "false",
                                       "before": _iso(day) + "T23:59:59+09:00"}, max_calls,
                     stop_when=lambda t: t <= start)
    return [b for b in r.bars if (parse_ts(b["timestamp"]) - timedelta(minutes=1)).date() == start.date()]


def cmd_candles_compare(client, con, danta: sqlite3.Connection, log: Log, now, day: str, n: int = 10) -> dict:
    codes = pick_codes(danta, day, n)
    if not codes:
        raise SystemExit(f"danta.sqlite에 {day} 분봉이 있는 jobs가 없습니다. --date를 확인하세요.")
    log(f"[candles-compare] 날짜 {day}, 종목 {len(codes)}개 (KIS 분봉 누적 거래대금 상위·하위)")
    log("  KIS minute_bar.time과 토스 timestamp(봉 종료 시각) 맞추는 규칙: shift 0 = 그대로, shift 1 = 1분 빼서 시작 시각으로")
    per, tot = [], {0: [0, 0], 1: [0, 0]}  # shift → [종가 일치 수, 비교 봉 수]
    for code, grp, v in codes:
        bars = toss_day_bars(client, code, day)
        store.save_candles(con, code, "1m", False, bars)
        kis = danta.execute("SELECT time, open, high, low, close, volume FROM minute_bar WHERE code=? AND date=?"
                            " ORDER BY time", (code, day)).fetchall()
        best, other = best_alignment(bars, kis)
        for x in (best, other):
            if x["matched"]:
                tot[x["shift_min"]][0] += round((x["match_rate"]["close"] or 0) * x["matched"])
                tot[x["shift_min"]][1] += x["matched"]
        sess = session_counts(bars)
        ts = sorted(parse_ts(b["timestamp"]) for b in bars)
        r = {"code": code, "group": grp, "kis_acml_value": v, "toss_bars_all": len(bars), "sessions": sess,
             "toss_first_end": ts[0].strftime("%H:%M") if ts else None,
             "toss_last_end": ts[-1].strftime("%H:%M") if ts else None,
             "kis_bars": len(kis), "kis_first": kis[0][0] if kis else None, "kis_last": kis[-1][0] if kis else None,
             "best": best, "other": other}
        per.append(r)
        mr = best["match_rate"]
        f = lambda x: "-" if x is None else f"{x * 100:.1f}%"
        vd = "-" if best["volume_diff_pct"] is None else f"{best['volume_diff_pct']:+.2f}%"
        log(f"  {grp} {code}: 토스 {len(bars)}봉(장전 {sess['pre_0800_0900']}, 정규 {sess['regular_0900_1530']}, "
            f"장후 {sess['after_1531_2000']}), 종료시각 {r['toss_first_end']}~{r['toss_last_end']} / "
            f"KIS {len(kis)}봉 {r['kis_first']}~{r['kis_last']}")
        log(f"     shift {best['shift_min']} 기준 정규장 일치 봉 {best['matched']}: 시가 {f(mr['open'])}, 고가 {f(mr['high'])}, "
            f"저가 {f(mr['low'])}, 종가 {f(mr['close'])}, 거래량 차이 {vd}"
            f" (다른 shift 종가 일치 {f(other['match_rate']['close'])})")
    rates = {s: (tot[s][0] / tot[s][1] if tot[s][1] else None) for s in tot}
    shift = max((s for s in rates if rates[s] is not None), key=lambda s: rates[s], default=None)
    nxt = sum(1 for r in per if r["sessions"]["pre_0800_0900"] or r["sessions"]["after_1531_2000"])
    out = {"date": day, "symbols": per, "close_rate_by_shift": rates, "chosen_shift": shift,
           "overall_close_rate": rates.get(shift) if shift is not None else None, "symbols_with_nxt_hours": nxt}
    log(f"[candles-compare] 종가 일치율: shift 0 = {rates[0]}, shift 1 = {rates[1]} → 정렬 규칙 shift {shift}")
    log(f"[candles-compare] 08:00~09:00 또는 15:31~20:00 봉이 있는 종목 {nxt}/{len(per)} "
        "(있으면 토스 분봉에 NXT 거래가 섞여 있을 가능성)")
    store.put_result(con, "candles_compare", out, _now_s(now))
    return out


# ---------------------------------------------------------------- candles-check (KIS 없이 자체 점검)
def default_symbols(client, n: int = 10) -> list[str]:
    """거래대금 순위(1d)에서 상위 n/2 + 100위 쪽 n/2. 점검 표본일 뿐이라 순위 기준일이 점검 날짜와 달라도 된다."""
    rk = (client.get("/api/v1/rankings", {"type": "MARKET_TRADING_AMOUNT", "marketCountry": "KR",
                                          "duration": "1d", "count": 100}).get("result") or {}).get("rankings") or []
    syms = [x["symbol"] for x in rk if x.get("symbol")]
    k = max(1, n // 2)
    return list(dict.fromkeys(syms[:k] + syms[-k:]))


def toss_daily_bar(client, symbol: str, day: str) -> dict | None:
    r = page_candles(client, CANDLES, {"symbol": symbol, "interval": "1d", "count": 5, "adjusted": "false",
                                       "before": _iso(day) + "T23:59:59+09:00"}, 1)
    return next((b for b in r.bars if parse_ts(b["timestamp"]).date().isoformat() == _iso(day)), None)


def previous_business_day(client) -> str:
    """장 운영 정보(market-calendar/KR)의 previousBusinessDay → YYYYMMDD."""
    cal = client.get("/api/v1/market-calendar/KR").get("result") or {}
    d = (cal.get("previousBusinessDay") or {}).get("date")
    if not d:
        raise SystemExit("전 영업일을 알 수 없습니다. --date로 지정하세요.")
    return d.replace("-", "")


def cmd_candles_check(client, con, log: Log, now, day: str | None = None, symbols: list[str] | None = None,
                      n: int = 10) -> dict:
    if not day:
        day = previous_business_day(client)
        log(f"[candles-check] --date 없음 → 전 영업일 {day}")
    syms = symbols or default_symbols(client, n)
    log(f"[candles-check] 날짜 {day}, 종목 {len(syms)}개: {', '.join(syms)}")
    log("  토스 1분봉(봉 시작 시각 기준)을 같은 날 토스 일봉과 대조. KIS 데이터는 쓰지 않음")
    per, miss_tot, exp_tot = [], 0, 0
    for sym in syms:
        bars = toss_day_bars(client, sym, day)
        store.save_candles(con, sym, "1m", False, bars)
        d1 = toss_daily_bar(client, sym, day)
        c = check_day(bars, d1)
        c["symbol"] = sym
        per.append(c)
        miss_tot += c["regular_missing"]
        exp_tot += c["regular_expected"]
        yn = lambda m: "-" if m is None else " ".join(f"{k[0].upper()}{'O' if v else 'X'}" for k, v in m.items())
        v = c["volume"]
        f = lambda x: "-" if x is None else f"{x:+.2f}%"
        log(f"  {sym}: 봉 {c['bars']}개 ({c['first_start']}~{c['last_start']} 시작), 정규장 누락 {c['regular_missing']}분"
            + (f" 예 {c['missing_examples']}" if c["regular_missing"] else "")
            + f", 15:20~15:29 봉 {c['auction_1520_1529_bars']}, 거래량 0인 봉 {c['zero_volume_bars']}")
        log(f"     일봉 {c['daily']}")
        log(f"     일봉과 같은가 (O 시가 H 고가 L 저가 C 종가): 정규장 분봉 {yn(c['match_regular'])} / "
            f"전체 세션 분봉 {yn(c['match_all_sessions'])}")
        log(f"     거래량: 정규장 분봉 합 {v['regular']} ({f(v['regular_vs_daily_pct'])}), 전체 분봉 합 {v['all']} "
            f"({f(v['all_vs_daily_pct'])}), 일봉 {v['daily']}")
    with_daily = [c for c in per if c["match_regular"] is not None]
    close_reg = sum(1 for c in with_daily if c["match_regular"]["close"])
    ohlc_all = sum(1 for c in with_daily if c["match_all_sessions"] and all(c["match_all_sessions"].values()))
    close_all = sum(1 for c in with_daily if c["match_all_sessions"] and c["match_all_sessions"]["close"])
    out = {"date": day, "symbols": per, "regular_completeness": (1 - miss_tot / exp_tot) if exp_tot else None,
           "daily_checked": len(with_daily), "close_match_regular": close_reg, "close_match_all": close_all,
           "ohlc_match_all": ohlc_all}
    comp = out["regular_completeness"]
    log(f"[candles-check] 정규장 분봉 완전성 {'-' if comp is None else f'{comp * 100:.2f}%'} "
        f"(누락 {miss_tot}분 / 기대 {exp_tot}분)")
    log(f"[candles-check] 일봉 종가 = 정규장 마지막 분봉 종가: {close_reg}/{len(with_daily)}, "
        f"= 전체 세션 마지막 분봉 종가: {close_all}/{len(with_daily)}")
    log(f"[candles-check] 일봉 시·고·저·종가 = 전체 세션 분봉 합성: {ohlc_all}/{len(with_daily)} "
        "(토스 일봉은 KRX+NXT 통합 기준)")
    store.put_result(con, "candles_check", out, _now_s(now))
    return out


# ---------------------------------------------------------------- rankings
RANK_COMBOS = [("MARKET_TRADING_AMOUNT", "realtime"), ("MARKET_TRADING_AMOUNT", "1d"),
               ("MARKET_TRADING_VOLUME", "realtime"), ("MARKET_TRADING_VOLUME", "1d"), ("TOP_GAINERS", "1d")]


def _ranking(client, con, typ: str, dur: str, at: str) -> dict:
    r = client.get("/api/v1/rankings", {"type": typ, "marketCountry": "KR", "duration": dur, "count": 100}
                   ).get("result") or {}
    items = r.get("rankings") or []
    with con:
        con.executemany("INSERT INTO ranking_snap VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(at, typ, dur, r.get("rankedAt"), i.get("rank"), i.get("symbol"),
                          (i.get("price") or {}).get("lastPrice"), (i.get("price") or {}).get("changeRate"),
                          i.get("tradingAmount"), i.get("tradingVolume")) for i in items])
    return {"type": typ, "duration": dur, "n": len(items), "rankedAt": r.get("rankedAt"),
            "sample": items[0] if items else None, "symbols": [i.get("symbol") for i in items]}


def cmd_rankings(client, con, log: Log, now, watch: float = 0, times: int = 10,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    out = {"rankings": [], "investor": {}, "watch": []}
    for typ, dur in RANK_COMBOS:
        r = _ranking(client, con, typ, dur, _now_s(now))
        out["rankings"].append({k: v for k, v in r.items() if k != "symbols"})
        log(f"[rankings] {typ} {dur}: {r['n']}종목, rankedAt {r['rankedAt']}")
        if r["sample"]:
            log(f"    예시: {json.dumps(r['sample'], ensure_ascii=False)}")
    inv = client.get("/api/v1/rankings/investor-trading", {"count": 100}).get("result") or {}
    for who in ("foreigner", "institution", "individual"):
        x = inv.get(who) or {}
        out["investor"][who] = {"rankedAt": x.get("rankedAt"), "netBuy": len(x.get("netBuyRankings") or []),
                                "netSell": len(x.get("netSellRankings") or [])}
        log(f"[rankings/investor-trading] {who}: rankedAt {x.get('rankedAt')}, 순매수 "
            f"{out['investor'][who]['netBuy']}, 순매도 {out['investor'][who]['netSell']}")
    if watch > 0:
        log(f"[rankings --watch] {watch:g}초 간격 {times}회, MARKET_TRADING_AMOUNT realtime")
        prev = None
        for i in range(times):
            r = _ranking(client, con, "MARKET_TRADING_AMOUNT", "realtime", _now_s(now))
            top = r["symbols"][:20]
            same = len(set(top) & set(prev)) if prev is not None else None
            out["watch"].append({"at": _now_s(now), "rankedAt": r["rankedAt"], "n": r["n"], "top20_same_as_prev": same})
            log(f"  {i + 1}/{times} rankedAt {r['rankedAt']}, {r['n']}종목, 상위 20 중 직전과 같은 종목 "
                f"{'-' if same is None else same}")
            prev = top
            if i < times - 1:
                sleep(watch)
    key = "rankings_watch" if watch > 0 else "rankings"
    store.put_result(con, key, out, _now_s(now))
    return out


# ---------------------------------------------------------------- stock-info
def _trend_depth(client, path: str, log: Log, max_calls: int) -> dict:
    p = {"count": 100}
    calls, n, oldest, stop = 0, 0, None, ""
    while True:
        if calls >= max_calls:
            stop = f"호출 상한 {max_calls}회"
            break
        r = client.get(path, p).get("result") or {}
        calls += 1
        recs = r.get("records") or []
        n += len(recs)
        ds = [x["date"] for x in recs if x.get("date")] + ([oldest] if oldest else [])
        if ds:
            oldest = min(ds)
        nu = r.get("nextUntil")
        if not recs or not nu or nu == p.get("until"):
            stop = "더 과거 데이터 없음"
            break
        p["until"] = nu
    log(f"[{path}] 가장 오래된 날짜 {oldest}, 기록 {n}일, 호출 {calls}회, 멈춘 이유: {stop}")
    return {"oldest": oldest, "records": n, "calls": calls, "stop": stop}


def cmd_stock_info(client, con, log: Log, now, symbol: str = "005930", max_calls: int = 300) -> dict:
    out = {"universe": {}, "delisted": {}, "warnings": {}, "trend": {}}
    delisted = []
    for market in ("KOSPI", "KOSDAQ"):
        for status in ("ACTIVE", "DELISTED"):
            items = client.get("/api/v1/stocks/all", {"market": market, "status": status}).get("result") or []
            common = sum(1 for i in items if i.get("isCommonShare"))
            stock_common = sum(1 for i in items if i.get("isCommonShare") and i.get("securityType") == "STOCK")
            types = Counter(i.get("securityType") for i in items)
            out["universe"][f"{market}_{status}"] = {"total": len(items), "common": common,
                                                     "stock_common": stock_common, "by_type": dict(types)}
            log(f"[stocks/all] {market} {status}: {len(items):,}종목, isCommonShare {common:,}, "
                f"그중 주식(STOCK) 보통주 {stock_common:,} / 유형별 {dict(types)}")
            if status == "DELISTED":
                delisted += [i["symbol"] for i in items if i.get("symbol")]
    # stocks/all 응답에는 delistDate가 없어 stocks(최대 200건씩)로 조회
    dates = []
    for i in range(0, len(delisted), 200):
        for s in client.get("/api/v1/stocks", {"symbols": ",".join(delisted[i:i + 200])}).get("result") or []:
            dates.append(s.get("delistDate"))
    today = now().date()
    known = [d for d in dates if d]
    recent = sum(1 for d in known if Date.fromisoformat(d[:10]) >= today - timedelta(days=365))
    by_year = Counter(d[:4] for d in known)
    out["delisted"] = {"symbols": len(delisted), "with_delist_date": len(known), "last_1y": recent,
                       "by_year": dict(sorted(by_year.items()))}
    log(f"[delisted] {len(delisted)}종목 중 delistDate 있음 {len(known)}, 최근 1년 {recent}, 연도별 {dict(sorted(by_year.items()))}")

    top = (client.get("/api/v1/rankings", {"type": "MARKET_TRADING_AMOUNT", "marketCountry": "KR",
                                           "duration": "realtime", "count": 30}).get("result") or {}).get("rankings") or []
    if not top:
        top = (client.get("/api/v1/rankings", {"type": "MARKET_TRADING_AMOUNT", "marketCountry": "KR",
                                               "duration": "1d", "count": 30}).get("result") or {}).get("rankings") or []
    cnt, with_any = Counter(), 0
    t = today.isoformat()
    for it in top[:30]:
        ws = client.get(f"/api/v1/stocks/{it['symbol']}/warnings").get("result") or []
        active = [w for w in ws if (w.get("startDate") or "") <= t and (w.get("endDate") is None or w["endDate"] >= t)]
        cnt.update(w.get("warningType") for w in active)
        with_any += bool(active)
    out["warnings"] = {"checked": len(top[:30]), "with_active": with_any, "by_type": dict(cnt)}
    log(f"[warnings] 거래대금 상위 {len(top[:30])}종목 중 활성 경고 있음 {with_any}, 유형별 {dict(cnt)}")

    for kind in ("investor-trading", "program-trades"):
        out["trend"][kind] = _trend_depth(client, f"/api/v1/stocks/{symbol}/{kind}", log, max_calls)
    store.put_result(con, "stock_info", out, _now_s(now))
    return out


# ---------------------------------------------------------------- market
def cmd_market(client, con, log: Log, now, max_calls: int = 3000, max_years: float = 3) -> dict:
    out = {}
    cal = client.get("/api/v1/market-calendar/KR").get("result")
    out["calendar"] = cal
    log("[market-calendar/KR] " + json.dumps(cal, ensure_ascii=False, indent=1))
    out["index_1m"] = {}
    for sym in ("KOSPI", "KOSDAQ"):  # 지수 캔들 timestamp는 봉 '시작' 시각 (종목 캔들과 다름)
        out["index_1m"][sym] = _depth(client, f"/api/v1/market-indicators/{sym}/candles",
                                      {"interval": "1m", "count": 200}, log, now, max_calls, max_years, f"지수 1m {sym}")
    sectors = (client.get("/api/v1/sectors").get("result") or {}).get("sectors") or []
    by_depth = Counter(s.get("depth") for s in sectors)
    out["sectors"] = {"total": len(sectors), "by_depth": {str(k): v for k, v in sorted(by_depth.items())}}
    log(f"[sectors] {len(sectors)}개, depth별 {dict(sorted(by_depth.items()))}")
    out["sector_rankings"] = {}
    for typ in ("TOP_GAINERS", "MARKET_TRADING_AMOUNT"):
        rk = (client.get("/api/v1/sectors/rankings", {"type": typ, "marketCountry": "KR", "duration": "1d"})
              .get("result") or {}).get("rankings") or []
        out["sector_rankings"][typ] = rk[:10]
        log(f"[sectors/rankings {typ} 1d] 상위 10")
        for x in rk[:10]:
            log(f"    {x.get('rank')}. {x.get('name')} 등락률 {x.get('changeRate')} 거래대금 {x.get('tradingAmount')}")
    store.put_result(con, "market", out, _now_s(now))
    return out


# ---------------------------------------------------------------- limits
def cmd_limits(con, log: Log, rate_factor: float) -> list[tuple]:
    from .client import DOC_LIMITS
    rows = con.execute(
        "SELECT grp, COUNT(*), SUM(status=429), GROUP_CONCAT(DISTINCT limit_hdr),"
        " MIN(CAST(remaining_hdr AS REAL)) FROM ratelimit_log GROUP BY grp ORDER BY grp").fetchall()
    log("[limits] 그룹 | 호출 수 | 429 수 | 응답 헤더 X-RateLimit-Limit 값 | 최소 Remaining | 문서 한도 | 이 프로그램 속도")
    for g, n, n429, lims, rem in rows:
        log(f"  {g} | {n} | {n429 or 0} | {lims or '-'} | {'-' if rem is None else f'{rem:g}'} | "
            f"{DOC_LIMITS.get(g, '-')} | {DOC_LIMITS.get(g, 1) * rate_factor:g}/초")
    return rows


# ---------------------------------------------------------------- report
def judge(depth: dict | None, compare: dict | None, check: dict | None = None) -> tuple[str, list[str]]:
    """KIS 분봉 매일 수집을 토스로 대체할 수 있는지.

    - KIS 비교(candles-compare)가 있으면: 1분봉 30거래일 이상 + 정규장 종가 일치율 99% 이상
    - 없으면 자체 점검(candles-check): 1분봉 30거래일 이상 + 정규장 분봉 완전성 99% 이상
      + 일봉 종가가 정규장 마지막 분봉 종가와 같은 종목 99% 이상. (KIS와 같은지는 확인하지 않음)
    """
    if not depth:
        return "판정 불가", ["측정 안 됨: candles-depth"]
    days = (depth.get("m1") or {}).get("trading_days") or 0
    why = [f"토스 1분봉 과거 깊이 {days}일 ({'충족' if days >= 30 else '미달'}, 기준 30거래일 이상)"]
    if compare and compare.get("overall_close_rate") is not None:
        rate = compare["overall_close_rate"]
        why.append(f"KIS 대비 정규장 종가 일치율 {rate * 100:.2f}% ({'충족' if rate >= 0.99 else '미달'}, 기준 99% 이상)")
        return ("대체 가능" if days >= 30 and rate >= 0.99 else "대체 불가"), why
    if check and check.get("regular_completeness") is not None and check.get("daily_checked"):
        comp = check["regular_completeness"]
        n = check["daily_checked"]
        # 토스 일봉은 KRX+NXT 통합 기준이므로 전체 세션 분봉으로 합성한 시·고·저·종가와 비교한다.
        # (0.1.1 이전 결과에는 ohlc_match_all이 없어 전체 세션 종가 일치 수로 대신한다)
        hit = check.get("ohlc_match_all", check.get("close_match_all", 0))
        cr = hit / n
        why.append(f"정규장 분봉 완전성 {comp * 100:.2f}% ({'충족' if comp >= 0.99 else '미달'}, 기준 99% 이상)")
        why.append(f"일봉 = 전체 세션 분봉 합성 {hit}/{n}종목 ({'충족' if cr >= 0.99 else '미달'}, 기준 99% 이상)")
        why.append("KIS 분봉과 같은 값인지는 비교하지 않았습니다 (토스 자체 점검 기준)")
        ok = days >= 30 and comp >= 0.99 and cr >= 0.99
        return ("대체 가능 (자체 점검 기준)" if ok else "대체 불가"), why
    return "판정 불가", ["측정 안 됨: candles-compare 또는 candles-check"]


def _yn(cond) -> str:
    return "가능" if cond is True else ("불가" if cond is False else "조건부")


def cmd_report(con, log: Log, now, out_dir: Path) -> Path:
    g = lambda k: store.get_result(con, k)[0]
    auth, depth, comp, rk, rkw, si, mk, chk = (g("auth"), g("candles_depth"), g("candles_compare"), g("rankings"),
                                               g("rankings_watch"), g("stock_info"), g("market"), g("candles_check"))
    L = [f"# 토스증권 Open API 측정 요약 ({now().strftime('%Y-%m-%d %H:%M')})", "",
         "| 항목 | 판정 | 숫자 | KIS와의 차이 |", "|---|---|---|---|"]
    if auth:
        L.append(f"| 인증·계좌 조회 | 가능 | 계좌 {auth['accounts']}개 | - |")
    if depth:
        m1, d1 = depth["m1"], depth["d1"]
        L.append(f"| 종목 1분봉 과거 깊이 | {_yn(m1['trading_days'] >= 30)} | {m1['oldest']} 부터, {m1['trading_days']}일, "
                 f"호출 {m1['calls']}회 ({m1['stop']}) | KIS 약 1년 + 10일 보관 |")
        L.append(f"| 종목 일봉 과거 깊이 | 가능 | {d1['oldest']} 부터, {d1['trading_days']}일 | - |")
        for a in depth["adjusted"]:
            L.append(f"| adjusted true/false ({a['interval']}) | 조건부 | 같은 시각 {a['common']}봉 중 다른 봉 {a['different']} | - |")
    if comp:
        r = comp["overall_close_rate"]
        L.append(f"| 분봉 KIS 비교 ({comp['date']}) | {_yn(r is not None and r >= 0.99)} | "
                 f"정렬 shift {comp['chosen_shift']}, 정규장 종가 일치 {'-' if r is None else f'{r * 100:.2f}%'} | "
                 f"NXT 시간대 봉 있는 종목 {comp['symbols_with_nxt_hours']}/{len(comp['symbols'])} |")
        for s in comp["symbols"]:
            b = s["best"]
            v = b["volume_diff_pct"]
            L.append(f"|  - {s['group']} {s['code']} | | 토스 {s['toss_bars_all']}봉 / KIS {s['kis_bars']}봉, "
                     f"종가 {(b['match_rate']['close'] or 0) * 100:.1f}% | 거래량 {'-' if v is None else f'{v:+.2f}%'} |")
    if chk:
        c = chk["regular_completeness"]
        L.append(f"| 분봉 자체 점검 ({chk['date']}) | {_yn(c is not None and c >= 0.99)} | 정규장 완전성 "
                 f"{'-' if c is None else f'{c * 100:.2f}%'}, 일봉 = 전체 세션 분봉 합성 "
                 f"{chk.get('ohlc_match_all', '-')}/{chk['daily_checked']}, 일봉 종가 = 정규장 마지막 분봉 "
                 f"{chk['close_match_regular']}/{chk['daily_checked']} | KIS 비교 안 함 |")
        for x in chk["symbols"]:
            v = x["volume"]
            pr = "-" if v["regular_vs_daily_pct"] is None else f"{v['regular_vs_daily_pct']:+.2f}%"
            pa = "-" if v["all_vs_daily_pct"] is None else f"{v['all_vs_daily_pct']:+.2f}%"
            L.append(f"|  - {x['symbol']} | | 봉 {x['bars']}, 정규장 누락 {x['regular_missing']}분, "
                     f"거래량 0 봉 {x['zero_volume_bars']} | 거래량 정규장/일봉 {pr}, 전체/일봉 {pa} |")
    if rk:
        for x in rk["rankings"]:
            L.append(f"| 순위 {x['type']} {x['duration']} | {_yn(x['n'] > 0)} | {x['n']}종목, rankedAt {x['rankedAt']} | - |")
        for who, x in rk["investor"].items():
            L.append(f"| 투자자 순매수 순위 {who} | {_yn(x['netBuy'] > 0)} | rankedAt {x['rankedAt']} | - |")
    if rkw:
        L.append(f"| 실시간 순위 반복 조회 | 가능 | {len(rkw['watch'])}회 | - |")
    if si:
        for k, v in si["universe"].items():
            L.append(f"| 종목 목록 {k} | 가능 | {v['total']:,} (isCommonShare {v['common']:,}, 주식 보통주 "
                     f"{v.get('stock_common', '-')}) | 기존 수집기 보통주 범위와 비교 필요 |")
        d = si["delisted"]
        L.append(f"| 상장폐지 종목 delistDate | 조건부 | {d['with_delist_date']}/{d['symbols']}, 최근 1년 {d['last_1y']} | - |")
        L.append(f"| 매수 유의사항 | 가능 | 상위 {si['warnings']['checked']} 중 {si['warnings']['with_active']}, "
                 f"{si['warnings']['by_type']} | - |")
        for k, v in si["trend"].items():
            L.append(f"| {k} 과거 깊이 | 가능 | {v['oldest']} 부터 {v['records']}일 | KIS 수급과 단위 다름(거래량만) |")
    if mk:
        for s, v in mk["index_1m"].items():
            L.append(f"| 지수 1분봉 {s} | {_yn(v['trading_days'] > 0)} | {v['oldest']} 부터, {v['trading_days']}일 | "
                     "지수 봉 시각은 '시작' 시각 |")
        L.append(f"| 섹터 | 가능 | {mk['sectors']['total']}개 {mk['sectors']['by_depth']} | - |")
    verdict, why = judge(depth, comp, chk)
    L += ["", f"## KIS 분봉 매일 수집 대체 가능 여부: **{verdict}**", ""] + [f"- {w}" for w in why]
    text = "\n".join(L) + "\n"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"toss_report_{now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text, encoding="utf-8")
    for line in L:
        log(line)
    return path
