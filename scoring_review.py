"""Deterministic endpoint for scoring-v2 outcome checks."""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

import trading_calendar

POLICY = "session-close-v1"
TIMEZONES = {
    "A": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "US": ZoneInfo("America/New_York"),
}
CLOSE_TIMES = {"A": dt.time(15, 0), "HK": dt.time(16, 0), "US": dt.time(16, 0)}


def fifth_session_close(market: str, created_at: str) -> dt.datetime | None:
    """Return the fifth exchange session close after the decision's local date.

    Unknown exchange-calendar years fail closed. The decision day is never counted.
    """
    market = market.upper()
    zone = TIMEZONES.get(market)
    if zone is None:
        return None
    try:
        created = dt.datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        day = created.astimezone(zone).date()
    except (TypeError, ValueError):
        return None

    count = 0
    for _ in range(20):
        day += dt.timedelta(days=1)
        if day.year in trading_calendar.PROVISIONAL_YEARS.get(market, ()):
            return None
        try:
            open_day = trading_calendar.is_trading_day(market, day)
        except (ValueError, KeyError):
            return None
        if not open_day:
            # The calendar module returns False for missing years; do not walk past
            # its known coverage and invent a weekend/holiday schedule.
            holidays = {"A": trading_calendar.A_HOLIDAYS, "HK": trading_calendar.HK_HOLIDAYS,
                        "US": trading_calendar.US_HOLIDAYS}[market]
            if day.year not in holidays:
                return None
            continue
        count += 1
        if count == 5:
            return dt.datetime.combine(day, CLOSE_TIMES[market], tzinfo=zone)
    return None


def exact_close(frame, target: dt.datetime, now: dt.datetime | None = None) -> float | None:
    """Return the close only when the exact endpoint bar is present and finished."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.astimezone(dt.timezone.utc) < target.astimezone(dt.timezone.utc):
        return None
    if frame is None or getattr(frame, "empty", True):
        return None
    date_column = "日期" if "日期" in frame.columns else "date" if "date" in frame.columns else None
    close_column = "收盘" if "收盘" in frame.columns else "close" if "close" in frame.columns else None
    if not date_column or not close_column:
        return None
    target_day = target.date().isoformat()
    rows = frame[frame[date_column].astype(str).str[:10] == target_day]
    if rows.empty:
        return None
    try:
        value = float(rows.iloc[-1][close_column])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None
