"""명령줄: probe / ratetest / jobs / backfill / recalc."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from collector.paths import desktop_dir

from collector.timeutil import now_kst

from . import commands, source, store
from .config import collector_lock, install_dir, load_config, load_keys, theme_file
from .kis import KisMinuteClient, redact
from .version import __version__


def _path_masks() -> list[tuple[str, str]]:
    """로그에서 가릴 경로 (Windows 사용자 폴더명, OneDrive 경로). 긴 것부터 바꾼다."""
    out = []
    try:
        out.append((str(desktop_dir()), "<바탕화면>"))
    except OSError:
        pass
    out.append((str(Path.home()), "<사용자폴더>"))
    return sorted({(a, b) for a, b in out if a and len(a) > 3}, key=lambda x: len(x[0]), reverse=True)


def mask_paths(text: str, masks: list[tuple[str, str]] | None = None) -> str:
    for src, dst in (masks if masks is not None else _path_masks()):
        text = text.replace(src, dst)
    return text


class Logger:
    """화면과 logs/danta_<명령>_<시각>.txt에 같은 내용을 남긴다. 키·토큰은 가린다."""

    def __init__(self, log_dir, name: str, now: datetime):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"danta_{name}_{now.strftime('%Y%m%d-%H%M%S')}.txt"
        self.f = self.path.open("a", encoding="utf-8")
        self.masks = _path_masks()

    def __call__(self, msg: str) -> None:
        line = f"{now_kst().strftime('%H:%M:%S')} {mask_paths(redact(msg), self.masks)}"
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="단타분봉수집기", description="KIS 1분봉 실측·수집 (시세 조회 전용)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("probe", help="하루치 분봉 실측 + 가장 오래된 날짜 탐색")
    pr.add_argument("--code", default="005930")
    pr.add_argument("--date", help="YYYYMMDD (기본: 기존 DB 달력의 직전 거래일)")
    pr.add_argument("--no-oldest", action="store_true", help="가장 오래된 날짜 탐색 생략")
    pr.add_argument("--compare", action="store_true",
                    help="거래대금 상위 5 + 하위 5 종목을 같은 날짜로 시장별 수집해 일봉 거래대금과 비교")
    pr.add_argument("--markets", default="J,UN", help="--compare에서 비교할 시장 구분 (기본 J,UN)")
    rt = sub.add_parser("ratetest", help="초당 호출 한도 단계별 측정")
    rt.add_argument("--code", default="005930")
    rt.add_argument("--date", help="YYYYMMDD (기본: 직전 거래일)")
    sub.add_parser("jobs", help="기존 일봉 DB에서 (날짜, 종목) 작업 목록 생성")
    bf = sub.add_parser("backfill", help="작업 목록을 오래된 날짜부터 수집 (이어받기)")
    bf.add_argument("--rate", type=float, help="초당 호출 수 (기본: 설정 RATE)")
    bf.add_argument("--max-calls", type=int, default=0, help="이번 실행 호출 상한 (0=무제한)")
    dl = sub.add_parser("daily", help="미수집 개장일만 골라 작업 생성 + 수집, 테마 스냅샷 (작업 스케줄러용)")
    dl.add_argument("--rate", type=float, help="초당 호출 수 (기본: 설정 DAILY_RATE=5)")
    dl.add_argument("--wait-minutes", type=float, help="기존 수집기 실행 중일 때 최대 대기 분 (기본 60)")
    ex = sub.add_parser("export", help="기존 DB에서 분석용 부분 DB(analysis/market_subset.sqlite) 만들기")
    ex.add_argument("--since", default="20250901", help="이 날짜(YYYYMMDD) 이후 행만 (기본 20250901)")
    ex.add_argument("--max-mb", type=float, default=380, help="결과 파일 크기 한도 MB (기본 380)")
    sub.add_parser("recalc", help="저장된 분봉의 1분 거래대금(value_krw) 다시 계산")
    return p


def _client(cfg, install) -> KisMinuteClient:
    keys = load_keys(install)
    return KisMinuteClient(
        base_url=cfg.get("KIS_BASE_URL"), app_key=keys["KIS_APP_KEY"], app_secret=keys["KIS_APP_SECRET"],
        token_path=cfg.token_path, rate=cfg.num("RATE"), backoff_retries=cfg.int("BACKOFF_RETRIES"),
        backoff_base=cfg.num("BACKOFF_BASE_SEC"), min_issue_gap=cfg.num("TOKEN_MIN_ISSUE_GAP_SEC"))


def _src(cfg, log, required: bool):
    try:
        return source.open_ro(cfg.source_db)
    except source.SourceDbError as exc:
        if required:
            raise SystemExit(str(exc))
        log(f"(참고) {exc}")
        return None


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if not args.cmd:
        build_parser().print_help()
        return 0
    install = install_dir()
    cfg = load_config(install)
    now = now_kst()
    log = Logger(cfg.log_dir, args.cmd, now)
    market, pw = cfg.get("MARKET"), cfg.get("PW_DATA_INCU_YN")
    try:
        log(f"단타분봉수집기 {__version__} / {args.cmd} / 데이터 폴더 {cfg.data_dir}")
        log(f"기존 DB(읽기 전용): {cfg.source_db}")
        if args.cmd == "jobs":
            con = store.connect(cfg.db_path)
            commands.cmd_jobs(_src(cfg, log, True), con, log, now.date(),
                              cfg.int("TOP_VALUE"), cfg.int("TOP_CHANGE"))
        elif args.cmd == "export":
            from .export import ExportError, export_subset
            try:
                r = export_subset(cfg.source_db, cfg.data_dir / "analysis" / "market_subset.sqlite", log,
                                  args.since, args.max_mb)
            except ExportError as exc:
                raise SystemExit(str(exc))
            log(f"로그 파일: {log.path}")
            return 0 if r["ok"] else 1
        elif args.cmd == "recalc":
            n = store.recalc_values(store.connect(cfg.db_path))
            log(f"[recalc] {n}개 (종목, 날짜) 다시 계산")
        elif args.cmd == "probe" and args.compare:
            markets = tuple(m.strip().upper() for m in args.markets.split(",") if m.strip())
            commands.cmd_probe_compare(_client(cfg, install), _src(cfg, log, True), log, now.date(),
                                       args.date, markets, pw)
        elif args.cmd == "probe":
            commands.cmd_probe(_client(cfg, install), _src(cfg, log, False), log, now.date(),
                               args.code, args.date, market, pw, oldest=not args.no_oldest)
        elif args.cmd == "ratetest":
            day = args.date
            if not day:
                src = _src(cfg, log, False)
                day = (source.last_open_before(src, now.strftime("%Y%m%d")) if src else None) \
                    or now.strftime("%Y%m%d")
            stages = [float(x) for x in cfg.get("RATETEST_STAGES").split(",") if x.strip()]
            commands.cmd_ratetest(_client(cfg, install), log, day, args.code, stages,
                                  cfg.num("RATETEST_SECONDS"), market, pw)
        elif args.cmd == "backfill":
            client = _client(cfg, install)
            if args.rate:
                client.set_rate(args.rate)
            con = store.connect(cfg.db_path)
            if not con.execute("SELECT 1 FROM jobs LIMIT 1").fetchone():
                raise SystemExit("작업 목록이 비어 있습니다. 먼저 jobs를 실행하세요.")
            if not cfg.acml_is_cumulative:
                log("(참고) ACML_IS_CUMULATIVE가 Y가 아니어서 value_krw는 비워 둡니다. 나중에 recalc로 채울 수 있습니다.")
            commands.cmd_backfill(client, con, log, now_kst, market, pw, args.max_calls,
                                  cfg.int("MAX_FAILURES"), cfg.acml_is_cumulative, max_retry=cfg.int("MAX_RETRY"))
        elif args.cmd == "daily":
            client = _client(cfg, install)
            client.set_rate(args.rate or cfg.num("DAILY_RATE"))
            if not cfg.acml_is_cumulative:
                log("(참고) ACML_IS_CUMULATIVE가 Y가 아니어서 value_krw는 비워 둡니다.")
            r = commands.cmd_daily(
                client, _src(cfg, log, True), store.connect(cfg.db_path), log, now_kst,
                theme_file(cfg), collector_lock(cfg), market, pw,
                args.wait_minutes if args.wait_minutes is not None else cfg.num("COLLECTOR_WAIT_MINUTES"),
                cfg.int("TOP_VALUE"), cfg.int("TOP_CHANGE"), cfg.acml_is_cumulative,
                cfg.int("MAX_RETRY"), cfg.int("MAX_FAILURES"))
            log(f"로그 파일: {log.path}")
            # 기존 수집기가 끝내 실행 중이면 2로 끝내 작업 스케줄러가 '실패'로 보고 다시 실행하게 한다
            return 2 if r["status"] == "collector_running" else 0
        log(f"로그 파일: {log.path}")  # 화면·로그 모두 경로는 가려서 출력
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, str):
            log(f"중단: {exc.code}")
            return 1
        raise
    except KeyboardInterrupt:
        log("사용자가 중단했습니다. 완료한 종목은 저장되어 있어 다음 실행 때 이어받습니다.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log(f"오류로 중단: {type(exc).__name__}: {exc}")
        return 1
    finally:
        log.close()


def main() -> int:
    argv = sys.argv[1:]
    code = run(argv)
    if not argv and getattr(sys, "frozen", False):
        input("\n엔터를 누르면 닫힙니다.")
    return code
