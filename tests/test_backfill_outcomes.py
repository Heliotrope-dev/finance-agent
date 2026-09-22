from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

import backfill_outcomes
import tracker


@pytest.fixture()
def isolated_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "track_record.db")
    monkeypatch.setattr(tracker, "_db_initialized", False)
    tracker.init_db()
    yield
    tracker._db_initialized = False


def test_schema_and_log_advice_preserve_risk_metadata(isolated_tracker):
    text = """目标价：HK$130.50（较现价上涨）
止损位：HK$95.25
置信度：高
维度打分：基本面18/22 · 价格位置15/20 · 技术面16/20 · 筹码面15/20 · 分析师预期6/8 · 数据确定性8/10
"""
    advice_id = tracker.log_advice(
        "user@example.com", "00700", 100, text, "测试", market="HK", score=78
    )

    with tracker._conn() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(advice)")}
        row = connection.execute(
            "SELECT stop_loss, target_price, confidence FROM advice WHERE id = ?", (advice_id,)
        ).fetchone()

    assert {
        "review_price_1d", "review_price_5d", "review_price_20d", "review_price_60d",
        "mfe_pct_20d", "mae_pct_20d", "stop_hit_day",
        "bench_ret_1d", "bench_ret_5d", "bench_ret_20d", "bench_ret_60d",
        "stop_loss", "target_price", "confidence",
    } <= columns
    assert row == (95.25, 130.5, "高")


def _bars(start: date, count: int, *, base: float = 100) -> pd.DataFrame:
    dates = [start + timedelta(days=index) for index in range(count)]
    closes = [base + index for index in range(count)]
    return pd.DataFrame({
        "日期": dates,
        "收盘": closes,
        "最高": [value + 2 for value in closes],
        "最低": [value - 3 for value in closes],
    })


def test_calculate_outcomes_requires_mature_windows():
    row = {
        "created_at": "2026-01-01T09:00:00+00:00",
        "price_at_advice": 100,
        "fundamental_verdict": "止损位：95\n目标价：130\n置信度：中",
        "stop_loss": 95,
    }
    history = _bars(date(2026, 1, 2), 5)
    benchmark = _bars(date(2026, 1, 1), 6, base=200)

    result = backfill_outcomes.calculate_outcomes(row, history, benchmark)

    assert result["review_price_1d"] == 100
    assert result["review_price_5d"] == 104
    assert "review_price_20d" not in result
    assert "mfe_pct_20d" not in result
    assert result["bench_ret_5d"] == pytest.approx(2.5)
    assert result["confidence"] == "中"


def test_calculate_outcomes_records_path_and_first_stop_hit():
    row = {
        "created_at": "2026-01-01T09:00:00+00:00",
        "price_at_advice": 100,
        "fundamental_verdict": "止损位：98\n置信度：低",
        "stop_loss": 98,
    }
    history = _bars(date(2026, 1, 2), 60)
    benchmark = _bars(date(2026, 1, 1), 61, base=200)

    result = backfill_outcomes.calculate_outcomes(row, history, benchmark)

    assert result["review_price_60d"] == 159
    assert result["mfe_pct_20d"] == pytest.approx(21)
    assert result["mae_pct_20d"] == pytest.approx(-3)
    assert result["stop_hit_day"] == 1
    assert result["bench_ret_60d"] == pytest.approx(30)


def test_record_outcomes_only_fills_null_cells(isolated_tracker):
    advice_id = tracker.log_advice(
        "user@example.com", "AAPL", 100, "置信度：低", "测试", market="US"
    )
    tracker.record_advice_outcomes(advice_id, {"review_price_1d": 101, "confidence": "高"})
    tracker.record_advice_outcomes(advice_id, {"review_price_1d": 999, "review_price_5d": 105})

    with tracker._conn() as connection:
        row = connection.execute(
            "SELECT review_price_1d, review_price_5d, confidence FROM advice WHERE id = ?",
            (advice_id,),
        ).fetchone()
    assert row == (101, 105, "低")
