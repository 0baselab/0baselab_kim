"""DART 실측 항목: 분기보고서의 thstrm_amount가 3개월 값인지 누적 값인지."""
from __future__ import annotations

from ..dart import NO_DATA, REPORT_CODES
from .common import FAILED, Ctx, Item, md_table

# 비교용으로 보여줄 표준 계정 ID (표시용 필터일 뿐, 없으면 그 표의 앞쪽 행을 보여준다)
FOCUS_ACCOUNTS = {
    "ifrs-full_Revenue", "dart_OperatingIncomeLoss", "ifrs-full_ProfitLoss",
    "ifrs-full_CashFlowsFromUsedInOperatingActivities",
}
SJ_DIVS = ("IS", "CIS", "CF")


def item9_dart(ctx: Ctx) -> Item:
    it = Item("9", "DART 재무: thstrm_amount가 3개월 값인지 누적 값인지 (손익계산서·현금흐름표)")
    year = str(ctx.now.year - 1)
    corp = ctx.dart.corp_codes()
    it.add(f"- 사업연도 {year}, 보고서 4종(1분기·반기·3분기·사업보고서), 연결(CFS) 우선·없으면 별도(OFS)",
           "- 1분기는 두 값이 같아야 정상. 반기·3분기에서 thstrm_amount와 thstrm_add_amount가 다르면 thstrm_amount가 3개월 값일 가능성.", "")
    for code in ctx.dart_stocks:
        cc = corp.get(code)
        it.add(f"### 종목 {code} (corp_code {cc or '못 찾음'})", "")
        if not cc:
            it.status = FAILED
            continue
        table = []
        for rc, rname in REPORT_CODES.items():
            body, fs = None, "CFS"
            for fs in ("CFS", "OFS"):
                body = ctx.dart.fs_all(cc, year, rc, fs)
                ctx.save_raw(f"dart_fs_{code}_{year}_{rc}_{fs}", body)
                if body.get("status") != NO_DATA:
                    break
            if body.get("status") != "000":
                table.append([rname, fs, "-", f"status {body.get('status')} {body.get('message', '')}", "", "", ""])
                continue
            items = [x for x in body.get("list", []) if x.get("sj_div") in SJ_DIVS]
            focus = [x for x in items if x.get("account_id") in FOCUS_ACCOUNTS] or items[:4]
            for x in focus:
                table.append([rname, fs, x.get("sj_div"), x.get("account_id"), x.get("thstrm_nm"),
                              x.get("thstrm_amount"), x.get("thstrm_add_amount")])
        it.add(*md_table(["보고서", "구분", "sj_div", "account_id", "thstrm_nm", "thstrm_amount", "thstrm_add_amount"], table))
        it.add("")
    return it
