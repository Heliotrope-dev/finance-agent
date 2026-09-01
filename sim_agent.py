"""AI模拟盘自主交易agent——2026-09-01用户明确要求"全自动、AI自己在模拟盘上
炒股、学习、试错"。这跟sim_trader.execute_simulated_trades（跟着advisor.py
每天17:30那次组合分析的信号走）是两条独立的执行路径：这里是每15分钟触发一次
的自主决策循环，AI自己看当前持仓/现金/候选股行情/过去的操作战绩，自己决定
买卖，不依赖组合分析那条链路。

复用advisor.py现成的千问客户端(_client()/_MODEL)，不需要新API key——这个
项目2026-08-22就从DeepSeek切到千问了，key已经配在secrets.toml里。

"学习/试错"的实现边界要如实说明：这不是训练模型权重的强化学习，是每次运行
把过去N次决策的资产变化摘要写进prompt当反馈，让AI"看到"自己过去操作后账户
是涨是跌，从而在文字层面调整策略——是一种朴素的、基于上下文的自我修正，
不是真正意义上的机器学习。
"""
import json
import os
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

import advisor
import sim_trader
import tracker

# 15分钟一次的节奏比之前设计的每小时紧得多——如果某一轮因为网络慢/AI响应
# 慢跑超过15分钟，下一次cron触发时上一轮可能还没结束，两个进程同时下单
# 有真实的重复交易风险。用一个简单的文件锁挡住重叠执行：抢不到锁就直接
# 跳过这次，不排队等待（排队会导致锁越攒越多，不如干脆跳过等下一个自然
# 触发点）。
_LOCK_PATH = Path(__file__).parent / "data" / "sim_agent.lock"

_MARKET_TZ = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}
_MARKET_HOURS = {
    "A": [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))],
    "HK": [(dtime(9, 30), dtime(12, 0)), (dtime(13, 0), dtime(16, 0))],
    "US": [(dtime(9, 30), dtime(16, 0))],
}

# 2026-09-01用户明确要求"仅需要在港美股开盘阶段思考就行"——自主决策循环
# 只看这两个市场是否开盘，A股不参与这个每15分钟一次的自主交易（A股T+1
# 不能当日回转，跟这个高频动量交易的节奏本来就不搭）。A股的SIMULATE账户
# 依然存在、依然会在快照里展示余额，只是这条自主决策链路不会主动碰它。
_AGENT_MARKETS = ("HK", "US")

# 2026-09-01用户明确要求"总资金十万港币"——但富途模拟账户本金没法通过
# 任何接口或App设置改成这个具体数字（客服回复是"理论无上限"，账户里实际
# 躺着港股100万+美股100万等值资金），用户明确要求"让AI自己控制一下"：
# 这里不是靠AI自觉，是代码层面的硬预算——AI能动用的仓位规模上限固定按
# 这个虚拟数字算，买入前会拿"当前已用仓位市值+这笔预计花费"跟这个上限比，
# 超了就不下单，不管AI自己怎么说。真实账户里那些用不到的钱，跟这个agent
# 的决策逻辑无关，只是账户碰巧有这么多，不代表这个agent的可用资金规模。
_VIRTUAL_BUDGET_HKD = 100_000

# 每个开盘市场喂给AI的候选股数量——每小时跑一次，不能像advise_portfolio
# 那样带财务摘要/新闻做深度分析（那样跑一次要几分钟、几十次AI调用，一小时
# 一次的节奏耗不起），这里只给"名称/代码/现价/涨跌幅"这种一眼行情，AI基于
# 盘面动量+已经知道的持仓上下文做决策，不是基本面深挖。
_CANDIDATES_PER_MARKET = 8

# 每次决策喂给AI的历史战绩条数——太多会把prompt撑得很长还没有额外信息量
# （早期几次的参考价值不如最近几次），5条足够体现"最近是涨是跌"这个趋势。
_HISTORY_CONTEXT_SIZE = 5


_CN_TZ = timezone(timedelta(hours=8))


def _to_cn_time_str(iso_str: str) -> str:
    """数据库存的run_at是UTC(datetime.now(timezone.utc).isoformat())——这里
    只是给AI看的prompt文本用，不是给最终用户的界面，但跟app.py那边统一
    改成北京时间展示，避免自己在prompt里写"04:30"这种跟真实交易时段对不
    上的时间，AI理解起来也别扭。"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16].replace("T", " ")


def _market_is_open(market: str) -> bool:
    now = datetime.now(ZoneInfo(_MARKET_TZ[market]))
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(start <= t <= end for start, end in _MARKET_HOURS[market])


def _open_markets() -> list[str]:
    return [m for m in _AGENT_MARKETS if _market_is_open(m)]


def _build_candidates(open_markets: list[str]) -> list[dict]:
    """从当前开盘市场的热门股池里各取一小批——复用advisor.py._build_watchlist
    现成的热门股/涨跌幅榜逻辑，不用每小时重新做一次全市场量化初筛。
    """
    import data_sources as ds

    all_items = advisor._build_watchlist()
    by_market: dict[str, list[dict]] = {}
    for it in all_items:
        if it["market"] in open_markets:
            by_market.setdefault(it["market"], []).append(it)

    candidates = []
    for market in open_markets:
        for it in by_market.get(market, [])[:_CANDIDATES_PER_MARKET]:
            try:
                spot = ds.get_stock_realtime(it["symbol"], market=market)
            except Exception:
                spot = None
            if not spot or not spot.get("最新价"):
                continue
            candidates.append({
                "symbol": it["symbol"], "market": market, "name": it.get("name") or it["symbol"],
                "price": spot["最新价"], "pct_chg": spot.get("涨跌幅"),
            })
    return candidates


def _fx_rates() -> tuple[float, float]:
    """(USD兑HKD, CNY兑HKD)——只服务于虚拟预算的估算/记账，不追求精确到
    分，接口都失败时用近似兜底值，不能让预算控制因为汇率接口挂了就整个
    失效（宁可用一个大致对的数字继续保守放行，也不能没了汇率就放弃拦截）。
    """
    import data_sources as ds

    usd_cny, _n1 = ds.get_fx_rate("USD")
    hkd_cny, _n2 = ds.get_fx_rate("HKD")
    usd_hkd = (usd_cny / hkd_cny) if (usd_cny and hkd_cny) else 8.6
    cny_hkd = (1 / hkd_cny) if hkd_cny else 1.08
    return usd_hkd, cny_hkd


def _estimate_amount_hkd(signal: dict, shares: float, price_map: dict, usd_hkd: float, cny_hkd: float) -> float:
    """一笔买卖预计涉及的金额(折HKD)——优先用candidates里这次决策时真实
    拉到的实时价格(比AI自己估的amount_cny准)，candidates里没有这个标的
    (AI选了候选池之外的股票)才退回用amount_cny粗略折算。"""
    price = price_map.get((signal["symbol"], signal["market"]))
    if price:
        rate = 1.0 if signal["market"] == "HK" else usd_hkd
        return price * shares * rate
    return (signal.get("amount_cny") or 0) * cny_hkd


def _apply_budget_limit(signals: list[dict], candidates: list[dict], holdings_value_hkd: float) -> tuple[list[dict], list[dict]]:
    """买入信号按_VIRTUAL_BUDGET_HKD做硬性拦截，卖出/不动不受影响（卖出只
    会腾出额度，不会超支）。按AI给出的顺序依次核对，额度不够了后面的买入
    直接丢弃，不是按金额排序挑"划算的"——AI给信号的顺序本身通常已经体现了
    优先级，这里不该越俎代庖重新排序。
    """
    price_map = {(c["symbol"], c["market"]): c["price"] for c in candidates}
    usd_hkd, cny_hkd = _fx_rates()

    remaining = _VIRTUAL_BUDGET_HKD - holdings_value_hkd
    kept, dropped = [], []
    for s in signals:
        if s.get("action") != "买入":
            kept.append(s)
            continue
        est_cost_hkd = _estimate_amount_hkd(s, s["shares"], price_map, usd_hkd, cny_hkd)
        if est_cost_hkd <= remaining:
            kept.append(s)
            remaining -= est_cost_hkd
        else:
            dropped.append(s)
    return kept, dropped


def _settle_virtual_cash(email: str, kept_signals: list[dict], exec_results: list[dict], candidates: list[dict]) -> float:
    """只对真正下单成功的信号结算虚拟现金——预算检查只是"预计"，真正扣钱
    以是否成功下单为准，不能纸上谈兵（比如lot_size取整后不足一手被
    sim_trader跳过的买入，不该扣虚拟现金，那笔钱根本没花出去）。用
    exec_results里的shares_ordered(真实下单股数，可能因取整跟AI原话不完全
    一致)而不是AI原始给的shares，金额算得更准。返回结算后的新虚拟现金
    余额（已经写入数据库，调用方不用再存一次）。
    """
    usd_hkd, cny_hkd = _fx_rates()
    price_map = {(c["symbol"], c["market"]): c["price"] for c in candidates}
    signal_map = {s["symbol"]: s for s in kept_signals}

    cash = tracker.get_sim_virtual_cash(email)
    if cash is None:
        cash = _VIRTUAL_BUDGET_HKD

    for r in exec_results:
        if r.get("status") != "成功":
            continue
        s = signal_map.get(r["symbol"])
        if not s:
            continue
        shares = r.get("shares") or 0
        amount_hkd = _estimate_amount_hkd(s, shares, price_map, usd_hkd, cny_hkd)
        if s["action"] == "买入":
            cash -= amount_hkd
        elif s["action"] == "卖出":
            cash += amount_hkd

    tracker.set_sim_virtual_cash(email, cash)
    return cash


def _virtual_net_value(email: str, holdings_value_hkd: float) -> float:
    """虚拟净值 = 当前持仓市值(精确、实时) + 虚拟现金余额(近似记账，见
    _settle_virtual_cash)。这是"如果只给AI十万港币"这个概念下真正该看的
    数字，不能用富途账户真实总资产——那里面绝大部分是AI碰不到的闲置
    资金，混进去收益率会完全失真。
    """
    cash = tracker.get_sim_virtual_cash(email)
    if cash is None:
        cash = _VIRTUAL_BUDGET_HKD
    return holdings_value_hkd + cash


def _history_context_lines(email: str, holdings_value_hkd: float) -> list[str]:
    """把最近几次运行的"决策前资产 vs 现在实际资产"摘要拼成几行文字，供
    AI在这次决策时参考——这是"学习试错"这个说法在LLM agent场景下能落地
    的实现方式，见文件头部说明，不是训练模型参数。
    """
    runs = tracker.get_sim_agent_runs(email, limit=_HISTORY_CONTEXT_SIZE)
    if not runs:
        return ["（还没有历史运行记录，这是第一次决策，没有过去战绩可参考）"]

    current_assets = _virtual_net_value(email, holdings_value_hkd)

    lines = []
    for r in runs:
        before = r.get("assets_hkd_before")
        when = _to_cn_time_str(r.get("run_at"))
        if before and current_assets:
            change_pct = (current_assets - before) / before * 100
            lines.append(f"- {when}（HK${before:,.0f}起）：截至现在累计变化{change_pct:+.2f}%，当时的判断：{(r.get('reasoning_text') or '（无记录）')[:80]}")
        else:
            lines.append(f"- {when}：{(r.get('reasoning_text') or '（无记录）')[:80]}")
    return lines


_AGENT_SYSTEM = f"""你是一个正在用虚拟资金自主管理富途模拟盘的投资agent，只交易港股和美股（A股
不在你的操作范围内，就算候选或持仓信息里出现也不要碰），目标是长期跑赢大盘，不是每次都要
交易——没有把握就"不动"，频繁交易会侵蚀模拟盘的长期表现（跟真实交易一样，这是在训练你形成
正确的交易纪律，不是鼓励你多操作显得"在干活"）。

你的虚拟预算是港币{_VIRTUAL_BUDGET_HKD:,.0f}元，这是你能动用的总规模上限（已建仓位市值+
还没用的现金合计不能超过这个数），不是账户里显示的全部资金——账户本身可能显示有更多余额，
那是系统给的，不属于你的可用范围，你只用把自己当成手里只有这么多钱在做决策就行。超出预算
的买入会被系统直接拦截不执行，所以你自己也要心里有数，不要开出明显超预算的买入。

你会看到：当前持仓明细（含浮动盈亏）、你的虚拟预算使用情况（已用多少、还剩多少额度）、
候选股当前行情（只有现价/涨跌幅，没有基本面/新闻）、以及你自己过去几次决策后账户实际的资产
变化（这是给你复盘用的——如果最近几次操作后资产在跌，说明策略需要更保守或调整方向；如果在
涨，可以适度延续当前思路，但不要因为涨了就盲目加大手笔）。

输出格式（必须严格遵守，不要输出这个格式之外的解释性文字混在信号行里）：
先写一段简短的决策理由（100-200字，说清楚这次为什么这么操作，或者为什么选择不动），然后另起一行写：
交易信号：
每条一行，格式为 名称|代码|市场(HK或US)|买入或卖出或不动|股数|预计金额(折人民币，就算你是用港币
思考决策的，这一列的数字也按人民币估算填，系统内部会自动处理，不用你自己换算成港币)
只有"买入"或"卖出"的行会被真实执行，"不动"的行可以省略（没有值得操作的就不用写这行）。
卖出时的股数不能超过你在"当前持仓"里看到的实际持有股数。
"""


def run_cycle(email: str) -> dict:
    """跑一次自主决策循环。没有市场开盘就直接跳过，不调用AI（省调用额度，
    也没有意义——候选股行情在休市时是不变的，AI在这个状态下做决策等于
    看着几个小时前的旧数据拍脑袋）。抢不到并发锁（上一轮还没跑完）也直接
    跳过，不重复执行。
    """
    import fcntl

    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return {"status": "跳过", "note": "上一轮还没跑完，本次不重叠执行"}

    try:
        return _run_cycle_locked(email)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _run_cycle_locked(email: str) -> dict:
    open_markets = _open_markets()
    if not open_markets:
        tracker.log_sim_agent_run(email, [], None, "", "[]", "跳过", "当前没有市场开盘")
        return {"status": "跳过", "note": "当前没有市场开盘"}

    try:
        snapshot = sim_trader.get_agent_snapshot()
    except Exception as e:
        tracker.log_sim_agent_run(email, open_markets, None, "", "[]", "失败", f"读取模拟盘快照失败：{e}")
        return {"status": "失败", "note": str(e)}

    candidates = _build_candidates(open_markets)
    holdings_value_hkd = snapshot.get("holdings_value_hkd", 0.0)
    history_lines = _history_context_lines(email, holdings_value_hkd)

    holdings_lines = []
    for p in snapshot["positions"]:
        pl_text = f"{p['pl_val']:+,.0f} {p['currency']}" if p.get("pl_val") is not None else "（浮盈亏未知）"
        holdings_lines.append(f"- {p['name']}（{p['code']}）：持有{p['qty']:g}股，浮动盈亏{pl_text}")
    holdings_text = "\n".join(holdings_lines) if holdings_lines else "（当前空仓）"

    # 预算文案给AI看的是"虚拟额度"，不是账户里真实躺着的港股/美股各百万
    # 现金——见_AGENT_SYSTEM和_VIRTUAL_BUDGET_HKD的注释，账户本金没法通过
    # API/App设置改成十万港币，只能靠这层软约束+下面的硬性拦截。
    remaining_hkd = _VIRTUAL_BUDGET_HKD - holdings_value_hkd
    budget_text = (
        f"虚拟预算总额：HK${_VIRTUAL_BUDGET_HKD:,.0f}\n"
        f"已用于持仓（按当前市值）：HK${holdings_value_hkd:,.0f}\n"
        f"还剩可用额度：HK${remaining_hkd:,.0f}"
        + ("（部分持仓汇率暂时获取不到，实际占用可能比这个数字更高，买入要更保守）" if snapshot.get("holdings_value_partial") else "")
    )

    candidates_text = "\n".join(
        f"- {c['name']}（{c['symbol']}·{c['market']}）：现价{c['price']:.2f}，涨跌幅{c['pct_chg']:+.2f}%"
        if c.get("pct_chg") is not None else f"- {c['name']}（{c['symbol']}·{c['market']}）：现价{c['price']:.2f}"
        for c in candidates
    ) if candidates else "（当前开盘市场暂时没有可用的候选股行情）"

    user_content = (
        f"当前开盘市场：{'、'.join(open_markets)}\n\n"
        f"{budget_text}\n\n"
        f"当前持仓：\n{holdings_text}\n\n"
        f"候选股行情（仅供参考，也可以选择不在这些里面操作，只要是当前开盘市场的股票都可以）：\n{candidates_text}\n\n"
        f"你过去几次决策后的实际战绩（用于复盘）：\n" + "\n".join(history_lines)
    )

    try:
        resp = advisor._client().chat.completions.create(
            model=advisor._MODEL,
            messages=[
                {"role": "system", "content": _AGENT_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            max_tokens=2000,
            temperature=0.4,
            stream=False,
        )
        text = resp.choices[0].message.content or ""
    except Exception as e:
        net_value = _virtual_net_value(email, holdings_value_hkd)
        tracker.log_sim_agent_run(email, open_markets, net_value, "", "[]", "失败", f"AI调用失败：{e}")
        return {"status": "失败", "note": str(e)}

    if not text.strip():
        net_value = _virtual_net_value(email, holdings_value_hkd)
        tracker.log_sim_agent_run(email, open_markets, net_value, "", "[]", "失败", "AI返回空内容")
        return {"status": "失败", "note": "AI返回空内容"}

    signals = advisor._parse_trade_signals(text)
    sig_idx = text.find("交易信号：")
    if sig_idx == -1:
        sig_idx = text.find("交易信号:")
    reasoning_text = text[:sig_idx].strip() if sig_idx != -1 else text.strip()

    # 硬性预算拦截——不能只信AI自己说的"我会控制在预算内"，必须代码层面
    # 真的核实过再放行，见_apply_budget_limit的docstring。
    kept_signals, dropped_signals = _apply_budget_limit(signals, candidates, holdings_value_hkd)
    if dropped_signals:
        _dropped_text = "、".join(f"{s['name']}（{s['symbol']}）{s['shares']:g}股" for s in dropped_signals)
        reasoning_text += f"\n\n（系统提示：以下买入超出十万港币虚拟预算，已被拦截未执行：{_dropped_text}）"

    try:
        exec_results = sim_trader.execute_simulated_trades(email, kept_signals)
    except Exception as e:
        exec_results = []
        reasoning_text += f"\n\n（下单执行失败：{e}）"

    net_value_after = _settle_virtual_cash(email, kept_signals, exec_results, candidates) + holdings_value_hkd

    tracker.log_sim_agent_run(
        email, open_markets, net_value_after, reasoning_text,
        json.dumps(signals, ensure_ascii=False), "完成",
        f"{len(exec_results)}条信号，其中执行成功{sum(1 for r in exec_results if r.get('status') == '成功')}条"
        + (f"，{len(dropped_signals)}条超预算被拦截" if dropped_signals else ""),
    )
    return {"status": "完成", "reasoning": reasoning_text, "signals": signals, "dropped": dropped_signals, "executed": exec_results}


if __name__ == "__main__":
    email = advisor._EMAIL
    result = run_cycle(email)
    print(json.dumps(result, ensure_ascii=False, indent=2))
