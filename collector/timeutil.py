"""모든 날짜·시각은 PC 설정과 무관하게 Asia/Seoul 기준 (명세서 2.3)."""
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


def stamp(dt: datetime) -> str:
    """파일명용 생성 시각 YYYYMMDD-HHMMSS (명세서 3.3)."""
    return dt.astimezone(KST).strftime("%Y%m%d-%H%M%S")


def ymd(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%Y%m%d")
