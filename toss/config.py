"""설정·경로·키.

- 키: 설치 폴더 secret/keys.env (TOSS_CLIENT_ID, TOSS_CLIENT_SECRET). 화면·로그에 쓰지 않는다.
- 설정: 설치 폴더 config/toss_settings.env (없으면 기본값). 배포 zip에 넣지 않는다.
"""
from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from collector.paths import desktop_dir

KEY_NAMES = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")
DATA_FOLDER_NAME = "단타분석"

DEFAULTS: dict[str, str] = {
    "DATA_DIR": "",          # 비우면 바탕화면/단타분석 (단타분봉수집기와 같은 폴더)
    "DANTA_DB": "",          # 비우면 DATA_DIR/danta.sqlite (읽기 전용)
    "BASE_URL": "https://openapi.tossinvest.com",
    "RATE_FACTOR": "0.5",    # 문서 한도의 몇 배로 호출할지 (기본 절반)
    "RETRY_429": "5",        # 429 재시도 횟수
    "MAX_CALLS": "3000",     # 과거 탐색 1회당 호출 상한
    "MAX_YEARS": "3",        # 과거 탐색 중단 기준 (년)
    "CHART_RATE": "5",       # 캔들(MARKET_DATA_CHART) 초당 호출 수. 실측 지속 속도 약 4.6/초 (헤더 한도 20과 다름)
    "HISTORY_DAYS": "365",   # 분봉·일봉을 처음 받을 때 과거 범위 (달력일)
    "INVESTOR_DAILY": "N",   # Y면 daily-update에서 종목별 투자자 순매수 거래량도 수집
    "MAX_RETRY": "3",        # 같은 (종목, 날짜) 분봉이 연속 이 횟수만큼 실패하면 그날은 건너뜀
}


def install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    values: dict[str, str]
    data_dir: Path
    danta_db: Path

    def get(self, k: str) -> str:
        return self.values[k]

    def num(self, k: str) -> float:
        return float(self.values[k])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "toss_probe.sqlite"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "system" / "toss_token.json"

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "toss_data"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "system" / "toss.lock"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def load_config(install: Path | None = None, platform: str | None = None) -> Config:
    install = install or install_dir()
    vals = dict(DEFAULTS)
    p = install / "config" / "toss_settings.env"
    if p.exists():
        for k, v in dotenv_values(p).items():
            if k in vals and v is not None:
                vals[k] = v.strip()
    data = Path(vals["DATA_DIR"]).expanduser() if vals["DATA_DIR"] else desktop_dir(platform) / DATA_FOLDER_NAME
    danta = Path(vals["DANTA_DB"]).expanduser() if vals["DANTA_DB"] else data / "danta.sqlite"
    return Config(vals, data, danta)


def load_keys(install: Path, ask: Callable[[str], str] = getpass.getpass,
              out: Callable[[str], None] = print) -> dict[str, str]:
    """빠진 키만 입력받아 secret/keys.env에 저장한다. 같은 파일의 다른 키는 그대로 둔다."""
    path = install / "secret" / "keys.env"
    vals = dotenv_values(path) if path.exists() else {}
    keys = {k: (vals.get(k) or "").strip() for k in KEY_NAMES}
    missing = [k for k in KEY_NAMES if not keys[k]]
    if missing:
        out("토스 Open API 키를 입력합니다. 입력한 글자는 화면에 보이지 않습니다.")
        for k in missing:
            keys[k] = ask(f"{k}: ").strip()
        merged = {k: (v or "") for k, v in vals.items()}
        merged.update(keys)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{k}={v}\n" for k, v in merged.items()), encoding="utf-8")
        out(f"키를 저장했습니다: {path}")
    return keys
