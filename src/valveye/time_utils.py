from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _last_sunday(year: int, month: int) -> datetime:
    # 返回该月最后一个周日（UTC 00:00）
    if month == 12:
        first_next = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        first_next = datetime(year, month + 1, 1, tzinfo=UTC)
    day = first_next - timedelta(days=1)
    while day.weekday() != 6:
        day -= timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def is_summer_time(now_utc: datetime) -> bool:
    """
    夏令时窗口：每年 3 月最后一个周日 01:00 UTC 到 10 月最后一个周日 01:00 UTC。
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    start = _last_sunday(now_utc.year, 3).replace(hour=1)
    end = _last_sunday(now_utc.year, 10).replace(hour=1)
    return start <= now_utc < end


def runtime_offset_hours(now_utc: datetime) -> int:
    # 需求确认：冬令时 UTC+3，夏令时 UTC+4
    return 4 if is_summer_time(now_utc) else 3


def local_hhmm(now_utc: datetime) -> tuple[int, int, str]:
    offset = runtime_offset_hours(now_utc)
    local_now = now_utc + timedelta(hours=offset)
    return local_now.hour, local_now.minute, local_now.date().isoformat()
