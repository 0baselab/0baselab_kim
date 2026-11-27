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
    ("4", "DART 수집", "dart"),
    ("5", "DART 구글 시트용 내보내기", "dart-export"),
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


def cmd_dart(args) -> int:
    from .app import start
    from .dart import DartClient
    from .dart_collect import run
    from .lock import AlreadyRunning

    try:
        app = start()
    except AlreadyRunning as exc:
        print(exc)
        return 1
    try:
        key = app.keys.get("DART_API_KEY")
        if not key:
            print("DART_API_KEY가 없습니다. 상태 점검(6번)에서 입력하세요.")
            return 1
        api = app.http_factory("DART", float(app.settings.get("DART_CALLS_PER_SEC") or 5))
        dart = DartClient(api, app.settings["DART_BASE_URL"], key)
        base_day = app.con.execute("SELECT MAX(date) FROM universe_daily").fetchone()[0]
        targets = {r[0] for r in app.con.execute(
            "SELECT code FROM universe_daily WHERE date=? AND in_universe=1", (base_day,))} if base_day else set()
        if not targets:
            print("대상 종목이 없습니다. 먼저 당일 수집(1번)을 실행하세요.")
            return 1
        limit = int(app.settings.get("DART_DAILY_LIMIT") or 18000)
        st = run(app.con, dart, app.root, app.now, limit, targets, progress=print)
        lines = [f"DART: 호출 {st.calls}회, 새 공시 {st.filings}건(정기보고서 {st.periodic_new}),",
                 f"  재무 받음 {st.fs_ok}(정정본 중복 생략 {st.fs_dedup}), 자료 없음 {st.fs_nodata}, 실패 {st.fs_failed}, 분기 계산 {st.quarters}행,",
                 f"  12월 결산이 아니어서 건너뜀 {st.skipped_fy}건, 남은 정기보고서 {st.remaining}건"]
        if st.stopped:
            lines.append("  " + st.stopped)
        lines += [f"  - {e}" for e in st.errors[:10]]
        print("\n".join(lines))
        return 0
    finally:
        app.close()


def cmd_dart_export(args) -> int:
    from .app import start
    from .dart_export import export

    app = start()
    try:
        for p in export(app.con, app.dirs["dart_export"], app.now()):
            print(f"저장: {p}")
        print("구글 시트: 파일 > 가져오기 > 업로드 > '현재 시트 바꾸기'로 올리세요.")
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


def cmd_verify_history() -> int:
    """과거 데이터 조회 가능 기간 실측. 수집 DB가 없어도 돌고, 데이터 폴더·DB를 새로 만들지 않는다."""
    from .verify.history import run_history

    install = paths.install_dir()
    settings = load_settings(install)
    root = paths.data_dir(settings)
    # 데이터 폴더가 이미 있으면 system/verify에, 없으면 설치 폴더 verify_history에 보고서를 둔다
    out_dir = root / "system" / "verify" if root.exists() else install / "verify_history"
    out_dir.mkdir(parents=True, exist_ok=True)
    now = now_kst()
    log_path = setup_logging(out_dir, stamp(now))
    keys = ensure_keys(install)
    print(f"주식데이터수집기 {__version__} / 과거 데이터 조회 가능 기간 실측")
    print("키: " + ", ".join(f"{k}={mask(v)}" for k, v in keys.items()))
    report = run_history(now=now, settings=settings, keys=keys, out_dir=out_dir,
                         db_path=root / "db" / "market.sqlite")
    print(f"\n보고서: {report}\n로그: {log_path}")
    return 0


def cmd_verify(args=None) -> int:
    from .verify.runner import run_verify

    if args is not None and getattr(args, "sub", None) == "history":
        return cmd_verify_history()

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


def cmd_backfill(args) -> int:
    """백테스트용 10년 과거 데이터 백필 (명세서 16장). backfill krx | kis | dart | status"""
    from . import backfill as B
    from .dart import DartClient
    from .http import ApiClient
    from .kis import KisClient
    from .krx import KrxClient
    from .lock import AlreadyRunning, Lock

    sub = getattr(args, "sub", None)
    if sub not in ("krx", "kis", "dart", "status", "auto"):
        print("사용법: backfill krx | kis | dart | status | auto [--shutdown]")
        return 1
    install = paths.install_dir()
    settings = load_settings(install)
    root = paths.data_dir(settings)
    for sub_dir in ("backtest", "system", "logs"):
        (root / sub_dir).mkdir(parents=True, exist_ok=True)
    now = now_kst()
    log_path = setup_logging(root / "logs", "backfill_" + stamp(now))
    lock = Lock(root / "system" / "collector.lock")  # 정기 수집과 같은 잠금
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"{exc} 정기 수집이나 다른 백필이 끝난 뒤 다시 실행하세요.")
        return 1
    con = B.open_bt(root)
    collect_con = B.open_collect_ro(root)
    try:
        today = now.strftime("%Y%m%d")
        if sub == "status":
            print("\n".join(B.status_lines(con, collect_con, settings, today)))
            return 0
        keys = ensure_keys(install)
        print(f"주식데이터수집기 {__version__} / 백필 {sub} (저장: {B.bt_path(root)})")
        make_krx = lambda: KrxClient(ApiClient("KRX", float(settings.get("KRX_CALLS_PER_SEC") or 2)),
                                     settings["KRX_BASE_URL"], keys["KRX_AUTH_KEY"])
        make_kis = lambda: KisClient(ApiClient("KIS", float(settings.get("KIS_CALLS_PER_SEC") or 10)),
                                     settings["KIS_BASE_URL"], keys["KIS_APP_KEY"], keys["KIS_APP_SECRET"],
                                     root / "system" / "kis_token.json")
        make_dart = lambda: DartClient(ApiClient("DART", float(settings.get("DART_CALLS_PER_SEC") or 5)),
                                       settings["DART_BASE_URL"], keys["DART_API_KEY"])
        if sub == "auto":
            import subprocess
            import time
            try:
                res = B.run_auto(con, root, collect_con, settings, now_kst, print, make_krx, make_kis, make_dart,
                                 root / "system" / "verify")
            except KeyboardInterrupt:
                print("\n사용자가 중단했습니다. 받은 것은 저장되었고 다음 실행 때 이어받습니다.")
                return 130
            print("\n=== 요약 ===")
            for name, result in res.stages:
                print(f"{name}: {result}")
            for f in res.failed:
                print(f"실패 단계: {f}")
            print("\n".join(B.status_lines(con, collect_con, settings, now_kst().strftime("%Y%m%d"))))
            for p in res.reports:
                print(f"보고서: {p}")
            if getattr(args, "shutdown", False):
                con.close()
                lock.release()
                B.shutdown_countdown(lambda cmd: subprocess.run(cmd, check=False), print, time.sleep)
            return 0
        if sub == "krx":
            krx = KrxClient(ApiClient("KRX", float(settings.get("KRX_CALLS_PER_SEC") or 2)), settings["KRX_BASE_URL"],
                            keys["KRX_AUTH_KEY"])
            rep = B.krx_backfill(con, krx, settings, now_kst, print)
        elif sub == "kis":
            kis = KisClient(ApiClient("KIS", float(settings.get("KIS_CALLS_PER_SEC") or 10)), settings["KIS_BASE_URL"],
                            keys["KIS_APP_KEY"], keys["KIS_APP_SECRET"], root / "system" / "kis_token.json")
            rep = B.kis_backfill(con, kis, now_kst, print)
        else:
            dart = DartClient(ApiClient("DART", float(settings.get("DART_CALLS_PER_SEC") or 5)),
                              settings["DART_BASE_URL"], keys["DART_API_KEY"])
            rep = B.dart_backfill(con, dart, root, collect_con, settings, now_kst, print)
        path = B.write_report(rep, con, root / "system" / "verify", now_kst())
        print("\n".join(rep.lines + ([f"멈춤: {rep.stopped}"] if rep.stopped else [])))
        print(f"\n보고서: {path}\n로그: {log_path}")
        return 0
    except B.BackfillStop as exc:
        print(f"중단: {exc}")
        return 2
    finally:
        con.close()
        if collect_con is not None:
            collect_con.close()
        lock.release()


def cmd_backtest(args) -> int:
    from .bt.cli import main as bt_main
    sub = getattr(args, "sub", None)
    r = getattr(args, "random", None)
    return bt_main(sub, getattr(args, "variant", None), getattr(args, "db", None), random_n=100 if r is None else r)


def cmd_menu(args) -> int:
    return menu()


COMMANDS = {"menu": cmd_menu, "daily": cmd_collect, "range": cmd_collect, "briefing": cmd_briefing, "status": cmd_status,
            "unclassified": cmd_unclassified, "dart": cmd_dart, "dart-export": cmd_dart_export, "verify": cmd_verify,
            "backfill": cmd_backfill, "backtest": cmd_backtest}


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
                             "unclassified(미분류 반복 등장 CSV) / dart(DART 수집) / dart-export(구글 시트용 CSV) / verify(실측 검증)")
    parser.add_argument("sub", nargs="?", help="세부 항목 (verify history / backfill krx|kis|dart|status)")
    parser.add_argument("variant", nargs="?", help="backtest run의 변형 A|B|C|D|all")
    parser.add_argument("--db", help="백테스트 DB 경로 (backtest 명령)")
    parser.add_argument("--random", type=int, help="backtest run 무작위 비교 횟수 (기본 100, 0이면 생략)")
    parser.add_argument("--from", dest="date_from", help="기간 수집 시작일 YYYYMMDD")
    parser.add_argument("--to", dest="date_to", help="기간 수집 종료일 YYYYMMDD")
    parser.add_argument("--date", help="브리핑 기준일 YYYYMMDD (기본: 마지막 수집일)")
    parser.add_argument("--json", action="store_true", help="브리핑을 JSON으로도 저장")
    parser.add_argument("--days", type=int, help="미분류 반복 등장 집계 거래일 수 (기본 20)")
    parser.add_argument("--force", action="store_true", help="이미 성공한 단위도 다시 받기")
    parser.add_argument("--shutdown", action="store_true", help="backfill auto가 끝나면 60초 뒤 Windows 종료")
    parser.add_argument("--yes", action="store_true", help="확인 질문을 모두 '예'로 처리")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        if args.command:
            code = COMMANDS[args.command](args)
        elif getattr(sys, "frozen", False):  # 명령줄용 exe를 인자 없이 실행 -> 터미널 메뉴
            args.command = "menu"
            code = menu()
        else:  # 코드로 실행(python -m collector) -> GUI
            from .gui import run_gui
            return run_gui()
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다.")
        code = 130
    if getattr(sys, "frozen", False) and args.command == "menu":
        try:
            input("\n엔터를 누르면 창이 닫힙니다.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
