"""실행 준비: 설정 → 폴더 → 로그 → 키 → DB(생성·버전·무결성) → 잠금 (명세서 2.3)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db, paths
from .http import ApiClient
from .kis import KisClient
from .krx import KrxClient
from .lock import Lock
from .logs import setup_logging
from .secrets import ensure_keys
from .settings import load_settings
from .timeutil import now_kst, stamp


@dataclass
class App:
    install: Path
    settings: dict
    root: Path
    dirs: dict[str, Path]
    keys: dict
    con: object
    lock: Lock
    log_path: Path
    started: datetime
    now: Callable[[], datetime]
    http_factory: Callable[[str, float], ApiClient]

    def kis(self) -> KisClient | None:
        if not (self.keys.get("KIS_APP_KEY") and self.keys.get("KIS_APP_SECRET")):
            return None
        if not hasattr(self, "_kis"):
            api = self.http_factory("KIS", float(self.settings.get("KIS_CALLS_PER_SEC") or 10))
            self._kis = KisClient(api, self.settings["KIS_BASE_URL"], self.keys["KIS_APP_KEY"],
                                  self.keys["KIS_APP_SECRET"], self.dirs["system"] / "kis_token.json", now=self.now)
        return self._kis

    def krx(self) -> KrxClient | None:
        if not self.keys.get("KRX_AUTH_KEY"):
            return None
        if not hasattr(self, "_krx"):
            api = self.http_factory("KRX", float(self.settings.get("KRX_CALLS_PER_SEC") or 2))
            self._krx = KrxClient(api, self.settings["KRX_BASE_URL"], self.keys["KRX_AUTH_KEY"])
        return self._krx

    def close(self) -> None:
        try:
            self.con.close()
        finally:
            self.lock.release()


def start(*, install: Path | None = None, now: Callable[[], datetime] = now_kst,
          http_factory: Callable[[str, float], ApiClient] | None = None,
          ask_key=None, out: Callable[[str], None] = print) -> App:
    install = install or paths.install_dir()
    settings = load_settings(install)
    root = paths.data_dir(settings)
    if paths.is_onedrive(root):
        out("경고: 데이터 폴더가 OneDrive 동기화 폴더 안에 있습니다. 실행 중인 DB가 손상될 수 있으니 "
            "config/settings.env의 DATA_DIR에 동기화되지 않는 경로를 지정하세요. (백업은 backup/ 스냅샷으로 대신합니다)")
    dirs = paths.ensure_data_dirs(root)
    started = now()
    log_path = setup_logging(dirs["logs"], stamp(started))
    keys = ensure_keys(install, **({"ask": ask_key} if ask_key else {}), out=out)
    lock = Lock(dirs["system"] / "collector.lock")
    lock.acquire()
    try:
        con = db.connect(dirs["db"] / "market.sqlite")
        db.init_db(con)
    except Exception:
        lock.release()
        raise
    return App(install=install, settings=settings, root=root, dirs=dirs, keys=keys, con=con, lock=lock,
               log_path=log_path, started=started, now=now,
               http_factory=http_factory or (lambda name, rate: ApiClient(name, rate)))
