"""브리핑 패키지 계산 (명세서 8장, 14.2). DB만 읽고 API는 부르지 않는다.

- 판단 없음: 수익률·합계·배수만. 순위·점수·강세/약세 없음.
- n일 합계·평균은 n거래일 자료가 모두 있을 때만. 빠지면 빈 칸 + missing_note.
- 기준일 이후 자료는 쓰지 않는다.
- 기업행동 의심(기준가 조정) 종목은 수집 단계에서 받은 수정주가로 수익률·이격을 계산한다 (8.4).
  수정주가가 없으면 그 창을 비우고 이유를 적는다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime

from . import trading_calendar as cal
from . import themes as themes_mod

RULE_VERSION = "3"
RET_WINDOWS = (5, 20, 60, 120, 250)
GAP_WINDOWS = (5, 20, 60, 120)
SUM_WINDOWS = (1, 5, 20)
INV_KEYS = ("frgn", "orgn", "prsn")


class BriefingError(Exception):
    pass


@dataclass
class Package:
    date: str
    generated_at: str
    theme_version: int | None
    universe_date: str
    provisional: dict
    info: list[tuple[str, str]] = field(default_factory=list)
    market: list[dict] = field(default_factory=list)
    index_series: list[dict] = field(default_factory=list)
    themes: list[dict] | None = None
    stocks: list[dict] = field(default_factory=list)
    unclassified: list[dict] = field(default_factory=list)
    status: list[dict] = field(default_factory=list)


def default_date(con) -> str | None:
    return con.execute("SELECT MAX(date) FROM price_daily").fetchone()[0]


def check_ready(con, day: str) -> tuple[bool, str]:
    """기준일 시세가 있는지: KRX 매매정보 success(코스피·코스닥) 또는 KIS 당일 잠정 시세 (14.2)."""
    krx = {r[0] for r in con.execute("SELECT dataset FROM collect_log WHERE source='KRX' AND unit_key=? AND status='success'"
                                    " AND dataset LIKE 'trade_%'", (day,))}
    if {"trade_KOSPI", "trade_KOSDAQ"} <= krx:
        return True, "KRX 확정"
    n_kis = con.execute("SELECT COUNT(*) FROM price_daily WHERE date=? AND source='KIS'", (day,)).fetchone()[0]
    if n_kis:
        return True, "KIS 잠정"
    if cal.is_open(con, day) is False:
        return False, f"{day}은 휴장일입니다."
    return False, f"{day}의 KRX 매매정보가 success가 아니고 KIS 당일 시세도 없습니다. 먼저 수집을 실행하세요."


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a / b - 1) * 100, 4)


def _ratio(a, b):
    if a is None or b in (None, 0):
        return None
    return round(a / b, 4)


def build(con, day: str, themes_dir, now: datetime, top_n: int = 50) -> Package:
    ok, why = check_ready(con, day)
    if not ok:
        raise BriefingError(why)
    days = sorted(r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date <= ? ORDER BY date DESC LIMIT 251", (day,)))
    if not days or days[-1] != day:
        raise BriefingError(f"{day}이 거래일 달력에 개장일로 없습니다.")
    pos = {d: i for i, d in enumerate(days)}
    # 대상 종목: 기준일 universe가 있으면 그 날, 없으면(당일 KIS) 직전 거래일 KRX 기준 (14.2)
    udate = con.execute("SELECT MAX(date) FROM universe_daily WHERE date <= ?", (day,)).fetchone()[0]
    if not udate:
        raise BriefingError("대상 종목(universe_daily)이 없습니다. KRX 종목기본정보를 먼저 수집하세요.")
    urows = {r["code"]: r for r in con.execute(
        "SELECT u.*, s.name, s.market, s.listed_date FROM universe_daily u LEFT JOIN stock_master s ON s.code=u.code"
        " WHERE u.date=? AND u.in_universe=1 ORDER BY u.code", (udate,))}
    codes = list(urows)

    prices: dict[str, dict[str, dict]] = {}
    for r in con.execute("SELECT * FROM price_daily WHERE date BETWEEN ? AND ?", (days[0], day)):
        prices.setdefault(r["code"], {})[r["date"]] = r
    inv: dict[str, dict[str, dict]] = {}
    inv_from = days[max(0, len(days) - 121)]
    for r in con.execute("SELECT * FROM investor_daily WHERE date BETWEEN ? AND ?", (inv_from, day)):
        inv.setdefault(r["code"], {})[r["date"]] = r

    adj: dict[str, dict[str, dict]] = {}
    adj_as_of: dict[str, str] = {}
    for r in con.execute("SELECT * FROM adj_price_daily WHERE date BETWEEN ? AND ?", (days[0], day)):
        adj.setdefault(r["code"], {})[r["date"]] = r
        adj_as_of[r["code"]] = r["as_of"]
    from .krx_collect import base_price_adjust_days
    adjusted = base_price_adjust_days(con, days[0], day, codes)
    master = {r[0] for r in con.execute("SELECT code FROM stock_master")}
    th = themes_mod.load(con, themes_dir, now, master, set(codes))

    pkg = Package(date=day, generated_at=now.isoformat(timespec="seconds"), theme_version=th.version,
                  universe_date=udate, provisional={})
    for code in codes:
        pkg.stocks.append(_stock_row(code, urows[code], prices.get(code, {}), inv.get(code, {}), days, pos, day,
                                     th.members.get(code, []), adj.get(code), adj_as_of.get(code), adjusted.get(code)))

    pkg.market = _market_rows(con, day, days, pkg.stocks)
    pkg.index_series = _index_rows(con, day, days)
    if th.exists:
        pkg.themes = _theme_rows(th, {s["code"]: s for s in pkg.stocks}, prices, days)
    pkg.unclassified = _unclassified(pkg.stocks, top_n)
    pkg.provisional = {
        "price": sum(1 for s in pkg.stocks if s["_price_provisional"]),
        "investor": sum(1 for s in pkg.stocks if s["investor_is_provisional"] == 1),
    }
    pkg.status = _status_rows(con, day, days, th, pkg)
    pkg.info = _info(pkg, why, len(codes), th)
    return pkg


def _window(days, pos, day, n, include_today=True):
    """기준일 포함 n거래일 (또는 기준일 제외 직전 n거래일). 달력이 모자라면 None."""
    i = pos[day]
    if include_today:
        return days[i - n + 1:i + 1] if i - n + 1 >= 0 else None
    return days[i - n:i] if i - n >= 0 else None


def _stock_row(code, u, P, I, days, pos, day, theme_list, A=None, as_of=None, adjust_days=None) -> dict:
    missing: dict[str, set] = {}
    today = P.get(day)
    close = today["close"] if today else None
    row = {
        "code": code, "name": u["name"], "market": u["market"], "themes": ";".join(sorted(theme_list)),
        "is_admin": u["is_admin"], "is_halted": u["is_halted"],
        "listed_date": u["listed_date"], "days_since_listing": u["days_since_listing"],
        "is_new_listing": u["is_new_listing"], "max_window": u["max_window"], "listing_note": u["listing_note"],
        "close": close, "change_rate_pct": today["change_rate_pct"] if today else None,
    }
    if today is None:
        missing.setdefault("price", set()).add(day)
    # 기업행동 의심: 창 안에서 기준가가 조정된 날 (KRX 종가 − 전일대비 ≠ 직전 거래일 종가)
    changes = adjust_days or []
    row["corp_action_suspect"] = 1 if changes and changes[-1] == day else 0
    corp_note = []
    # 수정주가: 마지막 변경일 기준으로 받은 것이 있으면 수익률·이격·250일 범위 계산에만 쓴다 (8.4-2)
    use_adj = bool(changes and A and as_of == changes[-1])
    if use_adj:
        def cl(d):
            r = A.get(d)
            return r["close"] if r else None
        calc_close = cl(day)
        corp_note.append(f"수익률·이격·250일 범위는 수정주가 사용 (기준가 조정 {changes[-1]})")
        if calc_close is None:
            corp_note.append("수정주가에 기준일 값 없음 -> 수익률·이격 비움")
        kind = "adj"
    else:
        def cl(d):
            r = P.get(d)
            return r["close"] if r else None
        calc_close = close
        kind = "price"
        if changes:
            corp_note.append("수정주가 없음")

    def blocked(win):
        if use_adj:
            return []
        return [d for d in changes if win and win[0] < d <= win[-1]]

    def note_missing(ds):
        missing.setdefault(kind, set()).update(d for d in ds if cl(d) is None)

    for n in RET_WINDOWS:
        key = f"ret_{n}d_pct"
        if calc_close is None or pos[day] - n < 0:
            row[key] = None
            continue
        base_day = days[pos[day] - n]
        hit = blocked(days[pos[day] - n:pos[day] + 1])
        if hit:
            row[key] = None
            corp_note.append(f"{n}일 수익률 비움(기준가 조정 {hit[-1]})")
        elif cl(base_day) is None:
            row[key] = None
            note_missing([base_day])
        else:
            row[key] = _pct(calc_close, cl(base_day))
    win250 = _window(days, pos, day, 250)
    closes = [cl(d) for d in win250] if win250 else None
    if closes and None not in closes and not blocked(win250) and calc_close is not None:
        hi, lo = max(closes), min(closes)
        row["high_250d"], row["low_250d"] = hi, lo
        row["pos_250d_pct"] = round((calc_close - lo) / (hi - lo) * 100, 4) if hi != lo else None
    else:
        row["high_250d"] = row["low_250d"] = row["pos_250d_pct"] = None
        if win250:
            note_missing(win250)
    for n in GAP_WINDOWS:
        key = f"gap_ma{n}_pct"
        win = _window(days, pos, day, n)
        vals = [cl(d) for d in win] if win else None
        if not vals or None in vals or calc_close is None:
            row[key] = None
            if win:
                note_missing(win)
        elif blocked(win):
            row[key] = None
            corp_note.append(f"{n}일 이격 비움(기준가 조정)")
        else:
            row[key] = _pct(calc_close, sum(vals) / n)
    row["volume_sh"] = today["volume_sh"] if today else None
    row["value_krw"] = today["value_krw"] if today else None
    for n, col, key in ((5, "value_krw", "value_x_ma5"), (20, "value_krw", "value_x_ma20"), (20, "volume_sh", "volume_x_ma20")):
        win = _window(days, pos, day, n, include_today=False)
        vals = [P[d][col] if d in P else None for d in win] if win else None
        if not vals or None in vals or row[col] is None:
            row[key] = None
            if win:
                missing.setdefault("price", set()).update(d for d in win if d not in P or P[d][col] is None)
        else:
            row[key] = _ratio(row[col], sum(vals) / n)
    row["mktcap_krw"] = today["mktcap_krw"] if today else None
    # 수급
    for who in INV_KEYS:
        for unit, col in (("qty", f"{who}_net_qty"), ("amt_mil", f"{who}_net_amt_mil")):
            for n in SUM_WINDOWS:
                key = f"{who}_net_{unit}_{n}d"
                win = _window(days, pos, day, n)
                vals = [I[d][col] if d in I else None for d in win] if win else None
                if not vals or None in vals:
                    row[key] = None
                    if win:
                        missing.setdefault("investor", set()).update(d for d in win if d not in I or I[d][col] is None)
                else:
                    row[key] = sum(vals)
    for who in ("frgn", "orgn"):
        row[f"{who}_streak"] = _streak(I, days, pos[day], f"{who}_net_amt_mil")
    a, b, mc = row["frgn_net_amt_mil_5d"], row["orgn_net_amt_mil_5d"], row["mktcap_krw"]
    row["frgn_orgn_net_amt_to_mktcap_bp_5d"] = round((a + b) * 1_000_000 / mc * 10_000, 4) \
        if a is not None and b is not None and mc else None
    t = I.get(day)
    row["investor_is_provisional"] = None if t is None else t["is_provisional"]
    row["_price_provisional"] = bool(today and today["is_provisional"])
    notes = []
    for kind, label in (("price", "시세"), ("adj", "수정주가"), ("investor", "수급")):
        ds = sorted(missing.get(kind, ()))
        if ds:
            notes.append(f"{label} 빠짐 {len(ds)}일(최근 {', '.join(ds[-3:])})")
    row["missing_note"] = "; ".join(notes + sorted(set(corp_note))) or None
    return row


def _streak(I, days, i, col):
    first = I.get(days[i])
    if first is None or first[col] is None:
        return None
    sign = (first[col] > 0) - (first[col] < 0)
    if sign == 0:
        return 0
    n = 0
    while i >= 0 and days[i] in I and I[days[i]][col] is not None and ((I[days[i]][col] > 0) - (I[days[i]][col] < 0)) == sign:
        n += 1
        i -= 1
    return n * sign


def _index_returns(con, series, name, day, days):
    rows = {r["date"]: r for r in con.execute(
        "SELECT * FROM index_daily WHERE series=? AND index_name=? AND date BETWEEN ? AND ?", (series, name, days[0], day))}
    t = rows.get(day)
    close = t["close"] if t else None
    out = {"close": close, "is_provisional": t["is_provisional"] if t else None}
    i = len(days) - 1
    for n in (1, 5, 20, 60):
        b = rows.get(days[i - n]) if i - n >= 0 else None
        out[f"ret_{n}d_pct"] = _pct(close, b["close"] if b else None)
    for n in (5, 20, 60):
        win = days[i - n + 1:i + 1] if i - n + 1 >= 0 else []
        vals = [rows[d]["close"] if d in rows else None for d in win]
        out[f"gap_ma{n}_pct"] = _pct(close, sum(vals) / n) if win and None not in vals else None
    return out


def _market_rows(con, day, days, stocks):
    out = []
    for series, name in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        r = {"market": series, "index_name": name}
        r.update({f"index_{k}": v for k, v in _index_returns(con, series, name, day, days).items()})
        mine = [s for s in stocks if s["market"] == series]
        chg = [s["change_rate_pct"] for s in mine if s["change_rate_pct"] is not None]
        r["stocks"] = len(mine)
        r["up_count"] = sum(1 for c in chg if c > 0)
        r["down_count"] = sum(1 for c in chg if c < 0)
        gaps = [s["gap_ma20_pct"] for s in mine if s["gap_ma20_pct"] is not None]
        r["above_ma20_pct"] = round(sum(1 for g in gaps if g > 0) / len(gaps) * 100, 2) if gaps else None
        vals = [s["value_krw"] for s in mine if s["value_krw"] is not None]
        r["value_krw_sum"] = sum(vals) if vals else None
        r["value_missing_stocks"] = len(mine) - len(vals)
        mrows = {x["date"]: x for x in con.execute(
            "SELECT * FROM market_investor_daily WHERE market=? AND date<=? ORDER BY date DESC LIMIT 20", (series, day))}
        for who in INV_KEYS:
            for n in SUM_WINDOWS:
                win = days[len(days) - n:]
                v = [mrows[d][f"{who}_net_amt_unconfirmed"] if d in mrows else None for d in win]
                r[f"{who}_net_amt_unconfirmed_{n}d"] = sum(v) if len(win) == n and None not in v else None
            for n in SUM_WINDOWS:
                v = [s[f"{who}_net_amt_mil_{n}d"] for s in mine]
                r[f"{who}_net_amt_mil_{n}d_stock_sum"] = sum(v) if v and None not in v else None
        r["market_investor_is_provisional"] = mrows[day]["is_provisional"] if day in mrows else None
        out.append(r)
    return out


def _index_rows(con, day, days):
    out = []
    names = con.execute("SELECT DISTINCT series, index_name FROM index_daily WHERE date=? ORDER BY series, index_name",
                        (day,)).fetchall()
    for n in names:
        r = {"series": n["series"], "index_name": n["index_name"]}
        r.update(_index_returns(con, n["series"], n["index_name"], day, days))
        out.append(r)
    return out


def _median(v):
    v = [x for x in v if x is not None]
    return round(statistics.median(v), 4) if v else None


def _theme_rows(th, by_code, prices, days):
    out = []
    day = days[-1]
    prev20 = days[-21:-1] if len(days) >= 21 else None
    for theme in sorted(th.themes):
        mem = [by_code[c] for c in th.themes[theme] if c in by_code]
        r = {"theme": theme, "stocks": len(mem)}
        r["ret_1d_pct_median"] = _median([s["change_rate_pct"] for s in mem])
        for n in (5, 20, 60):
            r[f"ret_{n}d_pct_median"] = _median([s[f"ret_{n}d_pct"] for s in mem])
        chg = [s["change_rate_pct"] for s in mem if s["change_rate_pct"] is not None]
        r["up_ratio_pct"] = round(sum(1 for c in chg if c > 0) / len(chg) * 100, 2) if chg else None
        vals = [s["value_krw"] for s in mem]
        r["value_krw_sum"] = sum(vals) if vals and None not in vals else None
        if prev20 and r["value_krw_sum"] is not None:
            daily = []
            for d in prev20:
                v = [prices.get(s["code"], {}).get(d) for s in mem]
                daily.append(None if any(x is None or x["value_krw"] is None for x in v) else sum(x["value_krw"] for x in v))
            r["value_x_ma20"] = _ratio(r["value_krw_sum"], sum(daily) / 20) if None not in daily else None
        else:
            r["value_x_ma20"] = None
        for who in ("frgn", "orgn"):
            for n in SUM_WINDOWS:
                v = [s[f"{who}_net_amt_mil_{n}d"] for s in mem]
                r[f"{who}_net_amt_mil_{n}d"] = sum(v) if v and None not in v else None
        both = [(s["frgn_net_amt_mil_1d"], s["orgn_net_amt_mil_1d"]) for s in mem]
        valid = [a + b for a, b in both if a is not None and b is not None]
        r["frgn_orgn_net_buy_ratio_pct"] = round(sum(1 for x in valid if x > 0) / len(valid) * 100, 2) if valid else None
        out.append(r)
    return out


def _unclassified(stocks, top_n):
    free = [s for s in stocks if not s["themes"]]
    a = sorted((s for s in free if s["value_x_ma20"] is not None), key=lambda s: (-s["value_x_ma20"], s["code"]))[:top_n]
    b = sorted((s for s in free if s["frgn_orgn_net_amt_to_mktcap_bp_5d"] is not None),
               key=lambda s: (-s["frgn_orgn_net_amt_to_mktcap_bp_5d"], s["code"]))[:top_n]
    out, seen = [], set()
    for lst, label in ((a, "value_x_ma20 상위"), (b, "외국인+기관 순매수/시총 상위")):
        for s in lst:
            if s["code"] in seen:
                next(x for x in out if x["code"] == s["code"])["selected_by"] += "; " + label
                continue
            seen.add(s["code"])
            out.append(dict(s, selected_by=label))
    return out


def _status_rows(con, day, days, th, pkg):
    out = []
    add = lambda kind, item, detail: out.append({"kind": kind, "item": item, "detail": detail})
    recent = days[-20:]
    for ds in ("trade_KOSPI", "trade_KOSDAQ", "base_KOSPI", "base_KOSDAQ", "index_KOSPI", "index_KOSDAQ"):
        ok = {r[0] for r in con.execute("SELECT unit_key FROM collect_log WHERE source='KRX' AND dataset=? AND status='success'"
                                        " AND unit_key BETWEEN ? AND ?", (ds, days[0], day))}
        miss = [d for d in days if d not in ok and d != day]
        if miss:
            add("빈 날짜", f"KRX {ds}", f"{len(miss)}일 (최근: {', '.join(miss[-5:])})")
    for r in con.execute("SELECT source, dataset, unit_key, status, last_error FROM collect_log WHERE status IN ('empty','failed')"
                         " AND (unit_key BETWEEN ? AND ? OR unit_key LIKE '%|' || ?) ORDER BY unit_key DESC LIMIT 200",
                         (recent[0], day, day)):
        add(r["status"], f"{r['source']} {r['dataset']} {r['unit_key']}", r["last_error"] or "")
    if pkg.provisional.get("price"):
        add("잠정값", "시세", f"기준일 시세 잠정(KIS) {pkg.provisional['price']}종목")
    if pkg.provisional.get("investor"):
        add("잠정값", "수급", f"기준일 수급 잠정 {pkg.provisional['investor']}종목")
    no_inv = sum(1 for s in pkg.stocks if s["investor_is_provisional"] is None)
    if no_inv:
        add("빈 값", "수급", f"기준일 수급 없음 {no_inv}종목 (해당 칸 비움)")
    for m in pkg.market:
        if m["index_close"] is None:
            add("빈 값", f"{m['index_name']} 지수", "기준일 지수 없음")
    for s in pkg.stocks:
        if s["corp_action_suspect"]:
            add("기업행동 의심", f"{s['code']} {s['name']}", s["missing_note"] or "기준가 조정")
    for line, orig, fixed in th.fixes:
        add("자동 보정", f"테마 CSV {line}줄", f"종목코드 {orig} -> {fixed}")
    for line, desc, problem in th.problems:
        add("테마 CSV 문제", f"{line}줄 {desc}", problem)
    add("filter_conflict", "-", "14.3에 따라 KRX 기준만 사용하므로 해당 없음")
    return out


UNITS = [
    ("*_krw", "원"), ("*_amt_mil", "백만 원 (KIS 순매수 대금, 실측 확인)"), ("value_krw", "원 (KRX ACC_TRDVAL / KIS acml_tr_pbmn, 실측 일치)"),
    ("*_sh, *_qty", "주"), ("*_pct", "% (0.5 = 0.5%)"), ("*_bp_*", "bp (0.01%)"),
    ("value_x_*, volume_x_*", "배 (당일 ÷ 직전 n거래일 평균)"),
    ("*_unconfirmed", "단위 미확인 (KIS 시장별 수급, KRX 지수 거래량·대금)"),
]


def _info(pkg, ready_reason, n, th):
    from .version import __version__
    return [
        ("기준일", pkg.date), ("생성 시각", pkg.generated_at), ("프로그램 버전", __version__),
        ("계산 규칙 버전", RULE_VERSION), ("기준일 시세 출처", ready_reason),
        ("대상 종목 기준일", pkg.universe_date), ("대상 종목 수", str(n)),
        ("테마 버전", str(th.version) if th.version else "테마 CSV 없음 (테마 시트 생략)"),
        ("잠정값", f"시세 {pkg.provisional['price']}종목, 수급 {pkg.provisional['investor']}종목 (다음 수집 때 확정)"),
        ("원칙", "판단(강세·약세·점수·추천·순위) 없음. 계산값만. 빈 칸은 자료 부족이며 0이 아님."),
        ("n일 수익률", "(기준일 종가 ÷ n거래일 전 종가 − 1) × 100. 거래일은 달력 테이블 기준"),
        ("n일 합계·평균", "n거래일 자료가 모두 있을 때만. 빠지면 빈 칸 + missing_note"),
        ("직전 n일 평균", "기준일 제외 앞의 n거래일 (value_x_ma5/20, volume_x_ma20)"),
        ("기업행동 의심", "기준가가 조정된 날(KRX 종가 − 전일대비 ≠ 직전 거래일 종가)이 있는 종목은 KIS 수정주가(FHKST03010100)로 수익률·이격·250일 범위를 계산. "
                          "원본 가격(close)은 그대로. 수정주가가 없으면 해당 창을 비우고 이유를 적음"),
        ("5_미분류", "판단이 아닌 목록: 테마 밖 종목 중 value_x_ma20 상위와 외국인+기관 순매수/시총 상위 합집합"),
    ] + [(f"단위 {k}", v) for k, v in UNITS]
