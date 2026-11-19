"""API 원본 응답 저장 (명세서 3.3). raw/{출처}/YYYY-MM/ 에 gzip. DB에는 데이터 폴더 기준 상대 경로만 남긴다."""
from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

from .secrets import redact
from .timeutil import stamp


def save_raw(root: Path, source: str, name: str, base_date: str, body, now: datetime) -> str:
    """name 예: stk_daily -> krx_stk_daily_20261118_20261118-183512.json.gz"""
    month = f"{base_date[:4]}-{base_date[4:6]}"
    folder = root / "raw" / source / month
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{source}_{name}_{base_date}_{stamp(now)}.json.gz"
    i = 1
    while path.exists():  # 같은 초에 두 번 저장해도 덮어쓰지 않게
        path = folder / f"{source}_{name}_{base_date}_{stamp(now)}-{i}.json.gz"
        i += 1
    data = redact(json.dumps(body, ensure_ascii=False)).encode("utf-8")
    with gzip.open(path, "wb") as f:
        f.write(data)
    return path.relative_to(root).as_posix()
