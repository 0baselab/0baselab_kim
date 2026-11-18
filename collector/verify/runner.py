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
from .common import FAILED, SKIPPED, Ctx, Item, md_table

log = logging.getLogger("collector")

RERUN_TIMES = ["15:30", "15:45", "16:30", "18:00"]

CHECKLIST = [
    ("1", "KIS 종목별 투자자매매동향: 1회 호출 일수, 연속 조회 깊이"),
    ("2", "KIS 금액 필드 단위"),
    ("3", "KIS 수급 당일 값 변동 (여러 시각 반복 실행)"),
    ("4", "KIS 실제 초당 한도 → 한도 시험은 하지 않고, 실행 중 EGW00201 발생 기록으로 대체"),
    ("5", "KRX 지수 시리즈 경로·지수 이름"),
    ("6", "KRX 당일 데이터 제공 시각 (여러 시각 반복 실행)"),
    ("7", "KRX 스팩·리츠·우선주 필드 값"),
    ("8", "KIS scty_grp_id_cd (스팩이 ST인지)"),
    ("9", "DART thstrm_amount 의미"),
    ("H", "(추가) KIS 휴장일조회 응답 일수"),
]


def _run(items: list[Item], no: str, title: str, fn: Callable[[], Item], needs) -> None:
    if needs is None:
        items.append(Item(no, title, SKIPPED, ["- 필요한 키가 없어 건너뜀"]))
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
    steps = [
        ("H", "KIS 휴장일조회", lambda: kis_items.item_holiday(ctx), kis),
        ("1", "KIS 수급 조회 일수", lambda: kis_items.item1_depth(ctx), kis),
        ("6", "KRX 당일 데이터", lambda: krx_items.item6_today(ctx), krx),
    ]
    later = [
        ("2", "KIS 금액 단위", lambda: kis_items.item2_units(ctx), kis and krx),
        ("3", "KIS 당일 값 변동", lambda: kis_items.item3_today(ctx), kis),
        ("5", "KRX 지수 시리즈", lambda: krx_items.item5_index(ctx), krx),
        ("7", "KRX 기본정보 필드", lambda: krx_items.item7_fields(ctx), krx),
        ("8", "KIS 증권그룹 코드", lambda: kis_items.item8_scty(ctx), kis),
        ("9", "DART 재무 필드", lambda: dart_items.item9_dart(ctx), dart),
    ]
    try:
        for i, (no, title, fn, needs) in enumerate(steps + later):
            if i == len(steps):
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
            out(f"  [{no}] {title} ...")
            _run(items, no, title, fn, needs)
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
          f"- 설정 속도: 초당 {ctx.settings.get('KIS_CALLS_PER_SEC')}건. 한도 시험은 하지 않았습니다."]
    events = ctx.kis.rate_limit_events if ctx.kis else []
    L.append(f"- 발생 횟수: **{len(events)}**")
    if events:
        L += [""] + md_table(["시각", "TR ID", "직전 1초 호출 수"], [[e.at, e.tr_id, e.recent_rate] for e in events])
    for no in ["H", "1", "2", "3", "5", "6", "7", "8", "9"]:
        if no in by_no:
            it = by_no[no]
            L += ["", f"## {it.no}. {it.title}", "", f"상태: **{it.status}**", ""] + it.lines
    L += ["", "## 다시 실행할 시각 (3·6번 항목)", "",
          f"장이 열린 날 {', '.join(RERUN_TIMES)} 무렵에 각각 다시 실행하세요. "
          "15:30은 KIS 문서상 당일 조회가 열리기 전(15:40) 시각입니다. 같은 날의 결과는 snapshots/ 기록에 쌓여 "
          "마지막 보고서에 모두 표시됩니다. 휴장일조회는 같은 날 캐시를 쓰므로 다시 호출하지 않습니다."]
    return redact("\n".join(L)) + "\n"
