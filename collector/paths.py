"""설치 폴더·데이터 폴더 찾기 (명세서 2.3, 3.1).

Windows 전용 동작(바탕화면 찾기)은 _windows_desktop()에 분리했다.
Linux에서는 대체 동작(~/Desktop)으로 테스트하고, 최종 확인은 PC의 exe로 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

DATA_FOLDER_NAME = "주식데이터수집"

# FOLDERID_Desktop (Windows Known Folder ID)
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def install_dir() -> Path:
    """exe로 실행하면 exe가 있는 폴더, 코드로 실행하면 저장소 루트."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _windows_desktop() -> Path:
    """SHGetKnownFolderPath로 실제 바탕화면 경로를 얻는다 (OneDrive 이동 포함)."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID.from_buffer_copy(uuid.UUID(_FOLDERID_DESKTOP).bytes_le)
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    if hr != 0:
        raise OSError(f"SHGetKnownFolderPath 실패 (HRESULT={hr})")
    try:
        return Path(out.value)
    finally:
        ole32.CoTaskMemFree(out)


def desktop_dir(platform: str | None = None) -> Path:
    platform = platform or sys.platform
    if platform == "win32":
        return _windows_desktop()
    # macOS는 ~/Desktop (명세서 3.1). Linux는 개발용 대체 동작으로 같은 규칙을 쓴다.
    return Path.home() / "Desktop"


def data_dir(settings: dict, platform: str | None = None) -> Path:
    """DATA_DIR 설정이 있으면 우선, 없으면 바탕화면/주식데이터수집."""
    custom = (settings.get("DATA_DIR") or "").strip()
    if custom:
        return Path(custom).expanduser()
    return desktop_dir(platform) / DATA_FOLDER_NAME


def is_onedrive(path: Path) -> bool:
    return "onedrive" in str(path).lower()


def ensure_verify_dirs(root: Path) -> dict[str, Path]:
    """0단계에 필요한 폴더만 만든다. 나머지 폴더 구조는 1단계에서 만든다."""
    dirs = {
        "root": root,
        "system": root / "system",
        "verify": root / "system" / "verify",
        "snapshots": root / "system" / "verify" / "snapshots",
        "logs": root / "logs",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


DATA_SUBDIRS = ["db", "raw", "briefing", "dart_export", "themes", "themes/history", "logs", "backup", "system"]


def ensure_data_dirs(root: Path) -> dict[str, Path]:
    """명세서 3.2의 폴더 구조. 없으면 만든다."""
    dirs = {"root": root}
    for sub in DATA_SUBDIRS:
        p = root.joinpath(*sub.split("/"))
        p.mkdir(parents=True, exist_ok=True)
        dirs[sub] = p
    return dirs
