import datetime as dt

import pandas as pd

import scoring
import scoring_review


def test_fifth_us_session_is_after_decision_day_and_uses_exchange_timezone():
    target = scoring_review.fifth_session_close("US", "2026-10-02T21:00:00+00:00")
    assert target == dt.datetime(2026, 10, 9, 16, 0,
                                 tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def test_review_requires_exact_completed_target_close():
    target = scoring_review.fifth_session_close("US", "2026-10-02T21:00:00+00:00")
    frame = pd.DataFrame({"日期": ["2026-10-08"], "收盘": [101.0]})
    assert scoring_review.exact_close(frame, target, target + dt.timedelta(minutes=1)) is None

    frame = pd.DataFrame({"日期": ["2026-10-09"], "收盘": [101.0]})
    assert scoring_review.exact_close(frame, target, target - dt.timedelta(minutes=1)) is None
    assert scoring_review.exact_close(frame, target, target + dt.timedelta(minutes=1)) == 101.0


def test_unknown_market_calendar_fails_closed():
    assert scoring_review.fifth_session_close("CC", "2026-10-02T21:00:00+00:00") is None


def test_a_share_fifth_session_skips_national_day_holiday():
    from zoneinfo import ZoneInfo

    target = scoring_review.fifth_session_close("A", "2026-09-30T07:00:00+00:00")
    assert target == dt.datetime(2026, 10, 14, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_invalid_dimensions_never_keep_buy_action():
    invalid = (
        "结论：买入\n维度打分：基本面18/22 · 价格位置15/20 · 技术面15/20 "
        "· 筹码面14/20 · 分析师5/8"
    )
    result = scoring.finalize(invalid, "equity", "买入")
    assert result["action"] == "观望"
    assert result["score"] is None
    assert "只作观察" in result["fundamental_verdict"]


def test_tracker_stores_only_valid_score_and_latest_invalid_hides_old_valid(tmp_path, monkeypatch):
    import tracker

    monkeypatch.setattr(tracker, "_DB_PATH", tmp_path / "track_record.db")
    monkeypatch.setattr(tracker, "_db_initialized", False)
    valid = scoring.finalize(
        "结论：观望\n维度打分：基本面18/22 · 价格位置15/20 · 技术面15/20 "
        "· 筹码面14/20 · 分析师5/8 · 数据确定性8/10",
        "equity", "观望",
    )
    assert valid["score_valid"] and valid["score"] == 75
    tracker.log_advice(
        "test@example.com", "0700", 100, valid["fundamental_verdict"], "",
        action=valid["action"], market="HK", name="Test", source="watchlist_hk",
    )
    invalid = scoring.finalize("结论：买入\n维度打分：缺项", "equity", "买入")
    tracker.log_advice(
        "test@example.com", "0700", 101, invalid["fundamental_verdict"], "",
        action=invalid["action"], market="HK", name="Test", source="watchlist_hk",
    )
    board = tracker.get_latest_leaderboard(source="watchlist_hk", asset_kind="equity")
    assert board["leaderboard"] == []
    assert tracker.get_advice_outcome_summary()["directional_count"] == 0
    assert tracker.get_recent_advice_outcomes(source="watchlist_hk") == []
    assert tracker.get_advice_accuracy("test@example.com")["总数"] == 0
    assert tracker.get_advice_outcome_windows() == []
    assert tracker.get_score_band_backtest(source="watchlist_hk")["total_reviewed"] == 0
    assert tracker.get_dimension_predictive_value(source="watchlist_hk")
    assert tracker.get_score_evidence_text(source="watchlist_hk")
    due = tracker.get_due_for_advice_review(
        "test@example.com", min_age_days=0, market="HK",
    )
    assert len(due) == 1 and due[0]["score"] is not None
    assert tracker.get_watchlist_verdict_for_symbol("0700", source="watchlist_hk")["in_pool"] is False
