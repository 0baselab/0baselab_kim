"""설정·경로·키.

- 키: 설치 폴더 secret/keys.env (KIS_APP_KEY, KIS_APP_SECRET). 화면·로그에 쓰지 않는다.
- 설정: 설치 폴더 config/danta_settings.env (없으면 기본값). 배포 zip에 넣지 않는다.
"""
from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from collector.paths import desktop_dir

KEY_NAMES = ("KIS_APP_KEY", "KIS_APP_SECRET")
DATA_FOLDER_NAME = "단타분석"
SOURCE_DB_PARTS = ("주식데이터수집", "db", "market.sqlite")

DEFAULTS: dict[str, str] = {
    "DATA_DIR": "",                # 비우면 바탕화면/단타분석
    "SOURCE_DB": "",               # 비우면 바탕화면/주식데이터수집/db/market.sqlite (읽기 전용)
    "KIS_BASE_URL": "https://openapi.koreainvestment.com:9443",
    "MARKET": "J",                 # J=KRX, NX=NXT, UN=통합
    "PW_DATA_INCU_YN": "N",        # 과거 데이터 포함 여부 (문서에 설명 없음, 실측으로 확인)
    "RATE": "10",                  # 초당 호출 수 (실측 전 기본값)
    "BACKOFF_RETRIES": "5",        # EGW00201 재시도 횟수 (1,2,4,8,16초 대기)
    "BACKOFF_BASE_SEC": "1",
    "TOKEN_MIN_ISSUE_GAP_SEC": "61",  # 토큰 발급 1분 1회 제한(알려진 값, 확인 필요)
    "TOP_VALUE": "100",
    "TOP_CHANGE": "50",
    "RATETEST_STAGES": "5,10,15,20,25,30",
    "RATETEST_SECONDS": "20",
    "MAX_FAILURES": "20",
    "ACML_IS_CUMULATIVE": "",
    "DAILY_RATE": "5",             # daily 기본 초당 호출 수 (실측 한도 약 6)
    "MAX_RETRY": "3",              # 같은 (종목, 날짜)가 연속 이 횟수만큼 실패하면 재시도 중단
    "COLLECTOR_WAIT_MINUTES": "60",  # 기존 수집기 실행 중이면 최대 대기 시간
    "THEME_FILE": "",              # 비우면 기존 수집기 데이터 폴더/themes/theme_master.csv
    "COLLECTOR_LOCK": "",          # 비우면 기존 수집기 데이터 폴더/system/collector.lock      # probe로 누적 확인 후 Y로 바꾸면 1분 거래대금(value_krw)을 계산해 저장
}


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def settings_path(install: Path) -> Path:
    return install / "config" / "danta_settings.env"


def keys_path(install: Path) -> Path:
    return install / "secret" / "keys.env"


@dataclass
class Config:
    values: dict[str, str]
    data_dir: Path
    source_db: Path

    def get(self, name: str) -> str:
        return self.values[name]

    def num(self, name: str) -> float:
        return float(self.values[name])

    def int(self, name: str) -> int:
        return int(float(self.values[name]))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "danta.sqlite"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "system" / "kis_token_danta.json"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def acml_is_cumulative(self) -> bool:
        return self.values["ACML_IS_CUMULATIVE"].strip().upper() in ("Y", "1", "TRUE")


def load_config(install: Path | None = None, platform: str | None = None) -> Config:
    install = install or install_dir()
    vals = dict(DEFAULTS)
    p = settings_path(install)
    if p.exists():
        for k, v in dotenv_values(p).items():
            if k in vals and v is not None:
                vals[k] = v.strip()
    desk = desktop_dir(platform)
    data = Path(vals["DATA_DIR"]).expanduser() if vals["DATA_DIR"] else desk / DATA_FOLDER_NAME
    src = Path(vals["SOURCE_DB"]).expanduser() if vals["SOURCE_DB"] else desk.joinpath(*SOURCE_DB_PARTS)
    return Config(vals, data, src)


def collector_root(cfg: "Config") -> Path:
    """기존 수집기 데이터 폴더 = SOURCE_DB(…/db/market.sqlite)의 두 단계 위."""
    return cfg.source_db.parent.parent


def theme_file(cfg: "Config") -> Path:
    v = cfg.values["THEME_FILE"]
    return Path(v).expanduser() if v else collector_root(cfg) / "themes" / "theme_master.csv"


def collector_lock(cfg: "Config") -> Path:
    v = cfg.values["COLLECTOR_LOCK"]
    return Path(v).expanduser() if v else collector_root(cfg) / "system" / "collector.lock"


def load_keys(install: Path, ask: Callable[[str], str] = getpass.getpass,
              out: Callable[[str], None] = print) -> dict[str, str]:
    """빠진 키만 입력받아 저장한다. 입력 글자는 화면에 보이지 않는다."""
    path = keys_path(install)
    vals = dotenv_values(path) if path.exists() else {}
    keys = {k: (vals.get(k) or "").strip() for k in KEY_NAMES}
    missing = [k for k in KEY_NAMES if not keys[k]]
    if missing:
        out("KIS 키를 입력합니다. 입력한 글자는 화면에 보이지 않습니다.")
        for k in missing:
            keys[k] = ask(f"{k}: ").strip()
        # 같은 파일에 다른 키(기존 수집기용)가 있으면 그대로 유지한다
        merged = {k: (v or "") for k, v in vals.items()}
        merged.update(keys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in merged.items()), encoding="utf-8")
        out(f"키를 저장했습니다: {path}")
    return keys
