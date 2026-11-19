"""KRX 수집: 일별매매정보·종목기본정보·지수 (명세서 4.1, 6장, 14장).

필드명은 실측(2026-11-17 원본)으로 확인한 이름만 쓴다.
- 일별매매정보: ISU_CD(단축코드), TDD_OPNPRC/HGPRC/LWPRC/CLSPRC, FLUC_RT, ACC_TRDVOL, ACC_TRDVAL, MKTCAP, LIST_SHRS
- 종목기본정보: ISU_SRT_CD(단축코드), ISU_ABBRV, LIST_DD, SECUGRP_NM, SECT_TP_NM, KIND_STKCERT_TP_NM, LIST_SHRS
- 지수: IDX_NM, IDX_CLSS, CLSPRC_IDX, FLUC_RT, OPNPRC_IDX, HGPRC_IDX, LWPRC_IDX, ACC_TRDVOL, ACC_TRDVAL, MKTCAP
KRX는 당일 값을 장 마감 후에 주지 않으므로(14.1) 오늘 날짜는 받지 않는다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import db
from .http import ApiError
from .krx import BASE_INFO_PATHS, ROWS_KEY, TRADE_PATHS, KrxClient
from .rawstore import save_raw

log = logging.getLogger("collector")

CODE_RE = re.compile(r"^[0-9A-Z]{6}$")
MARKETS = ("KOSPI", "KOSDAQ")
INDEX_SETTING = {"KOSPI": "KRX_INDEX_KOSPI_URL", "KOSDAQ": "KRX_INDEX_KOSDAQ_URL"}
PRICE_COMPARE_COLS = ("open", "high", "low", "close", "volume_sh", "value_krw", "mktcap_krw")


class KrxAuthError(Exception):
    pass


def valid_code(code) -> bool:
    return bool(CODE_RE.match(str(code or "")))


def to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(v):
    f = to_float(v)
    return None if f is None else int(round(f))


def datasets() -> list[str]:
    return [f"{kind}_{m}" for kind in ("trade", "base", "index") for m in MARKETS]


@dataclass
class KrxStats:
    success: int = 0
    empty: int = 0
    failed: int = 0
    skipped: int = 0
    replaced_provisional: int = 0
    bad_codes: int = 0
    base_dates: set = field(default_factory=set)
    errors: list = field(default_factory=list)


def pending_units(con, dates: list[str], force: bool) -> list[tuple[str, str]]:
    out = []
    for d in dates:
        for ds in datasets():
            st = db.get_status(con, "KRX", ds, d)
            if force or st not in ("success", "holiday"):
                out.append((d, ds))
    return out


def _fetch(krx: KrxClient, settings: dict, ds: str, day: str) -> dict | None:
    kind, market = ds.split("_")
    if kind == "trade":
        return krx.get(TRADE_PATHS[market], day)
    if kind == "base":
        return krx.get(BASE_INFO_PATHS[market], day)
    url = (settings.get(INDEX_SETTING[market]) or "").strip()
    if not url:
        return None
    return krx.get_url(url, day)


def collect(con, krx: KrxClient, settings: dict, root, units: list[tuple[str, str]],
            now_fn: Callable[[], datetime], progress: Callable[[str], None] = lambda s: None) -> KrxStats:
    stats = KrxStats()
    total = len(units)
    for i, (day, ds) in enumerate(units, 1):
        now = now_fn()
        ts = now.isoformat(timespec="seconds")
        if i % 20 == 0 or i == total:
            progress(f"  KRX {i}/{total} ({day} {ds})")
        try:
            body = _fetch(krx, settings, ds, day)
        except ApiError as exc:
            msg = str(exc)
            if "인증 실패" in msg:
                raise KrxAuthError(msg)
            with db.Tx(con):
                db.set_status(con, "KRX", ds, day, "failed", ts, msg)
            stats.failed += 1
            stats.errors.append(f"{day} {ds}: {msg}")
            continue
        if body is None:
            stats.skipped += 1
            stats.errors.append(f"{ds}: 설정 필요 (KRX 지수 URL이 비어 있음)")
            continue
        raw = save_raw(root, "krx", ds, day, body, now)
        rows = body.get(ROWS_KEY)
        if not isinstance(rows, list):
            msg = f"응답에 {ROWS_KEY} 없음 (최상위 키: {list(body.keys())[:5]})"
            with db.Tx(con):
                db.set_status(con, "KRX", ds, day, "failed", ts, msg, raw)
            stats.failed += 1
            stats.errors.append(f"{day} {ds}: {msg}")
            continue
        if not rows:  # 개장일의 빈 결과는 절대 success가 아니다 (명세서 6.4)
            with db.Tx(con):
                db.set_status(con, "KRX", ds, day, "empty", ts, None, raw)
            stats.empty += 1
            continue
        with db.Tx(con):
            kind, market = ds.split("_")
            if kind == "trade":
                _save_trade(con, day, market, rows, ts, raw, stats)
            elif kind == "base":
                _save_base(con, day, market, rows, ts, raw, stats)
                stats.base_dates.add(day)
            else:
                _save_index(con, day, market, rows, ts, raw)
            db.set_status(con, "KRX", ds, day, "success", ts, None, raw)
        stats.success += 1
    return stats


def _save_trade(con, day, market, rows, ts, raw, stats: KrxStats) -> None:
    for r in rows:
        code = str(r.get("ISU_CD") or "").strip()
        if not valid_code(code):
            stats.bad_codes += 1
            log.warning("KRX 일별매매정보 종목코드 형식 오류: %r (%s %s)", code, day, market)
            continue
        new = {
            "open": to_float(r.get("TDD_OPNPRC")), "high": to_float(r.get("TDD_HGPRC")),
            "low": to_float(r.get("TDD_LWPRC")), "close": to_float(r.get("TDD_CLSPRC")),
            "change_rate_pct": to_float(r.get("FLUC_RT")),
            "volume_sh": to_int(r.get("ACC_TRDVOL")), "value_krw": to_int(r.get("ACC_TRDVAL")),
            "mktcap_krw": to_int(r.get("MKTCAP")), "listed_shares": to_int(r.get("LIST_SHRS")),
            "cmpprevdd_prc": to_float(r.get("CMPPREVDD_PRC")),
        }
        old = con.execute("SELECT * FROM price_daily WHERE date=? AND code=?", (day, code)).fetchone()
        if old is not None and old["is_provisional"]:
            # KIS 잠정값 -> KRX 확정값 교체. 차이를 기록한다 (14.2)
            for col in PRICE_COMPARE_COLS:
                if old[col] != new[col]:
                    db.record_change(con, "price_daily", f"{day}|{code}", col, old[col], new[col], ts,
                                     f"{old['source']} 잠정 -> KRX 확정")
            stats.replaced_provisional += 1
        con.execute(
            "INSERT OR REPLACE INTO price_daily(date, code, market, open, high, low, close, change_rate_pct,"
            " volume_sh, value_krw, mktcap_krw, listed_shares, cmpprevdd_prc, is_provisional, source, fetched_at, raw_file)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,'KRX',?,?)",
            (day, code, market, new["open"], new["high"], new["low"], new["close"], new["change_rate_pct"],
             new["volume_sh"], new["value_krw"], new["mktcap_krw"], new["listed_shares"], new["cmpprevdd_prc"], ts, raw))


def _save_base(con, day, market, rows, ts, raw, stats: KrxStats) -> None:
    for r in rows:
        code = str(r.get("ISU_SRT_CD") or "").strip()
        if not valid_code(code):
            stats.bad_codes += 1
            log.warning("KRX 종목기본정보 종목코드 형식 오류: %r (%s %s)", code, day, market)
            continue
        name = str(r.get("ISU_ABBRV") or r.get("ISU_NM") or "").strip()
        listed = str(r.get("LIST_DD") or "").strip() or None
        secugrp, sect, kind = (str(r.get(k) or "").strip() or None
                               for k in ("SECUGRP_NM", "SECT_TP_NM", "KIND_STKCERT_TP_NM"))
        con.execute(
            "INSERT OR REPLACE INTO krx_base_daily(date, code, market, name, listed_date, secugrp, sect_tp, kind,"
            " list_shares, source, fetched_at, raw_file) VALUES(?,?,?,?,?,?,?,?,?,'KRX',?,?)",
            (day, code, market, name, listed, secugrp, sect, kind, to_int(r.get("LIST_SHRS")), ts, raw))
        cur = con.execute("SELECT first_seen, last_seen FROM stock_master WHERE code=?", (code,)).fetchone()
        if cur is None:
            con.execute(
                "INSERT INTO stock_master(code, name, market, listed_date, krx_secugrp, krx_sect_tp, krx_kind,"
                " first_seen, last_seen, source, fetched_at, raw_file) VALUES(?,?,?,?,?,?,?,?,?,'KRX',?,?)",
                (code, name, market, listed, secugrp, sect, kind, day, day, ts, raw))
        else:
            first = min(cur["first_seen"] or day, day)
            last = max(cur["last_seen"] or day, day)
            if day >= (cur["last_seen"] or ""):  # 가장 최근 날짜의 정보로 갱신
                con.execute(
                    "UPDATE stock_master SET name=?, market=?, listed_date=?, krx_secugrp=?, krx_sect_tp=?, krx_kind=?,"
                    " first_seen=?, last_seen=?, fetched_at=?, raw_file=? WHERE code=?",
                    (name, market, listed, secugrp, sect, kind, first, last, ts, raw, code))
            else:
                con.execute("UPDATE stock_master SET first_seen=? WHERE code=?", (first, code))


def _save_index(con, day, market, rows, ts, raw) -> None:
    for r in rows:
        name = str(r.get("IDX_NM") or "").strip()
        if not name:
            continue
        old = con.execute("SELECT close, is_provisional, source FROM index_daily WHERE date=? AND series=? AND index_name=?",
                          (day, market, name)).fetchone()
        new_close = to_float(r.get("CLSPRC_IDX"))
        if old is not None and old["is_provisional"] and old["close"] != new_close:
            db.record_change(con, "index_daily", f"{day}|{market}|{name}", "close", old["close"], new_close, ts,
                             f"{old['source']} 잠정 -> KRX 확정")
        con.execute(
            "INSERT OR REPLACE INTO index_daily(date, series, index_name, index_class, open, high, low, close,"
            " change_rate_pct, volume_unconfirmed, value_unconfirmed, mktcap_unconfirmed, is_provisional,"
            " source, fetched_at, raw_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,'KRX',?,?)",
            (day, market, name, r.get("IDX_CLSS"), to_float(r.get("OPNPRC_IDX")), to_float(r.get("HGPRC_IDX")),
             to_float(r.get("LWPRC_IDX")), to_float(r.get("CLSPRC_IDX")), to_float(r.get("FLUC_RT")),
             to_int(r.get("ACC_TRDVOL")), to_int(r.get("ACC_TRDVAL")), to_int(r.get("MKTCAP")), ts, raw))


def backfill_cmpprevdd(con, root) -> int:
    """0.6.0 이전에 받은 KRX 행의 CMPPREVDD_PRC를 저장해 둔 원본(raw)에서 채운다. 한 번만 실행."""
    import gzip
    import json

    if con.execute("SELECT value FROM meta WHERE key='backfill_cmpprevdd'").fetchone():
        return 0
    files = [r[0] for r in con.execute(
        "SELECT DISTINCT raw_file FROM price_daily WHERE source='KRX' AND cmpprevdd_prc IS NULL AND raw_file IS NOT NULL")]
    n = 0
    for rel in files:
        path = root.joinpath(*rel.split("/"))
        if not path.exists():
            continue
        try:
            body = json.loads(gzip.open(path, "rb").read().decode("utf-8"))
        except (OSError, ValueError):
            continue
        day = str((body.get(ROWS_KEY) or [{}])[0].get("BAS_DD") or "")
        with db.Tx(con):
            for r in body.get(ROWS_KEY) or []:
                v = to_float(r.get("CMPPREVDD_PRC"))
                if v is None:
                    continue
                cur = con.execute("UPDATE price_daily SET cmpprevdd_prc=? WHERE raw_file=? AND code=?",
                                  (v, rel, str(r.get("ISU_CD") or "").strip()))
                n += cur.rowcount
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('backfill_cmpprevdd', ?)", (str(n),))
    return n


def base_price_adjust_days(con, start: str, end: str, codes=None) -> dict[str, list[str]]:
    """기준가가 조정된 날: KRX 종가 − CMPPREVDD_PRC ≠ 직전 거래일 KRX 종가 (액면분할·병합·무상증자 등).
    상장주식수 변화(CB 전환·자사주 소각·스톡옵션)는 쓰지 않는다. 판단할 값이 없으면 판정하지 않는다."""
    days = [r[0] for r in con.execute(
        "SELECT date FROM trading_calendar WHERE is_open=1 AND date BETWEEN ? AND ? ORDER BY date", (start, end))]
    prev_of = {days[i]: days[i - 1] for i in range(1, len(days))}
    out: dict[str, list[str]] = {}
    closes: dict[tuple[str, str], float] = {}
    rows = con.execute("SELECT code, date, close, cmpprevdd_prc FROM price_daily WHERE source='KRX' AND is_provisional=0"
                       " AND date BETWEEN ? AND ? ORDER BY code, date", (start, end)).fetchall()
    for r in rows:
        if r["close"] is not None:
            closes[(r["code"], r["date"])] = r["close"]
    keep = set(codes) if codes is not None else None
    for r in rows:
        if keep is not None and r["code"] not in keep:
            continue
        pd = prev_of.get(r["date"])
        prev_close = closes.get((r["code"], pd)) if pd else None
        if r["close"] is None or r["cmpprevdd_prc"] is None or prev_close is None:
            continue
        if abs((r["close"] - r["cmpprevdd_prc"]) - prev_close) > 0.5:
            out.setdefault(r["code"], []).append(r["date"])
    return out
