"""백테스트 실행과 결과 파일 (요약 md + 거래내역 csv + 일별 자산 csv)."""
from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from ..timeutil import stamp
from ..version import __version__
from . import engine as E
from .config import TAX_TABLE, Config
from .data import coverage, load_panel, open_ro


def _f(v, d=2):
    return "" if v is None else (f"{v:,.{d}f}" if isinstance(v, float) else str(v))


def run_backtest(db_path: Path, cfg: Config, variants: list[str], now: datetime,
                 progress: Callable[[str, float], None] = lambda s, x: None, themes_csv: Path | None = None,
                 random_n: int = 0) -> Path:
    con = open_ro(db_path)
    try:
        cov = coverage(con)
        progress("데이터 읽는 중", 0.02)
        panel = load_panel(con, cfg.s("START"), themes_csv)
    finally:
        con.close()
    start_idx = next((i for i, d in enumerate(panel.dates) if d >= cfg.s("START")), len(panel.dates))
    progress("지표 계산 중", 0.10)
    ind = E.indicators(panel, cfg)
    stop0, flow0 = cfg.f("STOP_PCT"), cfg.f("FLOW_TOP_PCT")
    specs = {v: E.spec(v, cfg) for v in variants}
    can = cov.get("variants", {})

    def why_not(v):
        sp = specs[v]
        need = can.get(sp.mask_base, "확인 불가")
        if need is None and sp.index_filter and not cov.get("index", {}).get("to"):
            return "KRX 지수 없음"
        return need

    todo = [v for v in variants if why_not(v) is None]
    skipped = {v: why_not(v) for v in variants if v not in todo}
    jobs = []
    for v in todo:
        for en in E.ENTRY_METHODS:
            for ex in E.EXIT_METHODS:
                jobs.append((v, en, ex, stop0, flow0, "main"))
        for s in cfg.ints("SENS_STOP_PCTS") if not v.endswith("_sameB") else []:
            if s != stop0:
                jobs.append((v, "1", "ga", float(s), flow0, "sens"))
        if specs[v].base in "BCD":
            for ft in cfg.ints("SENS_FLOW_TOP_PCTS"):
                if ft != flow0:
                    jobs.append((v, "1", "ga", stop0, float(ft), "sens"))
    results, covs, sig_cache = [], {}, {}
    for k, (v, en, ex, sp, ft, kind) in enumerate(jobs):
        progress(f"변형 {v} 진입{en} 청산{ex} 손절{sp:g}% 수급{ft:g}%", 0.15 + 0.8 * k / max(len(jobs), 1))
        spc = specs[v]
        if v not in covs:
            listed, ok, reasons = E.need_masks(panel, ind, spc.mask_base, start_idx, need_index=spc.index_filter)
            covs[v] = (E.coverage_of(panel, ok, listed, reasons, v), ok)
        if (v, ft) not in sig_cache:
            sig_cache[(v, ft)] = E.signals(panel, ind, cfg, spc, covs[v][1], ft)
        sig, score, score2 = sig_cache[(v, ft)]
        R = E.simulate(panel, ind, cfg, v, sig, score, en, ex, sp, ft, start_idx, score2=score2)
        results.append((kind, R))
    randoms = {}
    if random_n > 0:
        base_res = [R for k, R in results if k == "main" and R.entry == "1" and R.exit == "ga"]
        seed = cfg.i("RANDOM_SEED")
        for vi, R in enumerate(base_res):
            spc = specs[R.variant]
            pool = E.candidate_pool(panel, ind, cfg, spc, covs[R.variant][1])
            sig, score, score2 = sig_cache[(R.variant, flow0)]
            vals = []
            for i in range(random_n):
                progress(f"무작위 비교 {R.variant} {i + 1}/{random_n}", 0.95)
                rng = np.random.default_rng([seed, vi, i])
                RR = E.simulate(panel, ind, cfg, R.variant, sig, score, "1", "ga", stop0, flow0, start_idx,
                                random_pool=pool, rng=rng)
                m = E.metrics(RR)
                vals.append((m.get("cagr_pct"), m.get("mdd_pct")))
            randoms[R.variant] = (E.metrics(R), vals)
    out_root = db_path.parent / "results"
    out = out_root / stamp(now)
    out.mkdir(parents=True, exist_ok=True)
    _write_files(out, panel, cfg, cov, covs, results, skipped, now, randoms, random_n)
    latest = out_root / "latest"
    if latest.exists():
        shutil.rmtree(latest)
    shutil.copytree(out, latest)
    progress("완료", 1.0)
    return out


def _idx_yearly(panel, mk):
    s = [(d, float(v)) for d, v in zip(panel.dates, panel.index_close.get(mk, [])) if not np.isnan(v)]
    return E.yearly(s)


def _pctile(x, arr):
    arr = [a for a in arr if a is not None]
    if x is None or not arr:
        return None
    return sum(1 for a in arr if a < x) / len(arr) * 100


def _write_files(out, panel, cfg, cov, covs, results, skipped, now, randoms=None, random_n=0):
    with (out / "trades.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["variant", "entry", "exit", "stop_pct", "flow_top_pct", "code", "name", "signal_date", "entry_date",
                    "entry_price", "shares", "exit_date", "exit_price", "reason", "pnl_krw", "ret_pct", "hold_days",
                    "pre_ret20_pct", "index_ret_pct", "excess_pct"])
        for kind, R in results:
            for x in R.trades:
                w.writerow([R.variant, R.entry, R.exit, R.stop_pct, R.flow_top, x["code"], x["name"], x["signal_date"],
                            x["entry_date"], x["entry_price"], x["shares"], x["exit_date"], x["exit_price"], x["reason"],
                            x["pnl_krw"], x["ret_pct"], x["hold_days"], x.get("pre_ret20_pct"),
                            x.get("index_ret_pct"), x.get("excess_pct")])
    with (out / "equity.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "variant", "entry", "exit", "stop_pct", "flow_top_pct", "equity_krw"])
        for kind, R in results:
            for d, v in R.equity:
                w.writerow([d, R.variant, R.entry, R.exit, R.stop_pct, R.flow_top, round(v)])
    dev_end = cfg.s("DEV_END")
    L = [f"# 추세추종 백테스트 결과 {now.strftime('%Y-%m-%d %H:%M')}", "",
         f"프로그램 {__version__}. 계산만 했으며 주문·매매는 하지 않습니다. 비용·슬리피지를 넣은 가정치입니다.", ""]
    from .data import universe_lines
    backdrop = cfg.s("BACKDROP_INDEX_FILTER").strip().lower() == "on"
    L += ["## 배경 설정", "",
          f"- 시장 배경(BACKDROP_INDEX_FILTER): **{'on' if backdrop else 'off'}**"
          + (" - 모든 변형에 지수 필터(해당 시장 지수 < 120일선이면 신규 매수 중단)를 똑같이 적용"
             if backdrop else " - 지수 필터는 D·A3·A4(및 INDEX_FILTER_FOR_A=on)에만"),
          "- 비교는 같은 배경 설정으로 낸 결과끼리만 하세요.",
          "- " + universe_lines(panel.universe_note)[0], ""]
    L += ["## 계산 범위", ""]
    for v, (cv, _) in covs.items():
        ratio = cv.pairs / cv.listed_pairs * 100 if cv.listed_pairs else 0
        L.append(f"- **변형 {v} ({E.VARIANTS[v]})**: {cv.first or '-'}~{cv.last or '-'}, 종목 {cv.codes}개, "
                 f"종목·날짜 {cv.pairs:,} / 상장 {cv.listed_pairs:,} ({ratio:.1f}%)")
        for why, n in cv.reasons.items():
            if n:
                L.append(f"  - 빠진 이유: {why} {n:,}건")
        if cv.big_only_warning:
            L.append("  - **경고: KIS 수급이 아직 대형주부터 쌓이는 중이라 '대형주 위주 결과'입니다.**")
    for v, why in skipped.items():
        L.append(f"- 변형 {v}: 실행 안 함 ({why})")
    L += ["", "## 진입·청산 방식 비교 (손절·수급 기본값)", ""]
    head = ["변형", "진입", "청산", "연환산%", "최대낙폭%", "승률%", "평균수익%", "평균손실%", "거래당기대값%",
            "평균초과수익%", "초과수익승률%", "거래", "평균보유일", "지정가체결%", "놓친신호20일%"]
    L += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for kind, R in results:
        if kind != "main":
            continue
        m = E.metrics(R)
        L.append("| " + " | ".join([R.variant, E.ENTRY_METHODS[R.entry], E.EXIT_METHODS[R.exit], _f(m.get("cagr_pct")),
                                    _f(m.get("mdd_pct")), _f(m.get("win_rate_pct")), _f(m.get("avg_win_pct")),
                                    _f(m.get("avg_loss_pct")), _f(m.get("expectancy_pct")), _f(m.get("avg_excess_pct")),
                                    _f(m.get("excess_win_pct")), str(m["trades"]),
                                    _f(m.get("avg_hold_days"), 1),
                                    _f(m.get("limit_fill_pct")), _f(m.get("missed_ret20_pct"))]) + " |")
    L += ["", f"## 기간 분할 (개발 ~{dev_end} / 검증 {int(dev_end[:4]) + 1}~), ① 다음 날 시가 + (가)", ""]
    L += ["| 변형 | 구간 | 연환산% | 최대낙폭% | 승률% | 거래 |", "|---|---|---|---|---|---|"]
    for kind, R in results:
        if kind == "main" and R.entry == "1" and R.exit == "ga":
            for label, s, e in (("개발", None, dev_end), ("검증", str(int(dev_end) + 1), None)):
                m = E.metrics(R, s, e)
                L.append(f"| {R.variant} | {label} | {_f(m.get('cagr_pct'))} | {_f(m.get('mdd_pct'))} | "
                         f"{_f(m.get('win_rate_pct'))} | {m['trades']} |")
    L += ["", "## 연도별 수익률 (%), ① + (가), 같은 기간 지수 비교", ""]
    base = [R for k, R in results if k == "main" and R.entry == "1" and R.exit == "ga"]
    years = sorted({d[:4] for R in base for d, _ in R.equity})
    ky, qy = _idx_yearly(panel, "KOSPI"), _idx_yearly(panel, "KOSDAQ")
    L += ["| 연도 | " + " | ".join(f"변형 {R.variant}" for R in base) + " | 코스피 | 코스닥 |",
          "|---|" + "---|" * (len(base) + 2)]
    yb = [E.yearly(R.equity) for R in base]
    for y in years:
        L.append(f"| {y} | " + " | ".join(_f(x.get(y)) for x in yb) + f" | {_f(ky.get(y))} | {_f(qy.get(y))} |")
    # 공정 비교: A와 B의 차이가 수급 효과인지 종목 범위 효과인지
    fair = [R for R in base if R.variant in ("A", "A_sameB", "B", "B2", "A4", "A4_sameB")]
    if fair:
        L += ["", "## 공정 비교 (① + (가)): 같은 종목·날짜 범위에서 비교", "",
              "A_sameB·A4_sameB는 A·A4 규칙을 B가 실제로 계산된 종목·날짜에만 적용한 결과입니다. "
              "A_sameB와 B의 차이는 수급 효과, A와 A_sameB의 차이는 종목 범위 효과입니다. B2는 B에서 거래대금 급증 조건만 뺀 것으로 이미 B와 같은 범위라 B2_sameB = B2입니다. A_sameB와 B2의 차이는 수급 조건만의 효과, B2와 B의 차이는 거래대금 급증 조건의 효과입니다.", "",
              "| 변형 | 연환산% | 최대낙폭% | 승률% | 거래당기대값% | 거래 |", "|---|---|---|---|---|---|"]
        for R in sorted(fair, key=lambda r: E.ALL_VARIANTS.index(r.variant)):
            m = E.metrics(R)
            L.append(f"| {R.variant} | {_f(m.get('cagr_pct'))} | {_f(m.get('mdd_pct'))} | {_f(m.get('win_rate_pct'))} | "
                     f"{_f(m.get('expectancy_pct'))} | {m['trades']} |")
    L += ["", "## 손실 상위 10건 (변형별, ① + (가))", ""]
    for R in base:
        worst = sorted(R.trades, key=lambda x: x["ret_pct"])[:10]
        if not worst:
            continue
        L += [f"**{R.variant}**", "", "| 종목 | 진입일 | 청산일 | 수익률% | 진입 직전 20일 수익률% | 청산 이유 |", "|---|---|---|---|---|---|"]
        L += [f"| {x['code']} {x['name']} | {x['entry_date']} | {x['exit_date']} | {x['ret_pct']:.2f} | "
              f"{_f(x.get('pre_ret20_pct'))} | {x['reason']} |" for x in worst]
        L.append("")
    if randoms:
        L += ["", f"## 무작위 비교 (① + (가), {random_n}회, 시드 {cfg.s('RANDOM_SEED')})", "",
              "같은 날 같은 후보군(거르기 통과 종목)에서 전략이 산 수만큼 무작위로 골라 같은 규칙으로 매매한 결과와 비교합니다. "
              "백분위가 높을수록 무작위보다 낫습니다(낙폭은 덜 깊을수록 높음).", "",
              "| 변형 | 전략 연환산% | 무작위 연환산 중앙값% | 백분위 | 전략 최대낙폭% | 무작위 낙폭 중앙값% | 백분위 |",
              "|---|---|---|---|---|---|---|"]
        for v, (m, vals) in randoms.items():
            cg = sorted(a for a, _ in vals if a is not None)
            md = sorted(b for _, b in vals if b is not None)
            med = lambda a: a[len(a) // 2] if a else None
            L.append(f"| {v} | {_f(m.get('cagr_pct'))} | {_f(med(cg))} | {_f(_pctile(m.get('cagr_pct'), cg), 0)} | "
                     f"{_f(m.get('mdd_pct'))} | {_f(med(md))} | {_f(_pctile(m.get('mdd_pct'), md), 0)} |")
    L += ["", "## 민감도 (① + (가))", "", "| 변형 | 손절% | 수급 상위% | 연환산% | 최대낙폭% | 승률% | 거래 |",
          "|---|---|---|---|---|---|---|"]
    for kind, R in results:
        if (kind == "sens") or (kind == "main" and R.entry == "1" and R.exit == "ga"):
            m = E.metrics(R)
            ft = f"{R.flow_top:g}" if R.variant[0] in "BCD" else "-"
            L.append(f"| {R.variant} | {R.stop_pct:g} | {ft} | "
                     f"{_f(m.get('cagr_pct'))} | {_f(m.get('mdd_pct'))} | {_f(m.get('win_rate_pct'))} | {m['trades']} |")
    L += ["", "## 규칙과 가정", "",
          f"- 기간 {cfg.s('START')}~마지막 데이터일, 수정계수 적용 가격, 날짜별로 그날 상장된 보통주만(상장폐지 포함)",
          f"- 거르기: 시총 {cfg.f('MIN_MKTCAP_KRW') / 1e8:,.0f}억 원 이상, 20일 평균 거래대금 {cfg.f('MIN_VALUE20_KRW') / 1e8:,.0f}억 원 이상",
          f"- 진입: 종가 > 60일선 > 120일선, 60일선 > 20일 전 60일선, 250일 범위 위치 {cfg.s('POS250_MIN_PCT')}% 이상 (장 마감 후 판단)",
          f"- B: 최근 {cfg.s('FLOW_DAYS')}일 외국인+기관 순매수/시총 상위 {cfg.s('FLOW_TOP_PCT')}%, 거래대금 ≥ 직전 20일 평균 x{cfg.s('VALUE_SPIKE_X')}",
          f"- C: 최근 4분기 영업이익 합 > 0, 부채비율 < {cfg.s('DEBT_RATIO_MAX_PCT')}%, 자본총계 > 0 (완전자본잠식 제외; 부분잠식은 자본금 자료가 없어 판별 안 함). 공시일 다음 거래일부터 반영",
          f"- D: 해당 시장 지수 종가 < {cfg.s('INDEX_MA_DAYS')}일선이면 신규 매수 중단",
          f"- 지정가 최대 {cfg.s('LIMIT_WAIT_DAYS')}거래일 대기, 저가 ≤ 지정가면 체결, 체결가 = min(지정가, 시가)",
          f"- 청산: 최고 종가 대비 -{cfg.s('STOP_PCT')}% 또는 종가 < 60일선. 상장폐지는 마지막 종가로 청산",
          f"- 비중: 최대 {cfg.s('MAX_POSITIONS')}종목 동일 금액(전날 총자산/종목 수), 같은 테마 최대 {cfg.s('MAX_PER_THEME')}종목"
          + (" (테마 파일 사용)" if panel.themes else " (테마 파일 없음: 제한 미적용)"),
          f"- 비용: 매수 {cfg.s('BUY_FEE_PCT')}%, 매도 {cfg.s('SELL_FEE_PCT')}% + 거래세, 슬리피지 편도 {cfg.s('SLIPPAGE_PCT')}%",
          "- 거래세(코스피 농특세 포함 합계): " + ", ".join(f"{d[:4]}.{d[4:6]}~ {r}%" for d, r in TAX_TABLE)
          + " (2019년 이전 세율은 간접 확인, 확인 필요)",
          f"- 초기 자금 {cfg.f('INITIAL_CASH_KRW'):,.0f}원"]
    (out / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
