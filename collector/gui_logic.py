"""GUI 뒤에서 도는 로직 (Tk 없이 테스트 가능)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .secrets import KEY_NAMES, load_keys
from .timeutil import stamp
from .version import __version__


def collect_mode(start: str, end: str, today: str) -> tuple[str, str | None, str | None]:
    """기간이 오늘 하루면 당일 수집(빠진 날 자동 보충), 아니면 기간 수집."""
    if start == end == today:
        return "daily", None, None
    return "range", start, end


def missing_keys(install: Path) -> list[str]:
    keys = load_keys(install)
    return [k for k in KEY_NAMES if not keys.get(k)]


def version_changed(install: Path) -> bool:
    """업데이트 직후 첫 실행인지. 설치 폴더 config/last_version에 기록한다."""
    p = install / "config" / "last_version"
    prev = p.read_text(encoding="utf-8").strip() if p.exists() else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(__version__, encoding="utf-8")
    return prev is not None and prev != __version__


def import_theme(src: Path, themes_dir: Path, now: datetime) -> Path:
    """고른 CSV를 마스터로 불러온다. 기존 마스터는 history에 보관 (프로그램 안에서 편집하지 않음)."""
    dst = themes_dir / "theme_master.csv"
    themes_dir.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        hist = themes_dir / "history"
        hist.mkdir(exist_ok=True)
        shutil.copyfile(dst, hist / f"theme_master_before_import_{stamp(now)}.csv")
    shutil.copyfile(src, dst)
    return dst


def export_theme(themes_dir: Path, dst: Path) -> Path:
    src = themes_dir / "theme_master.csv"
    if not src.exists():
        raise FileNotFoundError("테마 마스터 파일이 아직 없습니다.")
    shutil.copyfile(src, dst)
    return dst


def theme_summary(themes_dir: Path) -> str:
    p = themes_dir / "theme_master.csv"
    if not p.exists():
        return "테마 마스터 파일 없음"
    import csv
    import io
    from .themes import _read_text
    rows = list(csv.DictReader(io.StringIO(_read_text(p))))
    themes = {(r.get("theme") or "").strip() for r in rows if (r.get("theme") or "").strip()}
    mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{p}\n테마 {len(themes)}개, {len(rows)}줄 (마지막 수정 {mtime})"


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def run_history(con, limit: int = 200) -> list[dict]:
    import json
    out = []
    for r in con.execute("SELECT * FROM run_log ORDER BY run_id DESC LIMIT ?", (limit,)):
        try:
            lines = json.loads(r["summary"] or "[]")
        except ValueError:
            lines = []
        result = "중단" if any("중단" in l for l in lines) else ("오류 있음" if (r["errors"] or 0) else "완료")
        out.append({"started": (r["started_at"] or "")[:16].replace("T", " "), "mode": r["mode"],
                    "target": r["target"] or "", "kinds": r["kinds"] or "", "result": result,
                    "off_hours": "예" if r["off_hours"] else "", "calls": r["calls"], "summary": lines})
    return out


def hide_console_on_windows() -> None:
    """GUI로 실행할 때 검은 콘솔 창을 숨긴다 (명령줄 실행 때는 그대로)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()  # type: ignore[attr-defined]
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # type: ignore[attr-defined]
    except Exception:
        pass
