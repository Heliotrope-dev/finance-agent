from __future__ import annotations

import advisor
import pandas as pd


def test_calibration_uses_current_market_scheme_and_marks_small_samples(monkeypatch):
    seen = {}

    def fake_backtest(**kwargs):
        seen.update(kwargs)
        return {
            "current_scheme": "基本面22·价格位置20",
            "bands": [
                {"band": "70-89", "count": 40, "avg_excess_pct": 1.25,
                 "excess_win_rate_pct": 60},
                {"band": "50-69", "count": 8, "avg_excess_pct": None,
                 "excess_win_rate_pct": None},
            ],
        }

    monkeypatch.setattr(advisor.tracker, "get_score_band_backtest", fake_backtest)
    block = advisor._calibration_block("US", "screen")

    assert seen == {
        "source": "screen", "min_sample": 30, "market": "US",
        "current_scheme_only": True,
    }
    assert "平均五日超额+1.2%" in block
    assert "样本8（样本不足，不作结论）" in block
    assert "当前权重口径" in block


def test_deterministic_scores_are_observation_only_numeric_rules(monkeypatch):
    history = pd.DataFrame({
        "日期": pd.date_range("2026-01-01", periods=40),
        "收盘": [100 + index for index in range(40)],
        "成交量": [100] * 39 + [250],
    })
    monkeypatch.setattr(advisor.ds, "get_stock_history", lambda *args, **kwargs: history)
    result = advisor._deterministic_observation_scores("TEST", "US")
    # The series is strongly rising but more than 3% above MA20, so both rules
    # deliberately withhold the near-average points instead of rewarding chase.
    assert result == {"technical": 17, "price_position": 15}
