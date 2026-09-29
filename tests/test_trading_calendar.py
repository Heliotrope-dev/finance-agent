import datetime as dt

import pytest

import trading_calendar


@pytest.mark.parametrize(
    ("market", "day", "expected"),
    [
        ("HK", dt.date(2026, 9, 25), True),
        ("HK", dt.date(2026, 10, 1), False),
        ("US", dt.date(2026, 9, 7), False),
        ("US", dt.date(2026, 9, 8), True),
        ("US", dt.date(2026, 9, 6), False),
    ],
)
def test_is_trading_day(market, day, expected):
    assert trading_calendar.is_trading_day(market, day) is expected


def test_unknown_year_fails_closed():
    assert trading_calendar.is_trading_day("HK", dt.date(2028, 1, 4)) is False


def test_2027_is_covered():
    assert trading_calendar.is_trading_day("US", dt.date(2027, 1, 4)) is True
    assert trading_calendar.is_trading_day("US", dt.date(2027, 7, 5)) is False
    assert trading_calendar.is_trading_day("HK", dt.date(2027, 2, 8)) is False


def test_coverage_warnings_flag_missing_or_provisional_years():
    w = trading_calendar.coverage_warnings(dt.date(2026, 11, 2))
    assert any("HK 2027" in x and "暂定" in x for x in w)
    assert not any("US 2027" in x for x in w)
    assert any("2028" in x for x in trading_calendar.coverage_warnings(dt.date(2027, 11, 2)))


def test_unknown_market_rejected():
    with pytest.raises(ValueError):
        trading_calendar.is_trading_day("CN", dt.date(2026, 9, 25))
