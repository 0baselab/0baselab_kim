"""KRX 실측 항목: 지수 시리즈, 당일 데이터 제공 시각, 종목기본정보 필드 값."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta

from ..krx import BASE_INFO_PATHS, TRADE_PATHS, name_of, rows
from .common import DONE, FAILED, NEEDS_SETTING, SKIPPED, Ctx, Item, md_table

MAX_BACKTRACK = 10


def load_trade(ctx: Ctx, date: str) -> int:
    total = 0
    for market, path in TRADE_PATHS.items():
        if (date, market) not in ctx.krx_trade:
            body = ctx.krx.get(path, date)
            ctx.save_raw(f"krx_trade_{market}_{date}", body)
            ctx.krx_trade[(date, market)] = rows(body)
        total += len(ctx.krx_trade[(date, market)])
    return total


def fallback_ref_date(ctx: Ctx) -> str | None:
    """KIS로 기준일을 못 정했을 때만: 어제부터 거꾸로 KRX에 값이 있는 날을 찾는다.
    평일을 거래일로 가정하지 않고, 실제 응답이 있는 날만 쓴다."""
    d = datetime.strptime(ctx.today, "%Y%m%d")
    for i in range(1, MAX_BACKTRACK + 1):
        day = (d - timedelta(days=i)).strftime("%Y%m%d")
        if load_trade(ctx, day) > 0:
            return day
    return None


def item6_today(ctx: Ctx) -> Item:
    it = Item("6", "KRX 당일 데이터: 몇 시부터 당일 값이 나오는지")
    fetched = ctx.now.strftime("%H:%M:%S")
    counts = {}
    for market, path in TRADE_PATHS.items():
        body = ctx.krx.get(path, ctx.today)
        ctx.save_raw(f"krx_trade_{market}_{ctx.today}_today", body)
        r = rows(body)
        ctx.krx_trade[(ctx.today, market)] = r
        counts[market] = len(r)
    snap = ctx.dirs["snapshots"] / f"krx_today_{ctx.today}.jsonl"
    with snap.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"fetched_at": fetched, "counts": counts}, ensure_ascii=False) + "\n")
    history = [json.loads(x) for x in snap.read_text(encoding="utf-8").splitlines() if x.strip()]
    it.add(f"- 오늘({ctx.today}) basDd로 조회. 오늘 실행 기록 파일: system/verify/snapshots/{snap.name}", "")
    it.add(*md_table(["실행 시각", "코스피 행 수", "코스닥 행 수"],
                     [[h["fetched_at"], h["counts"].get("KOSPI"), h["counts"].get("KOSDAQ")] for h in history]))
    return it


def item5_index(ctx: Ctx) -> Item:
    it = Item("5", "KRX 지수 시리즈: 서비스 경로와 지수 이름 목록")
    urls = {"KOSPI": ctx.settings.get("KRX_INDEX_KOSPI_URL", "").strip(),
            "KOSDAQ": ctx.settings.get("KRX_INDEX_KOSDAQ_URL", "").strip()}
    if not ctx.ref_date:
        it.status = SKIPPED
        it.add("- 기준일이 없어 건너뜀")
        return it
    missing = [m for m, u in urls.items() if not u]
    if len(missing) == 2:
        it.status = NEEDS_SETTING
        it.add("- config/settings.env의 KRX_INDEX_KOSPI_URL, KRX_INDEX_KOSDAQ_URL이 비어 있어 건너뜀")
        return it
    for market, url in urls.items():
        it.add(f"**{market}** — `{url or '(비어 있음)'}`, basDd={ctx.ref_date}", "")
        if not url:
            it.status = NEEDS_SETTING
            it.add("- 설정 필요: URL이 비어 있어 건너뜀", "")
            continue
        body = ctx.krx.get_url(url, ctx.ref_date)
        ctx.save_raw(f"krx_index_{market}_{ctx.ref_date}", body)
        r = rows(body)
        if not r:
            it.status = FAILED
            it.add(f"- 행 없음. 응답 최상위 키: {list(body.keys())}", "")
            continue
        names = [str(x.get("IDX_NM", "")) for x in r]
        it.add(f"- 행 수: {len(r)}", f"- 필드: {', '.join(r[0].keys())}",
               f"- 지수 이름(IDX_NM) 전체: {', '.join(names) if any(names) else 'IDX_NM 필드 없음'}",
               f"- '코스피' 포함 이름: {[n for n in names if '코스피' in n] or '없음'}",
               f"- '코스닥' 포함 이름: {[n for n in names if '코스닥' in n] or '없음'}", "")
    return it


def item7_fields(ctx: Ctx) -> Item:
    it = Item("7", "KRX 종목기본정보: SECUGRP_NM·SECT_TP_NM·KIND_STKCERT_TP_NM 실제 값")
    if not ctx.ref_date:
        it.status = SKIPPED
        it.add("- 기준일이 없어 건너뜀")
        return it
    for market, path in BASE_INFO_PATHS.items():
        body = ctx.krx.get(path, ctx.ref_date)
        ctx.save_raw(f"krx_baseinfo_{market}_{ctx.ref_date}", body)
        r = rows(body)
        ctx.krx_base[market] = r
        it.add(f"**{market}** (basDd={ctx.ref_date}, {len(r)}행)", "")
        if not r:
            it.status = FAILED
            it.add(f"- 행 없음. 응답 최상위 키: {list(body.keys())}", "")
            continue
        it.add(f"- 필드: {', '.join(r[0].keys())}", "")
        for fld in ("SECUGRP_NM", "SECT_TP_NM", "KIND_STKCERT_TP_NM"):
            c = Counter(str(x.get(fld)) for x in r)
            table = []
            for val, n in c.most_common():
                ex = [name_of(x) for x in r if str(x.get(fld)) == val][:3]
                table.append([val, n, ", ".join(ex)])
            it.add(f"`{fld}`", "")
            it.add(*md_table(["값", "종목 수", "예시"], table))
            it.add("")
        spac = [x for x in r if "스팩" in name_of(x)]
        it.add(f"이름에 '스팩'이 들어간 종목 {len(spac)}개 (최대 10개 표시):", "")
        it.add(*md_table(["이름", "SECUGRP_NM", "SECT_TP_NM", "KIND_STKCERT_TP_NM"],
                         [[name_of(x), x.get("SECUGRP_NM"), x.get("SECT_TP_NM"), x.get("KIND_STKCERT_TP_NM")]
                          for x in spac[:10]]))
        it.add("")
    return it
