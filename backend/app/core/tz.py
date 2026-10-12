"""北京时间（UTC+8 固定偏移，无夏令时）。仅展示层使用；存储与计算一律 UTC。"""
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def to_bj(ts: datetime) -> datetime:
    """转为北京时间；naive 输入按库内 UTC 约定处理。"""
    return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(CST)
