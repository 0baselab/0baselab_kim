"""API 키 보관: 설치 폴더 secret/keys.env에만 둔다 (명세서 2.4).

키 값은 화면·로그·보고서에 쓰지 않는다. 필요하면 앞 4자리만 표시한다.
"""
from __future__ import annotations

import getpass
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

KEY_NAMES = ["KRX_AUTH_KEY", "KIS_APP_KEY", "KIS_APP_SECRET", "DART_API_KEY"]

# 로그·보고서에서 지워야 할 문자열 (키, 토큰). 실행 중 메모리에만 둔다.
_SENSITIVE: set[str] = set()


def register_sensitive(value: str | None) -> None:
    if value and len(value) >= 8:
        _SENSITIVE.add(value)


def sensitive_values() -> set[str]:
    return set(_SENSITIVE)


def clear_sensitive() -> None:
    _SENSITIVE.clear()


def redact(text: str) -> str:
    for v in sorted(_SENSITIVE, key=len, reverse=True):
        text = text.replace(v, "****")
    return text


def mask(value: str | None) -> str:
    if not value:
        return "(없음)"
    return value[:4] + "****"


def keys_path(install: Path) -> Path:
    return install / "secret" / "keys.env"


def load_keys(install: Path) -> dict[str, str]:
    path = keys_path(install)
    values = dotenv_values(path) if path.exists() else {}
    keys = {k: (values.get(k) or "").strip() for k in KEY_NAMES}
    for v in keys.values():
        register_sensitive(v)
    return keys


def ensure_keys(install: Path, ask: Callable[[str], str] = getpass.getpass,
                out: Callable[[str], None] = print) -> dict[str, str]:
    """빠진 키를 하나씩 입력받아 저장한다. 입력 내용은 화면에 보이지 않는다."""
    keys = load_keys(install)
    missing = [k for k in KEY_NAMES if not keys[k]]
    if missing:
        out("API 키를 입력합니다. 입력한 글자는 화면에 보이지 않습니다.")
        for name in missing:
            keys[name] = ask(f"{name}: ").strip()
            register_sensitive(keys[name])
        save_keys(install, keys)
        out(f"키를 저장했습니다: {keys_path(install)}")
    return keys


def save_keys(install: Path, keys: dict[str, str]) -> None:
    path = keys_path(install)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={keys.get(k, '')}" for k in KEY_NAMES]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
