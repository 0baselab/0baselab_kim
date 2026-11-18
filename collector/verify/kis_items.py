"""KIS 실측 항목: 휴장일, 수급 조회 일수, 금액 단위, 당일 값 변동, 증권그룹 코드."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..kis import HOLIDAY, INVESTOR_BY_STOCK, STOCK_INFO, holiday_params, investor_params, is_ok, stock_info_params
from ..krx import name_of, short_code
from .common import DONE, FAILED, SKIPPED, Ctx, Item, fmt, md_table, num, ratio

MAX_CONT_PAGES = 10
MAX_SHIFT_PAGES = 6
SNAPSHOT_FIELDS = ["acml_vol", "acml_tr_pbmn", "frgn_ntby_qty", "orgn_ntby_qty", "prsn_ntby_qty",
                   "frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "prsn_ntby_tr_pbmn"]


def _dates(body: dict) -> list[str]:
    return [r["stck_bsop_date"] for r in (body.get("output2") or []) if r.get("stck_bsop_date")]


def item_holiday(ctx: Ctx) -> Item:
    """휴장일조회는 하루 1회. 같은 날 이미 받았으면 캐시를 쓴다."""
    it = Item("H", "KIS 휴장일조회 (CTCA0903R): 응답에 며칠치가 오는지")
    cache = ctx.dirs["verify"] / f"holiday_{ctx.today}.json"
    if cache.exists():
        body = json.loads(cache.read_text(encoding="utf-8"))
        it.add(f"- 오늘({ctx.today}) 이미 호출한 결과가 있어 캐시를 사용했습니다. (API 호출 0회)")
    else:
        body = ctx.kis.get(HOLIDAY, holiday_params(ctx.today)).body
        cache.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        it.add(f"- 기준일자 BASS_DT={ctx.today}로 1회 호출했습니다.")
    ctx.save_raw("kis_holiday", body)
    if not is_ok(body):
        it.status = FAILED
        it.add(f"- 응답 코드 {body.get('msg_cd')}: {body.get('msg1')}")
        return it
    out = body.get("output") or []
    rows = out if isinstance(out, list) else [out]
    ctx.holiday_rows = rows
    dates = [r.get("bass_dt") for r in rows if r.get("bass_dt")]
    it.add(f"- 응답 행 수: **{len(rows)}일치**",
           f"- 날짜 범위: {min(dates) if dates else 'NULL'} ~ {max(dates) if dates else 'NULL'}",
           f"- 응답 헤더 tr_cont 사용 불가 API (KIS 문서). 연속 조회는 시도하지 않았습니다.", "")
    it.add(*md_table(["bass_dt", "wday_dvsn_cd", "bzdy_yn", "tr_day_yn", "opnd_yn", "sttl_day_yn"],
                     [[r.get(k, "") for k in ("bass_dt", "wday_dvsn_cd", "bzdy_yn", "tr_day_yn", "opnd_yn", "sttl_day_yn")]
                      for r in rows]))
    return it


def today_open(ctx: Ctx) -> str | None:
    for r in ctx.holiday_rows:
        if r.get("bass_dt") == ctx.today:
            return r.get("opnd_yn")
    return None


def item1_depth(ctx: Ctx) -> Item:
    it = Item("1", "KIS 종목별 투자자매매동향: 1회 조회 일수와 과거 조회 깊이")
    code = ctx.stocks[0]
    it.add(f"- 종목 {code}, FID_INPUT_DATE_1={ctx.today}, 시장코드 J(KRX)", "",
           "**방법 A: 연속 조회 (요청 헤더 tr_cont=N)**", "")
    rows, seen, tr_cont = [], set(), ""
    for page in range(1, MAX_CONT_PAGES + 1):
        res = ctx.kis.get(INVESTOR_BY_STOCK, investor_params(code, ctx.today), tr_cont=tr_cont)
        ctx.save_raw(f"kis_investor_{code}_cont{page}", res.body)
        if not is_ok(res.body):
            it.status = FAILED
            it.add(f"- {page}번째 호출 실패: {res.body.get('msg_cd')} {res.body.get('msg1')}")
            break
        d = _dates(res.body)
        if page == 1:
            ctx.kis_first[code] = res.body.get("output2") or []
        new = [x for x in d if x not in seen]
        seen.update(d)
        rows.append([page, len(d), min(d) if d else "NULL", max(d) if d else "NULL", len(new), res.tr_cont or "(공백)"])
        if res.tr_cont not in ("M", "F") or not new:
            break
        tr_cont = "N"
    it.add(*md_table(["호출", "받은 일수", "가장 오래된 날", "가장 최근 날", "새 날짜 수", "응답 tr_cont"], rows))
    it.add("", f"- 방법 A 누적 고유 날짜 수: **{len(seen)}**, 가장 오래된 날: {min(seen) if seen else 'NULL'}", "",
           "**방법 B: 기준일을 가장 오래된 날 하루 전으로 옮겨 다시 조회**", "")
    if not ctx.kis_first.get(code):
        it.add("- 첫 조회 결과가 없어 건너뜀")
        return it
    seen_b = set(_dates({"output2": ctx.kis_first[code]}))
    rows_b = []
    for page in range(1, MAX_SHIFT_PAGES + 1):
        base = datetime.strptime(min(seen_b), "%Y%m%d") - timedelta(days=1)
        q = base.strftime("%Y%m%d")
        res = ctx.kis.get(INVESTOR_BY_STOCK, investor_params(code, q))
        ctx.save_raw(f"kis_investor_{code}_shift{page}", res.body)
        if not is_ok(res.body):
            rows_b.append([page, q, "실패", f"{res.body.get('msg_cd')} {res.body.get('msg1')}", ""])
            break
        d = _dates(res.body)
        new = [x for x in d if x not in seen_b]
        seen_b.update(d)
        rows_b.append([page, q, len(d), min(d) if d else "NULL", len(new)])
        if not new:
            break
    it.add(*md_table(["호출", "요청 기준일", "받은 일수", "가장 오래된 날", "새 날짜 수"], rows_b))
    it.add("", f"- 방법 B 누적 고유 날짜 수: **{len(seen_b)}**, 가장 오래된 날: {min(seen_b)}")
    return it


def decide_ref_date(ctx: Ctx) -> None:
    """비교 기준일: KIS 응답에 들어온 날짜 중 오늘이 아닌 가장 최근 날 (거래일만 응답에 옴)."""
    for rows in ctx.kis_first.values():
        dates = sorted({r.get("stck_bsop_date") for r in rows if r.get("stck_bsop_date")}, reverse=True)
        past = [d for d in dates if d < ctx.today]
        if past:
            ctx.ref_date = past[0]
            return


def _krx_row(ctx: Ctx, date: str, code: str) -> tuple[str, dict] | None:
    for market in ("KOSPI", "KOSDAQ"):
        for r in ctx.krx_trade.get((date, market), []):
            if short_code(r) == code:
                return market, r
    return None


def _sci(v):
    return "NULL" if v is None else f"{v:.3e}"


def item2_units(ctx: Ctx) -> Item:
    it = Item("2", "KIS 금액 필드 단위: KRX 거래대금과 비교")
    if not ctx.ref_date:
        it.status = SKIPPED
        it.add("- 비교 기준일을 정하지 못해 건너뜀 (KIS 첫 조회 실패)")
        return it
    it.add(f"- 비교 기준일: {ctx.ref_date} (KIS 응답에 있는 오늘이 아닌 가장 최근 거래일)",
           "- KIS 문서 표기: 개요 '금액(백만원)', `acml_tr_pbmn` 설명 '단위 : 백만원'. 명세서 4.2는 응답 예시상 원 단위로 의심.",
           "- 아래 비율은 계산값일 뿐이며, 단위 확정은 보고서를 보고 함께 정합니다.", "")
    tbl1, tbl2 = [], []
    for code in ctx.stocks:
        if code not in ctx.kis_first:
            res = ctx.kis.get(INVESTOR_BY_STOCK, investor_params(code, ctx.today))
            ctx.save_raw(f"kis_investor_{code}", res.body)
            ctx.kis_first[code] = (res.body.get("output2") or []) if is_ok(res.body) else []
        kis_row = next((r for r in ctx.kis_first[code] if r.get("stck_bsop_date") == ctx.ref_date), None)
        found = _krx_row(ctx, ctx.ref_date, code)
        market, krx_row = found if found else ("?", {})
        krx_val, krx_vol = num(krx_row.get("ACC_TRDVAL")), num(krx_row.get("ACC_TRDVOL"))
        k = kis_row or {}
        kis_val, kis_vol = num(k.get("acml_tr_pbmn")), num(k.get("acml_vol"))
        tbl1.append([code, name_of(krx_row), market, fmt(krx_vol), fmt(kis_vol), fmt(krx_val), fmt(kis_val),
                     fmt(ratio(krx_val, kis_val), 2)])
        for fld in ("frgn_ntby_tr_pbmn", "orgn_ntby_tr_pbmn", "prsn_ntby_tr_pbmn"):
            v = num(k.get(fld))
            tbl2.append([code, fld, fmt(v), _sci(ratio(v, krx_val)),
                         fmt(ratio(v * 1_000_000 if v is not None else None, krx_val), 4)])
    it.add("**거래량·거래대금 비교** (KRX ÷ KIS 가 1이면 같은 단위, 1,000,000 근처면 KIS가 백만 원)", "")
    it.add(*md_table(["종목", "이름", "시장", "KRX ACC_TRDVOL", "KIS acml_vol", "KRX ACC_TRDVAL",
                      "KIS acml_tr_pbmn", "KRX÷KIS 대금"], tbl1))
    it.add("", "**순매수 대금 ÷ KRX 거래대금** (백만 원이라고 가정하면 오른쪽 열이 -1~1 사이여야 상식적)", "")
    it.add(*md_table(["종목", "필드", "KIS 값", "값 ÷ KRX대금", "(값×1,000,000) ÷ KRX대금"], tbl2))
    return it


def item3_today(ctx: Ctx) -> Item:
    it = Item("3", "KIS 수급 당일 값: 실행 시각별 변동")
    snap = ctx.dirs["snapshots"] / f"kis_investor_today_{ctx.today}.jsonl"
    fetched = ctx.now.strftime("%H:%M:%S")
    lines = []
    for code in ctx.stocks:
        row = next((r for r in ctx.kis_first.get(code, []) if r.get("stck_bsop_date") == ctx.today), None)
        lines.append({"fetched_at": fetched, "code": code, "has_today": row is not None,
                      "values": {f: (row or {}).get(f) for f in SNAPSHOT_FIELDS}})
    with snap.open("a", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    it.add(f"- 오늘 개장 여부(휴장일조회 opnd_yn): {today_open(ctx) or '확인 안 됨'}",
           f"- 이번 실행 시각 {fetched}: 당일({ctx.today}) 행이 있는 종목 {sum(l['has_today'] for l in lines)}/{len(lines)}",
           f"- 오늘 실행 기록 파일: system/verify/snapshots/{snap.name}", "")
    history = [json.loads(x) for x in snap.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = []
    for code in ctx.stocks:
        prev = None
        for h in (h for h in history if h["code"] == code):
            if not h["has_today"]:
                rows.append([code, h["fetched_at"], "당일 행 없음", ""])
            else:
                changed = "" if prev is None else ("바뀜" if prev != h["values"] else "같음")
                rows.append([code, h["fetched_at"], fmt(num(h["values"].get("frgn_ntby_qty"))), changed])
                prev = h["values"]
    it.add("직전 기록(당일 행이 있는 기록) 대비 8개 필드(누적 거래량·대금, 외국인·기관·개인 순매수 수량·대금)가 바뀌었는지:", "")
    it.add(*md_table(["종목", "실행 시각", "외국인 순매수 수량", "직전 대비"], rows))
    if len({h["fetched_at"] for h in history}) < 2:
        it.add("", "- 아직 오늘 실행이 1회뿐입니다. 안내된 시각에 다시 실행하면 비교가 쌓입니다.")
    return it


def pick_samples(ctx: Ctx, limit: int = 3) -> dict[str, list[tuple[str, str]]]:
    """KRX 기본정보에서 스팩·리츠·우선주 후보를 고른다 (이름·필드 값 기준, 검증용)."""
    groups: dict[str, list[tuple[str, str]]] = {"스팩(이름)": [], "리츠·부동산(이름/SECUGRP)": [], "우선주(KIND)": []}
    for rows in ctx.krx_base.values():
        for r in rows:
            code, name = short_code(r), name_of(r)
            if not code:
                continue
            if "스팩" in name:
                groups["스팩(이름)"].append((code, name))
            elif "리츠" in name or "부동산" in str(r.get("SECUGRP_NM", "")):
                groups["리츠·부동산(이름/SECUGRP)"].append((code, name))
            elif "우선" in str(r.get("KIND_STKCERT_TP_NM", "")):
                groups["우선주(KIND)"].append((code, name))
    return {k: v[:limit] for k, v in groups.items()}


def item8_scty(ctx: Ctx) -> Item:
    it = Item("8", "KIS 주식기본조회: 스팩·리츠·우선주의 scty_grp_id_cd, stck_kind_cd")
    samples = pick_samples(ctx)
    targets = [("보통주(대조군)", ctx.stocks[0], "")] + [(g, c, n) for g, lst in samples.items() for c, n in lst]
    if len(targets) == 1:
        it.add("- KRX 기본정보에서 스팩·리츠·우선주 후보를 찾지 못했습니다 (7번 결과 확인). 대조군만 조회합니다.")
    rows = []
    for group, code, name in targets:
        body = ctx.kis.get(STOCK_INFO, stock_info_params(code)).body
        ctx.save_raw(f"kis_stockinfo_{code}", body)
        if not is_ok(body):
            rows.append([group, code, name, "실패", body.get("msg_cd"), "", "", ""])
            continue
        o = body.get("output") or {}
        rows.append([group, code, name or o.get("prdt_abrv_name", ""), o.get("scty_grp_id_cd"),
                     o.get("stck_kind_cd"), o.get("mket_id_cd"), o.get("reits_kind_cd"), o.get("admn_item_yn")])
    it.add(*md_table(["구분", "종목", "이름", "scty_grp_id_cd", "stck_kind_cd", "mket_id_cd", "reits_kind_cd", "admn_item_yn"], rows))
    return it


# ---------------------------------------------------------------------------
# 10. KIS 당일 거래량·대금 기록 -> 다음 날 KRX와 비교 (애프터마켓 포함 여부)
# ---------------------------------------------------------------------------
DAILY_VALUES_FILE = "kis_daily_values.jsonl"
COMPARE_MAX_DATES = 5


def _load_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def item10_after_market(ctx: Ctx, window_ok: bool) -> Item:
    it = Item("10", "KIS 당일 acml_vol·acml_tr_pbmn vs 다음 날 KRX ACC_TRDVOL·ACC_TRDVAL (애프터마켓 포함 여부)")
    path = ctx.dirs["snapshots"] / DAILY_VALUES_FILE
    fetched = ctx.now.strftime("%Y-%m-%d %H:%M:%S")
    if window_ok and ctx.kis_first:
        n = 0
        with path.open("a", encoding="utf-8") as f:
            for code in ctx.stocks:
                row = next((r for r in ctx.kis_first.get(code, []) if r.get("stck_bsop_date") == ctx.today), None)
                if row:
                    n += 1
                    f.write(json.dumps({"date": ctx.today, "code": code, "recorded_at": fetched,
                                        "acml_vol": row.get("acml_vol"), "acml_tr_pbmn": row.get("acml_tr_pbmn"),
                                        "stck_clpr": row.get("stck_clpr")}, ensure_ascii=False) + "\n")
        it.add(f"- 이번 실행에서 당일({ctx.today}) 값 {n}/{len(ctx.stocks)}종목 기록 → system/verify/snapshots/{DAILY_VALUES_FILE}")
    else:
        it.add("- 이번 실행은 KIS 수급 조회 가능 시각(15:40~23:59)이 아니어서 당일 값을 기록하지 않았습니다.")
    # 날짜·종목별로 가장 늦게 기록한 값
    latest: dict[tuple[str, str], dict] = {}
    for rec in _load_jsonl(path):
        latest[(rec["date"], rec["code"])] = rec
    past_dates = sorted({d for d, _ in latest if d < ctx.today}, reverse=True)[:COMPARE_MAX_DATES]
    if not past_dates:
        it.add("- 비교할 지난 기록이 아직 없습니다. 오늘 20:30쯤 실행해 기록하고, 다음 날 다시 실행하면 비교됩니다.")
        return it
    if not ctx.krx:
        it.status = SKIPPED
        it.add("- KRX 키가 없어 비교를 건너뜀")
        return it
    from .krx_items import load_trade
    rows = []
    for d in past_dates:
        load_trade(ctx, d)
        for code in ctx.stocks:
            rec = latest.get((d, code))
            if not rec:
                continue
            found = _krx_row(ctx, d, code)
            krx = found[1] if found else {}
            kv, kp = num(rec.get("acml_vol")), num(rec.get("acml_tr_pbmn"))
            xv, xp = num(krx.get("ACC_TRDVOL")), num(krx.get("ACC_TRDVAL"))
            rows.append([d, code, rec["recorded_at"][11:], fmt(kv), fmt(xv),
                         fmt(None if kv is None or xv is None else kv - xv), fmt(kp), fmt(xp),
                         fmt(ratio(xp, kp), 4)])
    it.add("", "거래량 차이 = KIS − KRX. 0이면 같은 범위, 0이 아니면 한쪽에 애프터마켓 등 다른 거래가 섞였을 가능성. "
           "대금 비율(KRX÷KIS)은 단위 확인용입니다 (1이면 원, 1,000,000 근처면 KIS가 백만 원).", "")
    it.add(*md_table(["날짜", "종목", "KIS 기록 시각", "KIS acml_vol", "KRX ACC_TRDVOL", "거래량 차이",
                      "KIS acml_tr_pbmn", "KRX ACC_TRDVAL", "대금 KRX÷KIS"], rows))
    return it


# ---------------------------------------------------------------------------
# 11. KIS 업종 일자별지수(FHPUP02120000) 코스피 0001·코스닥 1001 -> 다음 날 KRX 지수와 비교
# ---------------------------------------------------------------------------
INDEX_VALUES_FILE = "kis_index_values.jsonl"
KIS_INDEXES = {"0001": "코스피", "1001": "코스닥"}
INDEX_FIELDS = ["bstp_nmix_prpr", "bstp_nmix_oprc", "bstp_nmix_hgpr", "bstp_nmix_lwpr", "acml_vol", "acml_tr_pbmn"]
KRX_INDEX_SETTING = {"0001": "KRX_INDEX_KOSPI_URL", "1001": "KRX_INDEX_KOSDAQ_URL"}


def item11_index(ctx: Ctx) -> Item:
    from ..kis import INDEX_DAILY, index_daily_params
    from ..krx import rows as krx_rows

    it = Item("11", "KIS 업종 일자별지수(FHPUP02120000): 코스피(0001)·코스닥(1001) 당일 값 vs 다음 날 KRX 지수")
    path = ctx.dirs["snapshots"] / INDEX_VALUES_FILE
    fetched = ctx.now.strftime("%Y-%m-%d %H:%M:%S")
    rows_now = []
    with path.open("a", encoding="utf-8") as f:
        for code, name in KIS_INDEXES.items():
            body = ctx.kis.get(INDEX_DAILY, index_daily_params(code, ctx.today)).body
            ctx.save_raw(f"kis_index_{code}_{ctx.today}", body)
            if not is_ok(body):
                rows_now.append([code, name, "실패", f"{body.get('msg_cd')} {body.get('msg1')}"] + [""] * 4)
                continue
            out2 = body.get("output2") or []
            dates = [r.get("stck_bsop_date") for r in out2 if r.get("stck_bsop_date")]
            today_row = next((r for r in out2 if r.get("stck_bsop_date") == ctx.today), None)
            if today_row:
                f.write(json.dumps({"date": ctx.today, "index": code, "recorded_at": fetched,
                                    **{k: today_row.get(k) for k in INDEX_FIELDS}}, ensure_ascii=False) + "\n")
            r = today_row or {}
            rows_now.append([code, name, f"{len(dates)}일치 ({min(dates) if dates else 'NULL'}~{max(dates) if dates else 'NULL'})",
                             "있음" if today_row else "없음", r.get("bstp_nmix_prpr", ""), r.get("bstp_nmix_hgpr", ""),
                             r.get("bstp_nmix_lwpr", ""), r.get("acml_tr_pbmn", "")])
    it.add(f"- 요청 값: FID_PERIOD_DIV_CODE=D, FID_COND_MRKT_DIV_CODE=U, FID_INPUT_DATE_1={ctx.today} (kis_api_reference.xlsx)", "")
    it.add(*md_table(["업종코드", "이름", "응답 일수(범위)", "당일 행", "종가(현재가)", "고가", "저가", "acml_tr_pbmn"], rows_now))

    latest: dict[tuple[str, str], dict] = {}
    for rec in _load_jsonl(path):
        latest[(rec["date"], rec["index"])] = rec
    past = sorted({d for d, _ in latest if d < ctx.today}, reverse=True)[:COMPARE_MAX_DATES]
    if not past:
        it.add("", "- 비교할 지난 기록이 아직 없습니다. 다음 날 다시 실행하면 KRX 지수와 비교됩니다.")
        return it
    if not ctx.krx:
        it.add("", "- KRX 키가 없어 비교를 건너뜀")
        return it
    it.add("", "**지난 기록 vs KRX 지수** (KRX 필드명은 실측 전이라 이름이 정확히 일치하는 행의 전체 필드를 보여줍니다)", "")
    cmp_rows = []
    for d in past:
        for code, name in KIS_INDEXES.items():
            rec = latest.get((d, code))
            if not rec:
                continue
            url = (ctx.settings.get(KRX_INDEX_SETTING[code]) or "").strip()
            if not url:
                cmp_rows.append([d, name, rec.get("bstp_nmix_prpr"), "설정 필요 (KRX 지수 URL 비어 있음)"])
                continue
            body = ctx.krx.get_url(url, d)
            ctx.save_raw(f"krx_index_{code}_{d}", body)
            match = [r for r in krx_rows(body) if str(r.get("IDX_NM", "")).strip() == name]
            krx_txt = "; ".join(f"{k}={v}" for k, v in match[0].items()) if match else \
                f"IDX_NM='{name}' 행 없음 (행 {len(krx_rows(body))}개)"
            cmp_rows.append([d, name, rec.get("bstp_nmix_prpr"), krx_txt])
    it.add(*md_table(["날짜", "지수", "KIS 종가(bstp_nmix_prpr)", "KRX 같은 날 행"], cmp_rows))
    return it
