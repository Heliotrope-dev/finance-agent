from __future__ import annotations

import score_diagnostics


def _rows(days: int = 10) -> list[dict]:
    rows = []
    for day in range(days):
        for rank in range(6):
            score = 45 + rank * 10
            row = {
                "created_at": f"2026-01-{day + 1:02d}T09:00:00+00:00",
                "market": "US", "score": score, "price_at_advice": 100,
                "action": "买入", "confidence": "高",
            }
            for dimension in score_diagnostics.DIMENSIONS:
                row[f"score_{dimension}"] = rank + 1
                row[f"score_{dimension}_max"] = 6
            for horizon in score_diagnostics.HORIZONS:
                row[f"review_price_{horizon}d"] = 100 + rank
                row[f"bench_ret_{horizon}d"] = 0
            rows.append(row)
    return rows


def test_daily_ic_removes_market_timing_and_keeps_ranking():
    result = score_diagnostics.daily_cross_sectional_ic(_rows(), 5)
    assert result["count"] == 10
    assert result["ic_mean"] == 1
    assert result["icir"] is None  # every day is identically perfect, so std=0


def test_dimension_tests_apply_fdr_and_keep_significant_signal():
    result = score_diagnostics.dimension_horizon_stats(
        _rows(), bootstrap_samples=300, seed=1
    )
    assert len(result) == 24
    assert all(item["sample_count"] == 60 for item in result)
    assert all(item["verdict"] == "有信号" for item in result)
    assert all(item["fdr_p"] <= 0.05 for item in result)
    assert all("monotonicity" in item for item in result)


def test_public_report_hides_unverified_estimates():
    report = {
        "dimension_horizon": [{
            "verdict": "尚未验证", "ic": 0.01, "ci_low": -0.2, "ci_high": 0.3,
        }]
    }
    safe = score_diagnostics.public_report(report)
    assert safe["dimension_horizon"][0]["ic"] is None
    assert safe["dimension_horizon"][0]["ci_low"] is None
    assert safe["dimension_horizon"][0]["ci_high"] is None


def test_confidence_calibration_reports_only_mature_groups():
    result = score_diagnostics.confidence_calibration(
        _rows(), bootstrap_samples=100, seed=1
    )
    high, medium, low = result
    assert high["verdict"] == "已验证"
    assert high["sample_count"] == 60
    assert high["hit_rate_pct"] > 0
    assert medium["verdict"] == low["verdict"] == "尚未验证"
    assert score_diagnostics.confidence_assessment(result) == {
        "verdict": "尚未验证", "ordered": None,
    }


def test_cross_sectional_diagnosis_distinguishes_selection_from_timing():
    rows = _rows()
    for row in rows:
        rank = int((row["score"] - 45) / 10)
        row["review_price_5d"] = 96 + rank
    result = score_diagnostics.cross_sectional_diagnosis(rows)
    assert result["daily_ic_mean"] == 1
    assert result["avg_excess_pct"] < 0
    assert result["diagnosis"] == "选股有效、择时无效"


def test_monthly_weight_suggestion_is_advisory_and_normalized():
    result = score_diagnostics.monthly_weight_suggestion(_rows(days=25))
    assert result["verdict"] == "仅建议、不自动修改"
    assert round(sum(result["suggested_weights_pct"].values()), 6) == 100
    assert all(item["cross_section_count"] == 25 for item in result["dimensions"])
