"""하루치 분봉 수집 (페이지 처리).

153000부터 호출 → 받은 행 중 가장 이른 시각 -1분으로 다음 호출 →
090000 도달, 해당 날짜 행 없음, 또는 이전 날짜 행이 섞여 나오면(그날의 시작에 도달) 종료.
다른 날짜 행은 버린다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

START_HOUR = "153000"
END_HOUR = "090000"
MAX_PAGES = 10  # 정규장 약 390봉 / 120 = 4페이지. 무한 반복 방지용 상한.


def minus_one_minute(hhmmss: str) -> str:
    return (datetime.strptime(hhmmss, "%H%M%S") - timedelta(minutes=1)).strftime("%H%M%S")


def to_int(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def minute_params(code: str, date: str, hour: str, market: str = "J", pw_incu: str = "N") -> dict:
    return {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": hour,
        "FID_INPUT_DATE_1": date,
        "FID_PW_DATA_INCU_YN": pw_incu,
        "FID_FAKE_TICK_INCU_YN": "",
    }


def parse_row(r: dict) -> dict:
    return {
        "time": r.get("stck_cntg_hour"),
        "open": to_int(r.get("stck_oprc")),
        "high": to_int(r.get("stck_hgpr")),
        "low": to_int(r.get("stck_lwpr")),
        "close": to_int(r.get("stck_prpr")),
        "volume": to_int(r.get("cntg_vol")),
        "acml_value_krw": to_int(r.get("acml_tr_pbmn")),
    }


@dataclass
class DayResult:
    code: str
    date: str
    bars: list[dict] = field(default_factory=list)  # 시각 오름차순
    calls: int = 0
    other_date_rows: int = 0


def fetch_day(client, code: str, date: str, market: str = "J", pw_incu: str = "N",
              start: str = START_HOUR, end: str = END_HOUR, max_pages: int = MAX_PAGES) -> DayResult:
    res = DayResult(code, date)
    bars: dict[str, dict] = {}
    hour = start
    while res.calls < max_pages:
        body = client.minute(minute_params(code, date, hour, market, pw_incu))
        res.calls += 1
        rows = [r for r in (body.get("output2") or []) if isinstance(r, dict)]
        mine = [r for r in rows if r.get("stck_bsop_date") == date and r.get("stck_cntg_hour")]
        older = [r for r in rows if (r.get("stck_bsop_date") or "") and r.get("stck_bsop_date") < date]
        res.other_date_rows += len(rows) - len(mine)
        new = 0
        for r in mine:
            t = r["stck_cntg_hour"]
            if t not in bars:
                bars[t] = parse_row(r)
                new += 1
        if not mine or new == 0:
            break
        earliest = min(r["stck_cntg_hour"] for r in mine)
        if earliest <= end or older:
            break
        hour = minus_one_minute(earliest)
    res.bars = [bars[t] for t in sorted(bars)]
    return res


def minute_values(bars: list[dict]) -> list[int | None]:
    """누적 거래대금 → 1분 거래대금. 하루 첫 봉은 그 값 자체.

    누적값이 없거나 앞 봉보다 줄어든 경우(누적이 아님)는 NULL.
    """
    out: list[int | None] = []
    prev: int | None = None
    for i, b in enumerate(bars):
        a = b.get("acml_value_krw")
        if a is None:
            out.append(None)
            continue
        if i == 0:
            out.append(a)
        elif prev is None or a < prev:
            out.append(None)
        else:
            out.append(a - prev)
        prev = a
    return out
