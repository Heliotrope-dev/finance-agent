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
    # 2027: PROVISIONAL (generated 2026-09-29 from pandas_market_calendars' HKEX calendar, which missed the
    # 2026-04-07 substitute holiday, so treat as unverified). Replace with HKEX's official 2027 list when published
    # and remove 2027 from PROVISIONAL_YEARS["HK"].
    2027: frozenset({
        dt.date(2027, 1, 1), dt.date(2027, 2, 8), dt.date(2027, 2, 9),
        dt.date(2027, 3, 26), dt.date(2027, 3, 29), dt.date(2027, 4, 5),
        dt.date(2027, 5, 13), dt.date(2027, 6, 9), dt.date(2027, 7, 1),
        dt.date(2027, 9, 16), dt.date(2027, 10, 1), dt.date(2027, 10, 8),
        dt.date(2027, 12, 27),
    }),
}

# Shanghai and Shenzhen exchanges share mainland A-share closure dates.
A_HOLIDAYS: dict[int, frozenset[dt.date]] = {
    2026: frozenset({
        dt.date(2026, 1, 1), dt.date(2026, 1, 2),
        dt.date(2026, 2, 16), dt.date(2026, 2, 17), dt.date(2026, 2, 18),
        dt.date(2026, 2, 19), dt.date(2026, 2, 20), dt.date(2026, 2, 23),
        dt.date(2026, 4, 6),
        dt.date(2026, 5, 1), dt.date(2026, 5, 4), dt.date(2026, 5, 5),
        dt.date(2026, 6, 19), dt.date(2026, 9, 25),
        dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 5),
        dt.date(2026, 10, 6), dt.date(2026, 10, 7),
    }),
}

US_HOLIDAYS: dict[int, frozenset[dt.date]] = {
    2026: frozenset({
        dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
        dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
        dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
        dt.date(2026, 12, 25),
    }),
    # 2027 from pandas_market_calendars' NYSE calendar (its 2026 output matches the table above exactly)
    2027: frozenset({
        dt.date(2027, 1, 1), dt.date(2027, 1, 18), dt.date(2027, 2, 15),
        dt.date(2027, 3, 26), dt.date(2027, 5, 31), dt.date(2027, 6, 18),
        dt.date(2027, 7, 5), dt.date(2027, 9, 6), dt.date(2027, 11, 25),
        dt.date(2027, 12, 24),
    }),
}

# years whose table is not yet confirmed against the exchange's official list
PROVISIONAL_YEARS: dict[str, frozenset[int]] = {
    "HK": frozenset({2027}), "US": frozenset(), "A": frozenset(),
}


def today_bj() -> dt.date:
    return dt.datetime.now(_BJ).date()


def is_trading_day(market: str, day: dt.date | None = None) -> bool:
    """Return whether ``day`` is a normal supported-market trading day."""
    day = day or today_bj()
    market = market.upper()
    if market not in {"A", "HK", "US"}:
        raise ValueError(f"unsupported market: {market!r}")
    if day.weekday() >= 5:
        return False
    holidays = {"A": A_HOLIDAYS, "HK": HK_HOLIDAYS, "US": US_HOLIDAYS}[market]
    return day.year in holidays and day not in holidays[day.year]


def coverage_warnings(today: dt.date | None = None) -> list[str]:
    """Warnings to surface (Codex audit #19): from November on, next year's table must exist and be confirmed,
    otherwise every scheduled job would silently skip from January 1st (unknown years fail closed)."""
    today = today or today_bj()
    out = []
    for market, table in (("A", A_HOLIDAYS), ("HK", HK_HOLIDAYS), ("US", US_HOLIDAYS)):
        for year in (today.year, today.year + 1) if today.month >= 11 else (today.year,):
            if year not in table:
                out.append(f"{market} {year} 年休市日表缺失，届时所有定时任务会被跳过")
            elif year in PROVISIONAL_YEARS.get(market, ()):
                out.append(f"{market} {year} 年休市日表为暂定，需按交易所正式公布的名单核对")
    return out
