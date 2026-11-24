"""명령줄: auth / candles-depth / candles-compare / rankings / stock-info / market / limits / report."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from collector.timeutil import now_kst
from danta.cli import _path_masks, mask_paths

from . import commands, store
from .client import IpNotAllowedError, NotAllowedError, TossApiError, TossAuthError, TossClient, redact
from .config import install_dir, load_config, load_keys
from .version import __version__

IP_HELP = ("허용되지 않은 IP에서 호출했습니다. 토스증권 WTS(웹)에 로그인 → 설정 → Open API → 허용 IP 관리에서 "
           "이 PC의 공인 IP를 등록한 뒤 다시 실행하세요. 공인 IP는 브라우저에서 '내 IP'를 검색하면 확인할 수 있습니다.")


class Logger:
    """화면과 logs/toss_<명령>_<시각>.txt에 같은 내용. 키·토큰·계좌번호와 사용자 폴더 경로를 가린다."""

    def __init__(self, log_dir, name: str, now: datetime):
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"toss_{name}_{now.strftime('%Y%m%d-%H%M%S')}.txt"
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
    p = argparse.ArgumentParser(prog="토스측정", description="토스증권 Open API 조회 전용 측정 (주문 기능 없음)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("auth", help="토큰 발급, 계좌 목록 조회")
    d = sub.add_parser("candles-depth", help="1분봉·일봉을 과거로 넘기며 가장 오래된 날짜 측정")
    d.add_argument("--symbol", default="005930")
    d.add_argument("--max-calls", type=int, help="봉 단위별 호출 상한 (기본 설정 MAX_CALLS=3000)")
    c = sub.add_parser("candles-compare", help="토스 1분봉과 KIS 분봉(danta.sqlite) 비교")
    c.add_argument("--date", required=True, help="YYYYMMDD")
    c.add_argument("--n", type=int, default=10, help="종목 수 (상위 n/2 + 하위 n/2, 기본 10)")
    r = sub.add_parser("rankings", help="순위 조회 (장중 실행 권장)")
    r.add_argument("--watch", type=float, default=0, help="이 초 간격으로 실시간 순위 반복 조회 (예: 60)")
    r.add_argument("--times", type=int, default=10, help="--watch 반복 횟수 (기본 10)")
    s = sub.add_parser("stock-info", help="종목 목록·상장폐지·매수 유의사항·수급 과거 깊이")
    s.add_argument("--symbol", default="005930")
    m = sub.add_parser("market", help="장 운영 시간, 지수 1분봉 깊이, 섹터")
    m.add_argument("--max-calls", type=int, help="지수별 호출 상한 (기본 설정 MAX_CALLS)")
    sub.add_parser("limits", help="기록된 그룹별 호출 한도 헤더 요약")
    sub.add_parser("report", help="측정 결과 요약(마크다운)과 KIS 분봉 대체 판정")
    return p


def run(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    install = install_dir()
    cfg = load_config(install)
    now = now_kst()
    log = Logger(cfg.log_dir, args.cmd, now)
    con = store.connect(cfg.db_path)
    rate_buf = []
    try:
        log(f"토스측정 {__version__} / {args.cmd} / 데이터 폴더 {cfg.data_dir}")
        rf = cfg.num("RATE_FACTOR")
        if args.cmd == "limits":
            commands.cmd_limits(con, log, rf)
        elif args.cmd == "report":
            path = commands.cmd_report(con, log, now_kst, cfg.data_dir)
            log(f"보고서 파일: {path}")
        else:
            keys = load_keys(install)
            client = TossClient(cfg.get("BASE_URL"), keys["TOSS_CLIENT_ID"], keys["TOSS_CLIENT_SECRET"],
                                cfg.token_path, rate_factor=rf, retry_429=int(cfg.num("RETRY_429")),
                                on_rate=lambda rec: rate_buf.append((now_kst().isoformat(timespec="seconds"), rec)))
            mc = int(cfg.num("MAX_CALLS"))
            yrs = cfg.num("MAX_YEARS")
            if args.cmd == "auth":
                commands.cmd_auth(client, con, log, now_kst)
            elif args.cmd == "candles-depth":
                commands.cmd_candles_depth(client, con, log, now_kst, args.symbol, args.max_calls or mc, yrs)
            elif args.cmd == "candles-compare":
                from danta.source import SourceDbError, open_ro
                try:
                    danta = open_ro(cfg.danta_db)
                except SourceDbError as exc:
                    raise SystemExit(str(exc).replace("기존 수집기 DB", "danta.sqlite"))
                commands.cmd_candles_compare(client, con, danta, log, now_kst, args.date, args.n)
            elif args.cmd == "rankings":
                commands.cmd_rankings(client, con, log, now_kst, args.watch, args.times)
            elif args.cmd == "stock-info":
                commands.cmd_stock_info(client, con, log, now_kst, args.symbol)
            elif args.cmd == "market":
                commands.cmd_market(client, con, log, now_kst, args.max_calls or mc, yrs)
            log(f"이번 실행 호출 {client.calls}회 (토큰 발급 {client.token_issues}회 포함), 429 응답 {client.n429}회")
        log(f"로그 파일: {log.path}")
        return 0
    except IpNotAllowedError as exc:
        log(f"중단: {exc}")
        log(IP_HELP)
        return 1
    except TossAuthError as exc:
        log(f"중단(인증): {exc}. TOSS_CLIENT_ID / TOSS_CLIENT_SECRET과 허용 IP를 확인하세요.")
        return 1
    except NotAllowedError as exc:
        log(f"중단(차단됨): {exc}")
        return 1
    except TossApiError as exc:
        log(f"중단(API 오류): {exc}")
        return 1
    except SystemExit as exc:
        if isinstance(exc.code, str):
            log(f"중단: {exc.code}")
            return 1
        raise
    except KeyboardInterrupt:
        log("사용자가 중단했습니다.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log(f"오류로 중단: {type(exc).__name__}: {exc}")
        return 1
    finally:
        with con:
            for at, rec in rate_buf:
                store.log_rate(con, at, rec)
        con.close()
        log.close()


def main() -> int:
    argv = sys.argv[1:]
    code = run(argv)
    if not argv and getattr(sys, "frozen", False):
        input("\n엔터를 누르면 닫힙니다.")
    return code
