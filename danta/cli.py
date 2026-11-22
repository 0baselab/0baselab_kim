"""명령줄: probe / ratetest / jobs / backfill / recalc."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from collector.timeutil import now_kst

from . import commands, source, store
from .config import install_dir, load_config, load_keys
from .kis import KisMinuteClient, redact
from .version import __version__


class Logger:
    """화면과 logs/danta_<명령>_<시각>.txt에 같은 내용을 남긴다. 키·토큰은 가린다."""

    def __init__(self, log_dir, name: str, now: datetime):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"danta_{name}_{now.strftime('%Y%m%d-%H%M%S')}.txt"
        self.f = self.path.open("a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"{now_kst().strftime('%H:%M:%S')} {redact(msg)}"
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
    rt = sub.add_parser("ratetest", help="초당 호출 한도 단계별 측정")
    rt.add_argument("--code", default="005930")
    rt.add_argument("--date", help="YYYYMMDD (기본: 직전 거래일)")
    sub.add_parser("jobs", help="기존 일봉 DB에서 (날짜, 종목) 작업 목록 생성")
    bf = sub.add_parser("backfill", help="작업 목록을 오래된 날짜부터 수집 (이어받기)")
    bf.add_argument("--rate", type=float, help="초당 호출 수 (기본: 설정 RATE)")
    bf.add_argument("--max-calls", type=int, default=0, help="이번 실행 호출 상한 (0=무제한)")
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
        elif args.cmd == "recalc":
            n = store.recalc_values(store.connect(cfg.db_path))
            log(f"[recalc] {n}개 (종목, 날짜) 다시 계산")
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
                                  cfg.int("MAX_FAILURES"), cfg.acml_is_cumulative)
        log(f"로그 파일: {log.path}")
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
