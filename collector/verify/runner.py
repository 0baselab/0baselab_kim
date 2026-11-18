"""0단계 실측 검증 실행기 (명세서 12.2).

DB에 쓰지 않고, 원본 응답과 보고서만 데이터 폴더 system/verify/에 남긴다.
항목 하나가 실패해도 나머지는 계속 진행한다. 결론은 내리지 않고 숫자만 남긴다.
"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import kis as kis_mod
from ..dart import DartClient
from ..http import ApiClient
from ..kis import KisAuthError, KisClient
from ..krx import KrxClient
from ..secrets import redact, sensitive_values
from ..timeutil import stamp
from ..version import __version__
from . import dart_items, kis_items, krx_items
from .common import FAILED, SKIPPED, Ctx, Item, fmt, md_table

log = logging.getLogger("collector")

RERUN_TIMES = ["15:45", "16:30", "18:00", "20:30"]

CHECKLIST = [
    ("1", "KIS 종목별 투자자매매동향: 1회 호출 일수, 연속 조회 깊이"),
    ("2", "KIS 금액 필드 단위"),
    ("3", "KIS 수급 당일 값 변동 (여러 시각 반복 실행)"),
    ("4", "KIS 실제 초당 한도 → 시험 없이 EGW00201 발생 기록, 발생 시 속도 자동 반감"),
    ("5", "KRX 지수 시리즈 경로·지수 이름"),
    ("6", "KRX 당일 데이터 제공 시각 (여러 시각 반복 실행)"),
    ("7", "KRX 스팩·리츠·우선주 필드 값"),
    ("8", "KIS scty_grp_id_cd (스팩이 ST인지)"),
    ("9", "DART thstrm_amount 의미"),
    ("H", "(추가) KIS 휴장일조회 응답 일수"),
    ("10", "(추가) KIS 당일 거래량·대금 vs 다음 날 KRX (애프터마켓 포함 여부)"),
    ("11", "(추가) KIS 업종지수 당일 값 vs 다음 날 KRX 지수"),
]


def _run(items: list[Item], no: str, title: str, fn: Callable[[], Item], needs, reason=None) -> None:
    if not needs:
        items.append(Item(no, title, SKIPPED, [f"- {reason or '필요한 키가 없어 건너뜀'}"]))
        return
    try:
        items.append(fn())
        log.info("항목 %s 완료", no)
    except KisAuthError:
        raise
    except Exception as exc:  # 한 항목 실패가 전체를 멈추지 않게 (명세서 11장)
        log.error("항목 %s 실패: %s\n%s", no, exc, traceback.format_exc())
        items.append(Item(no, title, FAILED, [f"- 오류: {type(exc).__name__}: {redact(str(exc))}"]))


def run_verify(*, now: datetime, settings: dict, keys: dict, dirs: dict[str, Path],
               http_factory: Callable[[str, float], ApiClient] | None = None,
               out: Callable[[str], None] = print) -> Path:
    http_factory = http_factory or (lambda name, rate: ApiClient(name, rate))
    st = stamp(now)
    raw_dir = dirs["verify"] / f"raw_{st}"
    kis_api = http_factory("KIS", float(settings.get("KIS_CALLS_PER_SEC") or 15))
    krx_api = http_factory("KRX", float(settings.get("KRX_CALLS_PER_SEC") or 2))
    dart_api = http_factory("DART", 5)
    kis = KisClient(kis_api, settings["KIS_BASE_URL"], keys["KIS_APP_KEY"], keys["KIS_APP_SECRET"],
                    dirs["system"] / "kis_token.json", now=lambda: now) \
        if keys.get("KIS_APP_KEY") and keys.get("KIS_APP_SECRET") else None
    krx = KrxClient(krx_api, settings["KRX_BASE_URL"], keys["KRX_AUTH_KEY"]) if keys.get("KRX_AUTH_KEY") else None
    dart = DartClient(dart_api, settings["DART_BASE_URL"], keys["DART_API_KEY"]) if keys.get("DART_API_KEY") else None
    split = lambda s: [c.strip() for c in (s or "").split(",") if c.strip()]
    ctx = Ctx(now=now, settings=settings, dirs=dirs, raw_dir=raw_dir, kis=kis, krx=krx, dart=dart,
              stocks=split(settings.get("VERIFY_STOCKS")), dart_stocks=split(settings.get("VERIFY_DART_STOCKS")))

    items: list[Item] = []
    auth_error = None
    window_ok = kis_mod.investor_window_ok(now)
    window_reason = (f"실행 시각 {now.strftime('%H:%M')}은 KIS 종목별 투자자매매동향 조회 가능 시각(15:40~23:59)이 아님. "
                     "00:00~15:40에는 OPSQ2001 TIME LIMIT 오류로 과거 데이터까지 조회 불가 (0.1.1 실측)")
    kis_inv = kis if window_ok else None
    # (번호, 제목, 함수, 필요한 클라이언트, 건너뛸 때 이유)
    before = [
        ("H", "KIS 휴장일조회", lambda: kis_items.item_holiday(ctx), kis, None),
        ("1", "KIS 수급 조회 일수", lambda: kis_items.item1_depth(ctx), kis_inv, window_reason),
    ]
    after = [
        ("6", "KRX 당일 데이터", lambda: krx_items.item6_today(ctx), krx, None),
        ("2", "KIS 금액 단위", lambda: kis_items.item2_units(ctx), kis_inv and krx, window_reason),
        ("3", "KIS 당일 값 변동", lambda: kis_items.item3_today(ctx), kis_inv, window_reason),
        ("10", "KIS 당일 거래량·대금 vs KRX", lambda: kis_items.item10_after_market(ctx, window_ok), kis, None),
        ("11", "KIS 업종지수 vs KRX 지수", lambda: kis_items.item11_index(ctx), kis, None),
        ("5", "KRX 지수 시리즈", lambda: krx_items.item5_index(ctx), krx, None),
        ("7", "KRX 기본정보 필드", lambda: krx_items.item7_fields(ctx), krx, None),
        ("8", "KIS 증권그룹 코드", lambda: kis_items.item8_scty(ctx), kis, None),
        ("9", "DART 재무 필드", lambda: dart_items.item9_dart(ctx), dart, None),
    ]
    try:
        for no, title, fn, needs, reason in before:
            out(f"  [{no}] {title} ...")
            _run(items, no, title, fn, needs, reason)
        kis_items.decide_ref_date(ctx)
        if not ctx.ref_date and krx:
            try:
                ctx.ref_date = krx_items.fallback_ref_date(ctx)
            except Exception as exc:
                log.error("기준일 찾기 실패: %s", exc)
        if ctx.ref_date and krx:
            try:
                krx_items.load_trade(ctx, ctx.ref_date)
            except Exception as exc:
                log.error("KRX 기준일 매매정보 실패: %s", exc)
        for no, title, fn, needs, reason in after:
            out(f"  [{no}] {title} ...")
            _run(items, no, title, fn, needs, reason)
    except KisAuthError as exc:  # 인증 실패만 즉시 중단 (명세서 11.2)
        auth_error = str(exc)
        out(f"중단: {auth_error}")

    report = build_report(ctx, items, auth_error, kis_api, krx_api, dart_api)
    path = dirs["verify"] / f"실측보고서_{st}.md"
    path.write_text(report, encoding="utf-8")
    leaks = scan_for_secrets([path, raw_dir, dirs["snapshots"]])
    if leaks:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n\n## 경고: 키·토큰 문자열 발견\n\n아래 파일은 저장소에 올리지 마세요.\n\n"
                    + "\n".join(f"- {p.name}" for p in leaks) + "\n")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n\n## 자동 보안 검사\n\n- 보고서·원본 응답·기록 파일에서 키·토큰 문자열이 발견되지 않았습니다.\n")
    return path


def scan_for_secrets(targets: list[Path]) -> list[Path]:
    secrets = [s for s in sensitive_values() if s]
    found = []
    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files += [p for p in t.rglob("*") if p.is_file()]
        elif t.exists():
            files.append(t)
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        if any(s in text for s in secrets):
            found.append(p)
    return found


def build_report(ctx: Ctx, items: list[Item], auth_error, kis_api, krx_api, dart_api) -> str:
    by_no = {i.no: i for i in items}
    L = [f"# 실측보고서 {ctx.now.strftime('%Y-%m-%d %H:%M:%S')} (Asia/Seoul)", "",
         f"- 프로그램 버전: {__version__}",
         f"- 검증 종목: {', '.join(ctx.stocks)} / DART: {', '.join(ctx.dart_stocks)}",
         f"- 비교 기준일: {ctx.ref_date or '정하지 못함'}",
         f"- 원본 응답 폴더: system/verify/{ctx.raw_dir.name}/",
         f"- 호출 수: KIS {kis_api.calls}, KRX {krx_api.calls}, DART {dart_api.calls}",
         "- 이 보고서는 숫자만 기록합니다. 단위·의미의 확정은 보고서를 보고 함께 정합니다.", ""]
    if auth_error:
        L += [f"**인증 실패로 중단:** {redact(auth_error)}", ""]
    L += ["## 확인 항목 요약 (명세서 12.2)", ""]
    L += md_table(["#", "항목", "상태"],
                  [[no, title, by_no[no].status if no in by_no else ("기록됨" if no == "4" else "실행 안 됨")]
                   for no, title in CHECKLIST])
    L += ["", "## 4. KIS 호출 한도 (EGW00201) 발생 기록", "",
          f"- 시작 속도: 초당 {ctx.settings.get('KIS_CALLS_PER_SEC')}건 (호출 간격을 고르게 벌림). 한도 시험은 하지 않았습니다.",
          "- 한도 초과가 나면 속도를 반으로 줄이고 1초 쉰 뒤 재시도합니다. 줄인 속도는 이번 실행 끝까지 유지합니다.",
          f"- 실행이 끝났을 때 속도: 초당 {fmt(ctx.kis.api.limiter.per_sec, 2) if ctx.kis else 'NULL'}건"]
    events = ctx.kis.rate_limit_events if ctx.kis else []
    L.append(f"- 발생 횟수: **{len(events)}**")
    if events:
        L += [""] + md_table(["시각", "TR ID", "직전 1초 호출 수", "설정 속도", "줄인 속도"], [[e.at, e.tr_id, e.recent_rate, fmt(e.rate_before, 2), fmt(e.rate_after, 2)] for e in events])
    for no in ["H", "1", "2", "3", "10", "11", "5", "6", "7", "8", "9"]:
        if no in by_no:
            it = by_no[no]
            L += ["", f"## {it.no}. {it.title}", "", f"상태: **{it.status}**", ""] + it.lines
    L += ["", "## 다시 실행할 시각 (3·6번 항목)", "",
          f"장이 열린 날 {', '.join(RERUN_TIMES)} 무렵에 각각 다시 실행하세요. "
          "15:40 이전에는 KIS 수급 항목이 자동으로 건너뛰어집니다. 10·11번은 장이 열린 날 밤(20:30쯤)에 기록하고 "
          "다음 날 한 번 더 실행해야 비교됩니다. 같은 날의 결과는 snapshots/ 기록에 쌓여 "
          "마지막 보고서에 모두 표시됩니다. 휴장일조회는 같은 날 캐시를 쓰므로 다시 호출하지 않습니다."]
    return redact("\n".join(L)) + "\n"
