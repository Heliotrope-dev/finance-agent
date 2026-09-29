"""Local exchange-calendar guard for the currently supported markets.

The calendar belongs to Invest Agent so scheduled work has no dependency on an
external workspace. Holiday tables must be extended before each new year;
unknown years fail closed for scheduled market work.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

_BJ = ZoneInfo("Asia/Shanghai")

HK_HOLIDAYS: dict[int, frozenset[dt.date]] = {
    2026: frozenset({
        dt.date(2026, 1, 1),
        dt.date(2026, 2, 17), dt.date(2026, 2, 18), dt.date(2026, 2, 19),
        dt.date(2026, 4, 3), dt.date(2026, 4, 6), dt.date(2026, 4, 7),
        dt.date(2026, 5, 1), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
        dt.date(2026, 7, 1), dt.date(2026, 10, 1), dt.date(2026, 10, 19),
        dt.date(2026, 12, 25),
    }),
}

US_HOLIDAYS: dict[int, frozenset[dt.date]] = {
    2026: frozenset({
        dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
        dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
        dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
    }),
}


def today_bj() -> dt.date:
    return dt.datetime.now(_BJ).date()


def is_trading_day(market: str, day: dt.date | None = None) -> bool:
    """Return whether ``day`` is a normal supported-market trading day."""
    day = day or today_bj()
    market = market.upper()
    if market not in {"HK", "US"}:
        raise ValueError(f"unsupported market: {market!r}")
    if day.weekday() >= 5:
        return False
    holidays = HK_HOLIDAYS if market == "HK" else US_HOLIDAYS
    return day.year in holidays and day not in holidays[day.year]
