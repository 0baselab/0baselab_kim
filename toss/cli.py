"""명령줄: auth / candles-depth / candles-compare / rankings / stock-info / market / limits / report."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from collector.timeutil import now_kst
from danta.cli import _path_masks, mask_paths

from . import collect, commands, datastore, store
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
    p = argparse.ArgumentParser(prog="토스단타", description="토스증권 Open API 조회 전용 수집·측정 (주문 기능 없음)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("universe", help="[수집] 주식 보통주 목록 갱신 (KOSPI·KOSDAQ)")
    sub.add_parser("daily-update", help="[수집·아침용] 일봉(+설정 시 수급)을 전 영업일까지 보충")
    mu = sub.add_parser("minute-update", help="[수집·검증용] 분봉 날짜 파일 채우기 (최근 날짜부터, 이어받기)")
    mu.add_argument("--max-minutes", type=float, default=0, help="이번 실행 최대 시간(분). 0=제한 없음")
    mu.add_argument("--rate", type=float, help="캔들 초당 호출 수 (기본: 설정 CHART_RATE=4)")
    mu.add_argument("--date", help="YYYYMMDD 하루만")
    mu.add_argument("--backfill", action="store_true",
                    help="빨리 채우기: 최근 미완성 날짜를 먼저 완성하고, 과거 날짜는 종목 단위로 연속 수집 (약 16%% 빠름)")
    mu.add_argument("--targets", help="CSV 파일(date,code)에 적힌 종목·날짜만 받기. 같은 종목의 전 거래일도 함께 받음")
    mu.add_argument("--regular-only", action="store_true",
                    help="--targets와 함께: 정규장(09:00~15:30) 봉만 받기. 호출 약 절반. code_done_reg에 기록")
    sub.add_parser("adjust-update", help="[수집] 수정주가 종가 1년치 갱신 (보통은 minute-update가 주 1회 자동으로 함)")
    sub.add_parser("sector-update", help="[수집] 토스 섹터와 구성 종목 스냅샷 (보통은 daily-update가 주 1회 자동으로 함)")
    sub.add_parser("status", help="[수집] 일봉·분봉이 어디까지 받아졌는지")
    tr = sub.add_parser("trade", help="[매매] 1단계: replay(저장된 분봉 재생, 가상 체결)만 가능. 실주문 없음")
    tr.add_argument("--mode", choices=["replay"], default="replay")
    tr.add_argument("--from", dest="d_from", help="YYYYMMDD (기본: 완성된 분봉이 있는 첫 날)")
    tr.add_argument("--to", dest="d_to", help="YYYYMMDD (기본: 완성된 분봉이 있는 마지막 날)")
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
    k = sub.add_parser("candles-check", help="KIS 없이 토스 1분봉을 같은 날 토스 일봉과 대조 (자체 점검)")
    k.add_argument("--date", help="YYYYMMDD. 비우면 전 영업일 (아침에 전일자 점검)")
    k.add_argument("--symbols", help="콤마로 구분한 종목코드. 비우면 오늘 거래대금 순위 상위 n/2 + 100위 쪽 n/2")
    k.add_argument("--n", type=int, default=10)
    sub.add_parser("limits", help="기록된 그룹별 호출 한도 헤더 요약")
    sub.add_parser("report", help="측정 결과 요약(마크다운)과 KIS 분봉 대체 판정")
    return p


def run_trade(args, cfg, install, log) -> None:
    from .trade import engine, store as tstore
    from .trade.settings import TradeSettings
    s = TradeSettings.load(install / "config" / "trade_settings.env")
    dcon = sqlite3.connect(f"{(cfg.store_dir / 'daily.sqlite').resolve().as_uri()}?mode=ro", uri=True)
    all_days = [r[0] for r in dcon.execute("SELECT date FROM trading_day ORDER BY date")]
    iso = lambda x: f"{x[:4]}-{x[4:6]}-{x[6:]}" if x else None
    lo, hi = iso(args.d_from), iso(args.d_to)
    days = [d for i, d in enumerate(all_days) if (not lo or d >= lo) and (not hi or d <= hi)
            and engine.replay_ready(dcon, cfg.store_dir, d, all_days[i - 1] if i else None, s)]
    if not days:
        raise SystemExit("재생할 날짜가 없습니다. 그날 관심종목(W7)과 전날 분봉이 받아진 날이 없음 (minute-update --backfill 진행 필요).")
    tcon = tstore.connect(cfg.data_dir / "trade.sqlite")
    started = now_kst()
    run_id = f"replay-{started.strftime('%Y%m%d-%H%M%S')}"
    tstore.start_run(tcon, run_id, "replay", started.isoformat(timespec="seconds"), days[0], days[-1], vars(s))
    log(f"[trade replay] {run_id}: {days[0]}~{days[-1]} {len(days)}일, 시드 {s.SEED_KRW:,}원, 1회 {s.ORDER_KRW:,}원, "
        f"동시 {s.MAX_POSITIONS}종목, 하루 손실 한도 {s.DAILY_LOSS_KRW:,}원, 누적 {s.TOTAL_LOSS_KRW:,}원")
    log(f"  전략: 시가 범위 09:00~09:29 돌파, 손절 {s.STOP_PCT * 100:g}%(또는 범위 저가), 익절 "
        f"{s.TAKE_PROFIT_PCT * 100:g}%, 15:19 청산, 비용 매수 {s.BUY_COST_RATE * 100:g}% 매도 {s.SELL_COST_RATE * 100:g}%, "
        f"슬리피지 {s.SLIPPAGE_TICKS}호가")
    summ = engine.replay(dcon, cfg.store_dir, days, s, tcon, run_id, log, all_days)
    if summ.get("disabled"):
        log(f"  누적 손실 한도 도달: {summ['disabled']}")
    engine.report(tcon, run_id, log)
    with tcon:
        tcon.execute("UPDATE run SET ended_at=? WHERE run_id=?", (now_kst().isoformat(timespec="seconds"), run_id))
    log(f"  기록: {cfg.data_dir / 'trade.sqlite'} (run_id {run_id})")


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
    lock = None
    try:
        log(f"토스단타 {__version__} / {args.cmd} / 데이터 폴더 {cfg.data_dir}")
        rf = cfg.num("RATE_FACTOR")
        if args.cmd not in ("limits", "report", "status", "trade"):  # 토큰은 1개뿐이라 동시에 두 개 실행하지 않는다
            from collector.lock import AlreadyRunning, Lock
            lock = Lock(cfg.lock_path)
            try:
                lock.acquire()
            except AlreadyRunning as exc:
                raise SystemExit(f"토스단타가 이미 실행 중입니다. ({exc})")
        if args.cmd == "trade":  # replay는 API를 부르지 않고 파일만 읽으므로 수집 중에도 실행 가능
            run_trade(args, cfg, install, log)
        elif args.cmd == "status":
            collect.status(datastore.connect_daily(cfg.store_dir), cfg.store_dir, log, now_kst, int(cfg.num("HISTORY_DAYS")))
        elif args.cmd == "limits":
            commands.cmd_limits(con, log, rf)
        elif args.cmd == "report":
            path = commands.cmd_report(con, log, now_kst, cfg.data_dir)
            log(f"보고서 파일: {path}")
        else:
            keys = load_keys(install)
            client = TossClient(cfg.get("BASE_URL"), keys["TOSS_CLIENT_ID"], keys["TOSS_CLIENT_SECRET"],
                                cfg.token_path, rate_factor=rf, retry_429=int(cfg.num("RETRY_429")),
                                group_rates={"MARKET_DATA_CHART": cfg.num("CHART_RATE")},
                                on_rate=lambda rec: rate_buf.append((now_kst().isoformat(timespec="seconds"), rec)))
            mc = int(cfg.num("MAX_CALLS"))
            yrs = cfg.num("MAX_YEARS")
            hd = int(cfg.num("HISTORY_DAYS"))
            if args.cmd in ("universe", "daily-update", "minute-update", "adjust-update", "sector-update"):
                dcon = datastore.connect_daily(cfg.store_dir)
            if args.cmd == "universe":
                collect.refresh_universe(client, dcon, log, now_kst)
            elif args.cmd == "daily-update":
                collect.daily_update(client, dcon, cfg.store_dir, log, now_kst, hd,
                                     investor=cfg.get("INVESTOR_DAILY").upper() in ("Y", "1", "TRUE"))
            elif args.cmd == "sector-update":
                collect.ensure_universe(client, dcon, log, now_kst)
                collect.sector_update(client, dcon, log, now_kst)
            elif args.cmd == "adjust-update":
                collect.ensure_universe(client, dcon, log, now_kst)
                collect.adjust_update(client, dcon, log, now_kst, hd)
            elif args.cmd == "minute-update":
                if args.rate:
                    client.set_group_rate("MARKET_DATA_CHART", args.rate)
                if not args.date and not args.targets:
                    collect.index_minute_update(client, dcon, log, now_kst, hd)
                if not args.date and not args.targets and collect.adjust_due(dcon, now_kst, int(cfg.num("ADJ_REFRESH_DAYS"))):
                    collect.ensure_universe(client, dcon, log, now_kst)
                    log("수정주가 갱신 주기가 되어 먼저 갱신합니다 (약 25분, 주 1회)")
                    collect.adjust_update(client, dcon, log, now_kst, hd, max_minutes=args.max_minutes)
                day = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:]}" if args.date else None
                if args.targets:
                    collect.targets_update(client, dcon, cfg.store_dir, log, now_kst, Path(args.targets),
                                           args.max_minutes, int(cfg.num("MAX_RETRY")), regular_only=args.regular_only)
                elif args.backfill and not day:
                    collect.minute_backfill(client, dcon, cfg.store_dir, log, now_kst, hd, args.max_minutes,
                                            int(cfg.num("MAX_RETRY")), chunk_days=int(cfg.num("BACKFILL_CHUNK_DAYS")))
                else:
                    collect.minute_update(client, dcon, cfg.store_dir, log, now_kst, hd, args.max_minutes,
                                          int(cfg.num("MAX_RETRY")), only_day=day)
            elif args.cmd == "auth":
                commands.cmd_auth(client, con, log, now_kst)
            elif args.cmd == "candles-depth":
                commands.cmd_candles_depth(client, con, log, now_kst, args.symbol, args.max_calls or mc, yrs)
            elif args.cmd == "candles-compare":
                from danta.source import SourceDbError, open_ro
                try:
                    danta = open_ro(cfg.danta_db)
                except SourceDbError:
                    raise SystemExit(f"danta.sqlite가 없습니다: {cfg.danta_db} (config/toss_settings.env의 DANTA_DB로 위치 지정). "
                                     "KIS 분봉 없이 점검하려면 candles-check를 쓰세요.")
                commands.cmd_candles_compare(client, con, danta, log, now_kst, args.date, args.n)
            elif args.cmd == "candles-check":
                syms = [x.strip() for x in (args.symbols or "").split(",") if x.strip()] or None
                commands.cmd_candles_check(client, con, log, now_kst, args.date, syms, args.n)
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
        if lock is not None:
            lock.release()
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
