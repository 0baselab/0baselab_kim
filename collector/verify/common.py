from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..dart import DartClient
from ..kis import KisClient
from ..krx import KrxClient
from ..secrets import redact

# 상태값: 완료(자료 확보, 판단은 사람이 함) / 실패 / 설정 필요 / 건너뜀
DONE, FAILED, NEEDS_SETTING, SKIPPED = "완료", "실패", "설정 필요", "건너뜀"


@dataclass
class Item:
    no: str
    title: str
    status: str = DONE
    lines: list[str] = field(default_factory=list)

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)


@dataclass
class Ctx:
    now: datetime
    settings: dict
    dirs: dict[str, Path]
    raw_dir: Path
    kis: KisClient | None
    krx: KrxClient | None
    dart: DartClient | None
    stocks: list[str]
    dart_stocks: list[str]
    ref_date: str | None = None
    holiday_rows: list[dict] = field(default_factory=list)
    kis_first: dict[str, list[dict]] = field(default_factory=dict)  # 종목 -> output2 (오늘 기준 첫 조회)
    krx_trade: dict[tuple[str, str], list[dict]] = field(default_factory=dict)  # (날짜, 시장) -> 행
    krx_base: dict[str, list[dict]] = field(default_factory=dict)  # 시장 -> 행

    @property
    def today(self) -> str:
        return self.now.strftime("%Y%m%d")

    def save_raw(self, name: str, body) -> Path:
        """원본 응답 본문만 저장한다 (요청 헤더 없음). 키·토큰 문자열은 지운다."""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        path = self.raw_dir / f"{name}.json"
        path.write_text(redact(json.dumps(body, ensure_ascii=False, indent=1)), encoding="utf-8")
        return path


def num(v) -> float | None:
    """쉼표는 지우고 변환, '-'·빈 값은 None. 0으로 바꾸지 않는다 (명세서 11.2)."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt(v, digits: int = 0) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        if digits == 0 and v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.{digits or 4}f}"
    return str(v)


def ratio(a, b) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b


def md_table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")
    return out
