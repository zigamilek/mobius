from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def timestamp_context_line(timezone_name: str) -> str:
    now = datetime.now(ZoneInfo(timezone_name))
    return (
        f"Current timestamp: {now.isoformat()} ({timezone_name}). "
        "Use this as the authoritative current date and time for this request."
    )
