"""Scheduling helpers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    match = _TIME_RE.match(value.strip())
    if not match:
        raise ValueError(f"invalid time '{value}', expected HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time '{value}'")
    return hour, minute


def next_daily_run(now: datetime, hour: int, minute: int) -> datetime:
    """The next occurrence of hour:minute (strictly in the future)."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target
