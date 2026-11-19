"""테마 마스터 CSV (명세서 10장). 프로그램은 읽기만 한다. 테마를 만들거나 바꾸지 않는다."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .timeutil import stamp

HEADER = ["theme", "code", "name", "basis", "added_date", "note"]
REQUIRED = ["theme", "code", "basis", "added_date"]
CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


@dataclass
class ThemeLoad:
    version: int | None = None
    exists: bool = False
    members: dict[str, list[str]] = field(default_factory=dict)  # code -> [theme]
    themes: dict[str, list[str]] = field(default_factory=dict)  # theme -> [code]
    problems: list[tuple[int, str, str]] = field(default_factory=list)  # (줄 번호, 내용, 문제)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load(con, themes_dir: Path, now: datetime, master_codes: set[str], universe_codes: set[str]) -> ThemeLoad:
    path = themes_dir / "theme_master.csv"
    out = ThemeLoad()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(HEADER) + "\n", encoding="utf-8-sig")
        return out
    out.exists = True
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows = list(csv.DictReader(io.StringIO(_read_text(path))))
    last = con.execute("SELECT version, file_hash FROM theme_versions ORDER BY version DESC LIMIT 1").fetchone()
    if last and last["file_hash"] == digest:
        out.version = last["version"]
    else:
        out.version = (last["version"] + 1) if last else 1
        hist = themes_dir / "history" / f"theme_master_v{out.version}_{stamp(now)}.csv"
        hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, hist)
        con.execute("BEGIN")
        con.execute("INSERT INTO theme_versions(version, loaded_at, file_hash, row_count, history_file) VALUES(?,?,?,?,?)",
                    (out.version, now.isoformat(timespec="seconds"), digest, len(rows), hist.name))
        seen_db = set()
        for r in rows:
            t, c = (r.get("theme") or "").strip(), (r.get("code") or "").strip()
            if t and CODE_RE.match(c) and (t, c) not in seen_db:
                seen_db.add((t, c))
                con.execute("INSERT INTO theme_membership VALUES(?,?,?,?,?,?)",
                            (out.version, t, c, (r.get("added_date") or "").strip(), (r.get("basis") or "").strip(),
                             (r.get("note") or "").strip()))
        con.execute("COMMIT")
    seen = set()
    for i, r in enumerate(rows, start=2):  # 1줄은 머리글
        t, c = (r.get("theme") or "").strip(), (r.get("code") or "").strip()
        desc = f"{t},{c}"
        blank = [k for k in REQUIRED if not (r.get(k) or "").strip()]
        if blank:
            out.problems.append((i, desc, "빈 필수 칸: " + ", ".join(blank)))
            continue
        if not CODE_RE.match(c):
            out.problems.append((i, desc, "종목코드 형식 오류 (영문 대문자·숫자 6자리. 엑셀이 앞의 0을 지웠는지 확인)"))
            continue
        if (t, c) in seen:
            out.problems.append((i, desc, "같은 (theme, code) 중복"))
            continue
        seen.add((t, c))
        if c not in master_codes:
            out.problems.append((i, desc, "없는 종목코드"))
            continue
        if c not in universe_codes:
            out.problems.append((i, desc, "분석 대상에서 제외된 종목"))
            continue
        out.members.setdefault(c, []).append(t)
        out.themes.setdefault(t, []).append(c)
    return out
