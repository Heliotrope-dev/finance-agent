"""打新（新股认购）收益测算 —— 纯计算层，不碰 Streamlit 也不自己取数。

升级路线图第8条里"打新收益计算器"那一项：输入认购手数、是否用孖展（融资）、
融资利率，结合历史首日表现，算出期望收益。

这个模块只做算术。历史首日涨跌幅从 ipo_performance 那张表来（调用方传进来），
中签率由用户自己填——这个数我们没有数据源，港交所的分配结果是逐只公布的，
而且同一只票不同认购档位的中签率差很多，硬猜一个填进去比留空更误导。

口径上的两个要点，都写在返回值里让渲染层能说清楚：

1. **期望收益用中位数，不用均值。** 新股首日收益是典型的长尾分布——一只翻倍
   能把均值拉高十几个点。用均值算"打一只大概赚多少"会系统性高估。两个都返回，
   但渲染层该把中位数摆在前面。

2. **孖展的利息是确定的，收益是不确定的。** 融资打新最容易被忽略的不是利率
   高低，而是"利息按认购金额算、收益按中签金额算"。认购10万、中签1万，利息
   是10万在计息，收益只有1万在赚。这个模块把这件事显式算出来。
"""

from __future__ import annotations


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def summarize_history(items: list[dict]) -> dict:
    """从历史新股明细里提炼出测算要用的几个数。

    items 就是 data_sources.get_recent_ipo_performance() 返回的 items，
    每条有 first_day_pct（首日收盘相对招股价）和 open_pct（首日开盘相对招股价）。
    """
    close_pcts = [float(i["first_day_pct"]) for i in items
                  if i.get("first_day_pct") is not None]
    open_pcts = [float(i["open_pct"]) for i in items
                 if i.get("open_pct") is not None]
    out: dict = {"n": len(close_pcts)}
    if close_pcts:
        out["close_median"] = _median(close_pcts)
        out["close_avg"] = sum(close_pcts) / len(close_pcts)
        out["break_rate"] = sum(1 for p in close_pcts if p < 0) / len(close_pcts)
    if open_pcts:
        out["open_median"] = _median(open_pcts)
        out["open_avg"] = sum(open_pcts) / len(open_pcts)
        out["open_break_rate"] = sum(1 for p in open_pcts if p < 0) / len(open_pcts)
    # 开盘就卖 vs 持到收盘，哪个更好——这是打新最实际的一个决策，而且只用
    # 已有数据就能回答，不需要暗盘数据。
    if close_pcts and open_pcts and len(close_pcts) == len(open_pcts):
        diffs = [c - o for c, o in zip(close_pcts, open_pcts)]
        out["hold_gain_median"] = _median(diffs)
        out["hold_better_rate"] = sum(1 for d in diffs if d > 0) / len(diffs)
    return out


def estimate(
    lot_price: float,
    lots: int,
    hit_rate_pct: float,
    expected_move_pct: float,
    *,
    margin_ratio: float = 0.0,
    margin_rate_pct: float = 0.0,
    margin_days: int = 7,
    fee_per_subscription: float = 100.0,
) -> dict:
    """算一次打新的预期结果。金额单位跟 lot_price 一致（港股就是港币）。

    lot_price           每手金额（入场费），= 招股价 × 每手股数
    lots                认购手数
    hit_rate_pct        中签率，百分数（用户填，我们没有数据源）
    expected_move_pct   预期首日涨跌幅，百分数（默认用历史中位数）
    margin_ratio        孖展比例，0=不融资，0.9=九成融资
    margin_rate_pct     融资年利率，百分数
    margin_days         计息天数，港股打新一般冻结5~7天
    fee_per_subscription 券商认购手续费，一次性

    返回的 dict 里所有金额都是同一币种，不做任何换算。
    """
    out: dict = {}
    lots = max(int(lots or 0), 0)
    lot_price = max(float(lot_price or 0), 0.0)
    if lots <= 0 or lot_price <= 0:
        return out

    subscribe_amount = lot_price * lots
    margin_ratio = min(max(float(margin_ratio or 0.0), 0.0), 0.99)
    borrowed = subscribe_amount * margin_ratio
    own_capital = subscribe_amount - borrowed

    # 利息按认购总额里借来的那部分算，跟中不中签无关——这正是融资打新最容易
    # 被忽略的一点：认购10万、中签1万，利息是借来的那9万在计息，收益只有
    # 中签的1万在赚。
    interest = borrowed * (float(margin_rate_pct or 0.0) / 100.0) * (int(margin_days or 0) / 365.0)

    hit_rate = max(float(hit_rate_pct or 0.0), 0.0) / 100.0
    # 中签金额：按中签率折算的期望值。实际中签是离散的（中0手或中1手），
    # 这里算的是"长期重复打新的平均结果"，单次会跳变——渲染层要写明。
    allotted_amount = subscribe_amount * hit_rate
    gross_profit = allotted_amount * (float(expected_move_pct or 0.0) / 100.0)
    net_profit = gross_profit - interest - float(fee_per_subscription or 0.0)

    out.update({
        "subscribe_amount": subscribe_amount,
        "own_capital": own_capital,
        "borrowed": borrowed,
        "interest": interest,
        "fee": float(fee_per_subscription or 0.0),
        "allotted_amount": allotted_amount,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
    })
    if own_capital > 0:
        out["return_on_own_capital"] = net_profit / own_capital

    # 盈亏平衡：首日要涨多少，中签的这部分才够覆盖利息和手续费。
    # 融资比例越高、中签率越低，这个数越吓人——这正是要让用户看见的东西。
    if allotted_amount > 0:
        out["breakeven_move_pct"] = (interest + float(fee_per_subscription or 0.0)) / allotted_amount * 100.0
    return out
