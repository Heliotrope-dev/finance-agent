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


def save_profile(updates: dict, path: Path | str = _PROFILE_PATH) -> dict:
    """更新风险档案里的若干字段，返回写回后的完整档案。

    2026-09-12新增（前端审计第12条）：可执行清单里一直在引用"盈亏比低于
    风险档案下限 2:1"这类话，但这份档案此前只能手工改 data/risk_profile.json，
    网站上任何地方都找不到入口——用户看得到约束、却没法调整约束。

    刻意做成"读出来改几个键再写回"而不是整份覆盖：档案里除了这四个必填项
    还有 starting_capital / allowed_markets 这些不在设置界面上的字段，整份
    覆盖会把它们悄悄抹掉。校验沿用 load_profile 的口径（四个必填项都要是
    正数、两个百分比不超过100），写之前先验一遍，不让界面把档案写成一个
    load_profile 之后会判定无效、进而让整个新开仓闸门静默失效的状态。
    """
    p = Path(path)
    try:
        current = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, json.JSONDecodeError):
        current = {}

    merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
    for key in _REQUIRED:
        if key not in merged:
            raise ValueError(f"风险档案缺少必填项：{key}")
        if float(merged[key]) <= 0:
            raise ValueError(f"{key} 必须大于 0")
    if float(merged["max_risk_per_trade_pct"]) > 100 or float(merged["max_position_pct"]) > 100:
        raise ValueError("百分比类上限不能超过 100")

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


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
