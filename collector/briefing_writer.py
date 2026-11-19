"""브리핑 패키지 파일 쓰기: xlsx(필수), JSON(선택). 파일명은 기준일 + 생성 시각 (명세서 3.3)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from .briefing import Package
from .timeutil import stamp


def _clean(rows):
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def _sheet(wb, title, rows, empty_note="해당 없음"):
    ws = wb.create_sheet(title)
    rows = _clean(rows)
    if not rows:
        ws.append([empty_note])
        return
    cols = list(rows[0].keys())
    ws.append(cols)
    for r in rows:
        ws.append([r.get(c) for c in cols])


def write(pkg: Package, out_dir: Path, now: datetime, with_json: bool = False) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"briefing_{pkg.date}_{stamp(now)}"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("0_안내")
    ws.append(["항목", "값"])
    for k, v in pkg.info:
        ws.append([k, v])
    _sheet(wb, "1_시장", pkg.market)
    _sheet(wb, "2_지수시리즈", pkg.index_series)
    if pkg.themes is not None:
        _sheet(wb, "3_테마", pkg.themes, "테마 CSV에 유효한 줄이 없음")
    _sheet(wb, "4_종목", pkg.stocks)
    _sheet(wb, "5_미분류", pkg.unclassified)
    _sheet(wb, "6_데이터상태", pkg.status)
    paths = [out_dir / f"{base}.xlsx"]
    wb.save(paths[0])
    if with_json:
        data = {"info": dict(pkg.info), "market": _clean(pkg.market), "index_series": _clean(pkg.index_series),
                "themes": pkg.themes, "stocks": _clean(pkg.stocks), "unclassified": _clean(pkg.unclassified),
                "data_status": pkg.status}
        paths.append(out_dir / f"{base}.json")
        paths[1].write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return paths


def record_unclassified(con, pkg: Package) -> None:
    """10.3 미분류 반복 등장 집계용 기록."""
    con.execute("BEGIN")
    con.execute("DELETE FROM briefing_unclassified WHERE date=?", (pkg.date,))
    for r in pkg.unclassified:
        for label in r["selected_by"].split("; "):
            con.execute("INSERT OR REPLACE INTO briefing_unclassified VALUES(?,?,?,?)",
                        (pkg.date, r["code"], label, pkg.generated_at))
    con.execute("COMMIT")


def export_unclassified_repeats(con, out_dir: Path, now: datetime, n_days: int = 20) -> Path:
    """최근 n거래일 동안 5_미분류에 오른 횟수를 종목별로 센 CSV (10.3)."""
    import csv
    days = [r[0] for r in con.execute("SELECT DISTINCT date FROM briefing_unclassified ORDER BY date DESC LIMIT ?", (n_days,))]
    path = out_dir / f"unclassified_repeats_{days[-1] if days else 'none'}-{days[0] if days else 'none'}_{stamp(now)}.csv"
    rows = con.execute(
        "SELECT b.code, s.name, COUNT(DISTINCT b.date) AS days, GROUP_CONCAT(DISTINCT b.list_name) AS lists,"
        " MIN(b.date) AS first, MAX(b.date) AS last FROM briefing_unclassified b LEFT JOIN stock_master s ON s.code=b.code"
        f" WHERE b.date IN ({','.join('?' * len(days)) or 'NULL'}) GROUP BY b.code ORDER BY days DESC, b.code", days).fetchall()
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "days_listed", "lists", "first_date", "last_date"])
        for r in rows:
            w.writerow(list(r))
    return path
