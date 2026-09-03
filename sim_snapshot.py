"""AI模拟盘资产快照——2026-09-01用户反馈"走势图15分钟一格太稀疏，遇到
低波动持仓（工商银行/中国神华这种防御型股票）连续几次数字精确不变，图表
看着像死了"。这个脚本跟sim_agent.py的决策循环完全解耦：不调用AI、不判断
要不要买卖，只是单纯查一次当前持仓市值+虚拟现金，写一条快照记录，喂给
走势图用，让曲线更连续细腻。"AI每次决策记录"那个列表继续用sim_agent.py
那15分钟一次的记录，两者互不影响——决策历史和资产曲线本来就是两回事，
不用绑在同一个采样频率上。

只在港股/美股开盘时段才记（复用sim_agent._open_markets），非开盘时段
市场没有新成交、市值不会变，记了也是重复数据，不如不记。

真实故障（2026-09-03用户从走势图截图发现）：某次快照net_value_hkd
凭空冲到96412（约合$12360），5分钟后又跌回80907——查数据库发现是虚拟
现金余额（tracker.sim_virtual_cash_hkd）在一次卖出成交后立刻+19530，
但同一时刻sim_trader.get_agent_snapshot()读到的持仓市值(holdings_value_hkd)
还没跟着变（Futu模拟账户那边的持仓列表更新有延迟，不是瞬时的），于是
"卖出到手的现金"和"还没来得及消失的旧持仓市值"被同时算了一遍，净值
虚高一截；下一次(5分钟后)持仓列表追上了，市值掉下来，净值也跟着"假摔"
回真实水平。跟sim_agent.py决策循环本身那个"记账时点错位"是同一类问题，
但这里是这个独立的快照脚本自己的另一份实例——decision cron结算现金的
那一刻，如果这个快照脚本刚好也在那前后5分钟内跑，就会撞上这个不一致
窗口。改法：跟决策循环抢同一把sim_agent._LOCK_PATH文件锁（非阻塞），
抢不到就说明决策循环正在结算中，直接跳过这一轮快照，等下一次5分钟
再采——反正这个不一致窗口只有几秒到几十秒，跳过一次不会让走势图缺一
大截。
"""
import json

import sim_agent
import sim_trader
import tracker


def take_snapshot(email: str) -> dict:
    open_markets = sim_agent._open_markets()
    if not open_markets:
        return {"status": "跳过", "note": "当前没有市场开盘"}

    import fcntl

    sim_agent._LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(sim_agent._LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return {"status": "跳过", "note": "决策循环正在结算交易，本次快照跳过避免读到中间态"}

    try:
        return _take_snapshot_locked(email)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _take_snapshot_locked(email: str) -> dict:
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
