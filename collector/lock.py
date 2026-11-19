"""중복 실행 방지 잠금 파일 (명세서 2.3). 프로세스가 죽었으면 남은 잠금을 자동 해제한다."""
from __future__ import annotations

import os
import sys
from pathlib import Path


class AlreadyRunning(Exception):
    pass


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                pid = 0
            if pid != os.getpid() and pid_alive(pid):
                raise AlreadyRunning(f"이미 실행 중입니다 (PID {pid}). 다른 창을 확인하세요.")
            self.path.unlink(missing_ok=True)  # 죽은 프로세스의 잠금 해제
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        self.held = True

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
