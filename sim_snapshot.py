"""AI模拟盘资产快照——2026-09-01用户反馈"走势图15分钟一格太稀疏，遇到
低波动持仓（工商银行/中国神华这种防御型股票）连续几次数字精确不变，图表
看着像死了"。这个脚本跟sim_agent.py的决策循环完全解耦：不调用AI、不判断
要不要买卖，只是单纯查一次当前持仓市值+虚拟现金，写一条快照记录，喂给
走势图用，让曲线更连续细腻。"AI每次决策记录"那个列表继续用sim_agent.py
那15分钟一次的记录，两者互不影响——决策历史和资产曲线本来就是两回事，
不用绑在同一个采样频率上。

只在港股/美股开盘时段才记（复用sim_agent._open_markets），非开盘时段
市场没有新成交、市值不会变，记了也是重复数据，不如不记。
"""
import json

import sim_agent
import sim_trader
import tracker


def take_snapshot(email: str) -> dict:
    open_markets = sim_agent._open_markets()
    if not open_markets:
        return {"status": "跳过", "note": "当前没有市场开盘"}

    try:
        snapshot = sim_trader.get_agent_snapshot()
    except Exception as e:
        return {"status": "失败", "note": str(e)}

    holdings_value = snapshot.get("holdings_value_hkd", 0.0)
    virtual_cash = tracker.get_sim_virtual_cash(email)
    if virtual_cash is None:
        virtual_cash = sim_agent._VIRTUAL_BUDGET_HKD

    tracker.log_equity_snapshot(email, holdings_value, virtual_cash)
    return {"status": "完成", "holdings_value_hkd": holdings_value, "virtual_cash_hkd": virtual_cash}


if __name__ == "__main__":
    import advisor

    result = take_snapshot(advisor._EMAIL)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # 跟sim_agent.py/advisor.py同一个老坑：Futu SDK开的线程不是daemon
    # 线程，不强制退出的话进程会一直挂着。
    import os
    os._exit(0)
