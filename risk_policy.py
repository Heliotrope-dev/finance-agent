# -*- coding: utf-8 -*-
"""Explicit, deterministic guardrails for new-position suggestions.

No numeric risk limit is inferred from a user's balance.  Until the user has
explicitly configured a profile, the system can research and monitor a symbol
but cannot present a new-buy size as an executable instruction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


_PROFILE_PATH = Path(__file__).resolve().parent / "data" / "risk_profile.json"
_REQUIRED = {"max_risk_per_trade_pct", "max_position_pct", "max_daily_loss_pct", "min_reward_risk"}


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    max_risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    min_reward_risk: float | None = None


def load_profile(path: Path | str = _PROFILE_PATH) -> dict | None:
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(profile, dict) or not _REQUIRED.issubset(profile):
        return None
    try:
        for key in _REQUIRED:
            if float(profile[key]) <= 0:
                return None
        if float(profile["max_risk_per_trade_pct"]) > 100 or float(profile["max_position_pct"]) > 100:
            return None
    except (TypeError, ValueError):
        return None
    return profile


def validate_new_position(*, market: str, entry: float | None, stop: float | None,
                          target: float | None, reward_risk: float | None,
                          profile: dict | None = None) -> RiskDecision:
    """Validate only deterministic facts required for a new-buy instruction."""
    if profile is None:
        profile = load_profile()
    elif not isinstance(profile, dict) or not _REQUIRED.issubset(profile):
        profile = None
    reasons: list[str] = []
    if profile is None:
        return RiskDecision(False, ("风险档案未配置：只提供观察，不给出新开仓数量。",))

    try:
        entry_f, stop_f, target_f, rr_f = float(entry), float(stop), float(target), float(reward_risk)
    except (TypeError, ValueError):
        return RiskDecision(False, ("执行参数不完整：缺少有效的入场、止损、目标或盈亏比。",))
    if not (stop_f < entry_f < target_f):
        reasons.append("执行参数不一致：必须满足止损 < 入场 < 目标。")
    try:
        min_rr = float(profile["min_reward_risk"])
        max_risk = float(profile["max_risk_per_trade_pct"])
        max_position = float(profile["max_position_pct"])
    except (TypeError, ValueError):
        return RiskDecision(False, ("风险档案格式无效：只提供观察，不给出新开仓数量。",))
    if rr_f < min_rr:
        reasons.append(f"盈亏比 {rr_f:.2f}:1 低于风险档案下限 {min_rr:g}:1。")
    allowed_markets = profile.get("allowed_markets")
    if allowed_markets and market not in set(allowed_markets):
        reasons.append(f"{market} 不在风险档案允许的市场范围内。")
    return RiskDecision(
        not reasons,
        tuple(reasons),
        max_risk,
        max_position,
        min_rr,
    )
