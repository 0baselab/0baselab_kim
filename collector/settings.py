"""설치 폴더의 config/settings.env (명세서 2.4, 11.1).

파일이 없으면 만들고, 새 버전에 새 항목이 생기면 기본값만 덧붙인다.
사용자가 바꾼 값은 덮어쓰지 않는다.
"""
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

# (키, 기본값, 설명)
DEFAULTS: list[tuple[str, str, str]] = [
    ("DATA_DIR", "", "데이터 폴더. 비우면 바탕화면/주식데이터수집"),
    ("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443", "KIS 실전 도메인 (KIS 문서)"),
    ("KIS_CALLS_PER_SEC", "10", "KIS 초당 호출 수. 한도 초과(EGW00201)가 나면 실행 중 자동으로 반씩 줄임"),
    ("KRX_BASE_URL", "https://data-dbg.krx.co.kr/svc/apis", "KRX Open API 기본 주소"),
    ("KRX_INDEX_KOSPI_URL", "https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd",
     "KOSPI 시리즈 일별시세정보 요청 URL (사용자가 KRX 사이트에서 확인). 비우면 '설정 필요'로 건너뜀"),
    ("KRX_INDEX_KOSDAQ_URL", "https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd",
     "KOSDAQ 시리즈 일별시세정보 요청 URL (사용자가 KRX 사이트에서 확인). 비우면 '설정 필요'로 건너뜀"),
    ("KRX_CALLS_PER_SEC", "2", "KRX 초당 호출 수"),
    ("DART_BASE_URL", "https://opendart.fss.or.kr/api", "Open DART 기본 주소"),
    ("DART_DAILY_LIMIT", "18000", "DART 하루 호출 한도"),
    ("KRX_RETENTION_DAYS", "250", "KRX 시세·지수 보관(수집) 거래일 수"),
    ("KIS_RETENTION_DAYS", "120", "KIS 수급 보관(수집) 거래일 수"),
    ("KIS_STOCKINFO_REFRESH_DAYS", "7", "KIS 주식기본조회 전 종목 재조회 주기(일)"),
    ("LOG_KEEP_DAYS", "90", "로그 보관 일수"),
    ("BACKUP_KEEP", "7", "DB 스냅샷 보관 개수"),
    ("UNCLASSIFIED_TOP_N", "50", "브리핑 5_미분류: 각 기준별 상위 종목 수"),
    ("VERIFY_STOCKS", "005930,000660,005380,247540,196170",
     "실측 검증 종목 (코스피 3: 삼성전자·SK하이닉스·현대차 / 코스닥 2: 에코프로비엠·알테오젠)"),
    ("VERIFY_DART_STOCKS", "005930,000660,247540", "DART 재무 검증 종목 (코스피 2, 코스닥 1)"),
]


# 예전 기본값을 그대로 쓰고 있을 때만 새 기본값으로 바꾼다. 사용자가 바꾼 값은 건드리지 않는다.
# (키, 예전 기본값, 새 기본값, 바꾼 버전)
MIGRATIONS: list[tuple[str, str, str, str]] = [
    ("KIS_CALLS_PER_SEC", "15", "10", "0.1.2"),
]


def settings_path(install: Path) -> Path:
    return install / "config" / "settings.env"


def load_settings(install: Path) -> dict[str, str]:
    path = settings_path(install)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = dotenv_values(path) if path.exists() else {}
    missing = [d for d in DEFAULTS if d[0] not in existing]
    if missing:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            if path.stat().st_size and not path.read_text(encoding="utf-8").endswith("\n"):
                f.write("\n")
            for key, value, desc in missing:
                f.write(f"# {desc}\n{key}={value}\n")
        existing = dotenv_values(path)
    migrate(path, existing)
    return {k: (v or "") for k, v in existing.items()}


def migrate(path: Path, values: dict) -> list[str]:
    """예전 기본값 -> 새 기본값. 바꾼 항목 설명 목록을 돌려준다."""
    changed = []
    text = path.read_text(encoding="utf-8")
    for key, old, new, ver in MIGRATIONS:
        if (values.get(key) or "").strip() == old:
            text = text.replace(f"{key}={old}\n", f"{key}={new}\n", 1)
            values[key] = new
            changed.append(f"{key}: {old} -> {new} ({ver} 기본값 변경)")
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed
