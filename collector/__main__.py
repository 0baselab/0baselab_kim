"""진입점: `python -m collector [명령]`, exe도 같은 인자를 받는다 (명세서 2.2)."""
from __future__ import annotations

import argparse
import sys

from . import paths
from .logs import setup_logging
from .secrets import ensure_keys, mask
from .settings import load_settings
from .timeutil import now_kst, stamp
from .version import __version__

MENU = [
    ("1", "당일 수집", None),
    ("2", "기간 수집", None),
    ("3", "브리핑 패키지 생성", None),
    ("4", "DART 수집", None),
    ("5", "DART 구글 시트용 내보내기", None),
    ("6", "상태 점검", None),
    ("7", "실측 검증", "verify"),
]


def cmd_verify() -> int:
    from .verify.runner import run_verify

    install = paths.install_dir()
    settings = load_settings(install)
    root = paths.data_dir(settings)
    if paths.is_onedrive(root):
        print("경고: 데이터 폴더가 OneDrive 동기화 폴더 안에 있습니다. "
              "config/settings.env의 DATA_DIR에 동기화되지 않는 경로를 지정하는 것을 권장합니다.")
    dirs = paths.ensure_verify_dirs(root)
    now = now_kst()
    log_path = setup_logging(dirs["logs"], stamp(now))
    keys = ensure_keys(install)
    print(f"주식데이터수집기 {__version__} / 실측 검증")
    print(f"데이터 폴더: {root}")
    print("키: " + ", ".join(f"{k}={mask(v)}" for k, v in keys.items()))
    report = run_verify(now=now, settings=settings, keys=keys, dirs=dirs)
    print(f"\n보고서: {report}\n로그: {log_path}")
    return 0


COMMANDS = {"verify": cmd_verify}


def menu() -> int:
    print(f"주식데이터수집기 {__version__}")
    for no, title, cmd in MENU:
        print(f"  {no}. {title}" + ("" if cmd else " (준비 중)"))
    try:
        choice = input("번호를 입력하세요: ").strip()
    except EOFError:
        return 1
    cmd = next((c for n, _, c in MENU if n == choice), None)
    if not cmd:
        print("아직 준비되지 않았거나 없는 번호입니다.")
        return 1
    return COMMANDS[cmd]()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="주식데이터수집기")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS))
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        code = COMMANDS[args.command]() if args.command else menu()
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다.")
        code = 130
    if getattr(sys, "frozen", False) and not argv:
        try:
            input("\n엔터를 누르면 창이 닫힙니다.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
