from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

import tracker

CN = timezone(timedelta(hours=8))
EMAIL = "t@example.com"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(tracker, "_db_initialized", False)
    tracker.init_db()

    def add(ts_cn: str, value: float):
        dt = datetime.fromisoformat(ts_cn).replace(tzinfo=CN).astimezone(timezone.utc)
        with closing(tracker._conn()) as c:
            c.execute(
                "INSERT INTO sim_equity_snapshots (email, snapshot_at, holdings_value_hkd, virtual_cash_hkd, net_value_hkd)"
                " VALUES (?, ?, ?, 0, ?)", (EMAIL, dt.isoformat(), value, value),
            )
            c.commit()
    return add


def test_us_session_across_midnight_counts_as_one_day(db):
    # 2026-10-02(五) 美股收盘后的净值 1000；10-05(一) 09:30 快照仍是 1000
    db("2026-09-30 10:00", 990)
    db("2026-10-02 03:55", 1000)
    db("2026-10-05 09:30", 1000)
    db("2026-10-05 23:55", 990)   # 美股盘中跌，过零点后又涨回
    db("2026-10-06 03:55", 1009)
    db("2026-10-06 09:30", 1010)  # 10-06 开盘前 = 10-05 美股收盘

    r = tracker.get_period_pnl(EMAIL, 1010, now=datetime(2026, 10, 6, 11, 0, tzinfo=CN))
    assert r["today"]["change"] == 0          # 今天港美都还没动
    assert r["yesterday"]["change"] == 10     # 10-05 美股收盘对收盘，含最后几分钟
    assert r["month"]["change"] == 10         # 上月末收盘 1000（10-01 第一条快照前的那天）


def test_monday_yesterday_is_friday(db):
    db("2026-10-02 09:30", 1000)
    db("2026-10-03 03:55", 1020)
    db("2026-10-05 09:30", 1020)
    r = tracker.get_period_pnl(EMAIL, 1020, now=datetime(2026, 10, 5, 12, 0, tzinfo=CN))
    assert r["yesterday"]["change"] == 20


def test_before_first_snapshot_of_day_today_is_none(db):
    db("2026-10-05 09:30", 1000)
    r = tracker.get_period_pnl(EMAIL, 1000, now=datetime(2026, 10, 6, 8, 30, tzinfo=CN))
    assert r["today"] is None
    assert r["yesterday"]["change"] == 0
    assert r["month"] is None  # 本月就是有记录的第一个月
