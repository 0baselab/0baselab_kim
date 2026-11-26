"""backtest coverage | run [A|B|C|D|all] | sync | export  (--db <경로>). API 키가 필요 없고 잠금 파일을 잡지 않는다."""
from __future__ import annotations

from pathlib import Path

from .. import paths
from ..settings import load_settings, set_setting
from ..timeutil import now_kst


def resolve_db(install: Path, settings: dict, db_arg: str | None) -> Path:
    if db_arg:
        p = Path(db_arg).expanduser()
        set_setting(install, "BACKTEST_DB_PATH", str(p))  # 고른 경로를 기억
        return p
    saved = (settings.get("BACKTEST_DB_PATH") or "").strip()
    if saved:
        return Path(saved).expanduser()
    return paths.data_dir(settings) / "backtest" / "backtest.sqlite"


def themes_csv_for(db: Path, settings: dict) -> Path:
    p = paths.data_dir(settings) / "themes" / "theme_master.csv"
    return p if p.exists() else db.parent.parent / "themes" / "theme_master.csv"


def main(sub: str | None, variant: str | None, db_arg: str | None, out=print) -> int:
    from . import config as C
    from .data import coverage, coverage_lines, open_ro
    install = paths.install_dir()
    settings = load_settings(install)
    db = resolve_db(install, settings, db_arg)
    if sub not in ("coverage", "run", "sync", "export"):
        out("사용법: backtest coverage | run [A|B|C|D|all] | sync | export  [--db <경로>]")
        return 1
    if sub in ("sync", "export"):
        from .sync import export, sync
        if not db.exists():
            out(f"백테스트 DB가 없습니다: {db}")
            return 1
        root = paths.data_dir(settings)
        lines, rep = sync(db, root / "db" / "market.sqlite", (root if root.exists() else db.parent) / "system" / "verify", now_kst())
        out("\n".join(lines))
        if rep:
            out(f"보고서: {rep}")
        if sub == "export":
            z = export(db, paths.desktop_dir(), now_kst())
            out(f"내보내기: {z}")
        return 0
    if not db.exists():
        out(f"백테스트 DB가 없습니다: {db}\n--db <경로>로 지정하거나 backfill krx를 먼저 실행하세요.")
        return 1
    con = open_ro(db)
    try:
        cov = coverage(con)
    finally:
        con.close()
    out("\n".join(coverage_lines(cov)))
    if sub == "coverage":
        return 0
    from .run import run_backtest
    v = (variant or "all").upper()
    variants = ["A", "B", "C", "D"] if v == "ALL" else [x for x in v.split(",") if x in "ABCD" and x]
    if not variants:
        out("변형은 A, B, C, D, all 중 하나입니다.")
        return 1
    cfg = C.load(install)
    res = run_backtest(db, cfg, variants, now_kst(), progress=lambda s, x: out(f"  [{x * 100:3.0f}%] {s}"),
                       themes_csv=themes_csv_for(db, settings))
    out(f"\n결과: {res}\n최신 결과: {res.parent / 'latest'}")
    return 0
