import risk_policy


def test_missing_profile_blocks_new_buy():
    decision = risk_policy.validate_new_position(
        market="HK", entry=100, stop=95, target=115, reward_risk=3, profile={},
    )
    assert decision.allowed is False
    assert "风险档案未配置" in decision.reasons[0]


def test_profile_requires_consistent_prices_and_reward_risk():
    profile = {
        "max_risk_per_trade_pct": 1,
        "max_position_pct": 10,
        "max_daily_loss_pct": 3,
        "min_reward_risk": 2.5,
        "allowed_markets": ["HK"],
    }
    assert risk_policy.validate_new_position(
        market="HK", entry=100, stop=95, target=115, reward_risk=3, profile=profile,
    ).allowed is True
    bad = risk_policy.validate_new_position(
        market="US", entry=100, stop=105, target=102, reward_risk=1, profile=profile,
    )
    assert bad.allowed is False
    assert len(bad.reasons) == 3
