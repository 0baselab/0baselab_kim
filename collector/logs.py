"""실행 로그 (명세서 11.3). 키·토큰은 redact()로 지운 뒤 기록한다."""
from __future__ import annotations

import logging
from pathlib import Path

from .secrets import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path, stamp: str) -> Path:
    path = log_dir / f"run_{stamp}.log"
    logger = logging.getLogger("collector")
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    # httpx는 요청 URL을 INFO로 남기므로 (DART는 URL에 키가 들어감) 끈다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path
