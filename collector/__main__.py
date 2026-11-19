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
    ("1", "당일 수집", "daily"),
    ("2", "기간 수집", "range"),
    ("3", "브리핑 패키지 생성", "briefing"),
    ("4", "DART 수집", None),
    ("5", "DART 구글 시트용 내보내기", None),
    ("6", "상태 점검", "status"),
    ("7", "실측 검증", "verify"),
]


def _yes(question: str) -> bool:
    try:
        return input(question + " (y/N): ").strip().lower() in ("y", "yes", "예")
    except EOFError:
        return False


def _valid_date(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{8}", s or ""))


def cmd_collect(args) -> int:
    from .app import start
    from .collect import run_collect
    from .lock import AlreadyRunning

    mode = args.command
    start_d, end_d = getattr(args, "date_from", None), getattr(args, "date_to", None)
    if mode == "range":
        if not start_d or not end_d:
            try:
                start_d = start_d or input("시작일 (YYYYMMDD): ").strip()
                end_d = end_d or input("종료일 (YYYYMMDD): ").strip()
            except EOFError:
                return 1
        if not (_valid_date(start_d) and _valid_date(end_d)) or start_d > end_d:
            print("날짜는 YYYYMMDD 형식이고, 시작일이 종료일보다 늦으면 안 됩니다.")
            return 1
    try:
        app = start()
    except AlreadyRunning as exc:
        print(exc)
        return 1
    try:
        confirm = (lambda q: True) if args.yes else _yes
        res = run_collect(app, mode, start_d, end_d, force=args.force, confirm=confirm)
        print("\n".join(["", "=== 실행 요약 ==="] + res.lines + [f"로그: {app.log_path}"]))
        return 0 if not res.stopped else 2
    finally:
        app.close()


def cmd_briefing(args) -> int:
    from .app import start
    from .briefing import BriefingError, build, default_date
    from .briefing_writer import record_unclassified, write
    from .lock import AlreadyRunning

    try:
        app = start()
    except AlreadyRunning as exc:
        print(exc)
        return 1
    try:
        day = getattr(args, "date", None) or default_date(app.con)
        if not getattr(args, "date", None) and args.command == "briefing" and not args.yes:
            try:
                typed = input(f"기준일 (YYYYMMDD, 엔터 = {day}): ").strip()
            except EOFError:
                typed = ""
            day = typed or day
        if not day or not _valid_date(day):
            print("기준일이 없거나 형식이 틀렸습니다. 먼저 수집을 실행하세요.")
            return 1
        now = app.now()
        try:
            top_n = int(app.settings.get("UNCLASSIFIED_TOP_N") or 50)
            pkg = build(app.con, day, app.dirs["themes"], now, top_n=top_n)
        except BriefingError as exc:
            print(f"브리핑 패키지를 만들지 않았습니다: {exc}")
            return 1
        paths = write(pkg, app.dirs["briefing"], now, with_json=getattr(args, "json", False))
        record_unclassified(app.con, pkg)
        print(f"기준일 {day}, 대상 종목 {len(pkg.stocks)}개, 테마 버전 {pkg.theme_version or '없음'}")
        print(f"잠정값: 시세 {pkg.provisional['price']}종목, 수급 {pkg.provisional['investor']}종목")
        for p in paths:
            print(f"저장: {p}")
        return 0
    finally:
        app.close()


def cmd_unclassified(args) -> int:
    from .app import start
    from .briefing_writer import export_unclassified_repeats

    app = start()
    try:
        p = export_unclassified_repeats(app.con, app.dirs["briefing"], app.now(), getattr(args, "days", None) or 20)
        print(f"저장: {p}")
        return 0
    finally:
        app.close()


def cmd_status(args) -> int:
    from .app import start
    from .lock import AlreadyRunning
    from .secrets import KEY_NAMES, register_sensitive, save_keys
    from .status import report
    import getpass

    try:
        app = start()
    except AlreadyRunning as exc:
        print(exc)
        return 1
    try:
        print("\n".join(report(app)))
        if not args.yes and _yes("\n키를 다시 입력할까요?"):
            for i, k in enumerate(KEY_NAMES, 1):
                print(f"  {i}. {k}")
            try:
                pick = input("번호: ").strip()
            except EOFError:
                return 0
            if pick.isdigit() and 1 <= int(pick) <= len(KEY_NAMES):
                name = KEY_NAMES[int(pick) - 1]
                app.keys[name] = getpass.getpass(f"{name}: ").strip()
                register_sensitive(app.keys[name])
                save_keys(app.install, app.keys)
                print(f"{name}을(를) 저장했습니다.")
        return 0
    finally:
        app.close()


def cmd_verify(args=None) -> int:
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


COMMANDS = {"daily": cmd_collect, "range": cmd_collect, "briefing": cmd_briefing, "status": cmd_status,
            "unclassified": cmd_unclassified, "verify": cmd_verify}


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
    return COMMANDS[cmd](argparse.Namespace(command=cmd, date_from=None, date_to=None, force=False, yes=False,
                                               date=None, json=False, days=None))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="주식데이터수집기")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="daily(당일 수집) / range(기간 수집) / briefing(브리핑 패키지) / status(상태 점검) / "
                             "unclassified(미분류 반복 등장 CSV) / verify(실측 검증)")
    parser.add_argument("--from", dest="date_from", help="기간 수집 시작일 YYYYMMDD")
    parser.add_argument("--to", dest="date_to", help="기간 수집 종료일 YYYYMMDD")
    parser.add_argument("--date", help="브리핑 기준일 YYYYMMDD (기본: 마지막 수집일)")
    parser.add_argument("--json", action="store_true", help="브리핑을 JSON으로도 저장")
    parser.add_argument("--days", type=int, help="미분류 반복 등장 집계 거래일 수 (기본 20)")
    parser.add_argument("--force", action="store_true", help="이미 성공한 단위도 다시 받기")
    parser.add_argument("--yes", action="store_true", help="확인 질문을 모두 '예'로 처리")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        code = COMMANDS[args.command](args) if args.command else menu()
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
