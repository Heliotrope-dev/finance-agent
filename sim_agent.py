"""AI模拟盘自主交易agent——2026-09-01用户明确要求"全自动、AI自己在模拟盘上
炒股、学习、试错"。这跟sim_trader.execute_simulated_trades（跟着advisor.py
每天17:30那次组合分析的信号走）是两条独立的执行路径：这里是每小时触发一次
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
from datetime import datetime, time as dtime

from zoneinfo import ZoneInfo

import advisor
import sim_trader
import tracker

_MARKET_TZ = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}
_MARKET_HOURS = {
    "A": [(dtime(9, 30), dtime(11, 30)), (dtime(13, 0), dtime(15, 0))],
    "HK": [(dtime(9, 30), dtime(12, 0)), (dtime(13, 0), dtime(16, 0))],
    "US": [(dtime(9, 30), dtime(16, 0))],
}

# 每个开盘市场喂给AI的候选股数量——每小时跑一次，不能像advise_portfolio
# 那样带财务摘要/新闻做深度分析（那样跑一次要几分钟、几十次AI调用，一小时
# 一次的节奏耗不起），这里只给"名称/代码/现价/涨跌幅"这种一眼行情，AI基于
# 盘面动量+已经知道的持仓上下文做决策，不是基本面深挖。
_CANDIDATES_PER_MARKET = 8

# 每次决策喂给AI的历史战绩条数——太多会把prompt撑得很长还没有额外信息量
# （早期几次的参考价值不如最近几次），5条足够体现"最近是涨是跌"这个趋势。
_HISTORY_CONTEXT_SIZE = 5


def _market_is_open(market: str) -> bool:
    now = datetime.now(ZoneInfo(_MARKET_TZ[market]))
    if now.weekday() >= 5:
        return False
    t = now.time()
    return any(start <= t <= end for start, end in _MARKET_HOURS[market])


def _open_markets() -> list[str]:
    return [m for m in _MARKET_TZ if _market_is_open(m)]


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


def _history_context_lines(email: str) -> list[str]:
    """把最近几次运行的"决策前资产 vs 现在实际资产"摘要拼成几行文字，供
    AI在这次决策时参考——这是"学习试错"这个说法在LLM agent场景下能落地
    的实现方式，见文件头部说明，不是训练模型参数。
    """
    runs = tracker.get_sim_agent_runs(email, limit=_HISTORY_CONTEXT_SIZE)
    if not runs:
        return ["（还没有历史运行记录，这是第一次决策，没有过去战绩可参考）"]

    try:
        current_assets = sim_trader.get_sim_snapshot()["total_assets_cny"]
    except Exception:
        current_assets = None

    lines = []
    for r in runs:
        before = r.get("assets_cny_before")
        when = (r.get("run_at") or "")[:16].replace("T", " ")
        if before and current_assets:
            change_pct = (current_assets - before) / before * 100
            lines.append(f"- {when}（¥{before:,.0f}起）：截至现在累计变化{change_pct:+.2f}%，当时的判断：{(r.get('reasoning_text') or '（无记录）')[:80]}")
        else:
            lines.append(f"- {when}：{(r.get('reasoning_text') or '（无记录）')[:80]}")
    return lines


_AGENT_SYSTEM = """你是一个正在用虚拟资金自主管理富途模拟盘的投资agent，目标是长期跑赢大盘，
不是每次都要交易——没有把握就"不动"，频繁交易会侵蚀模拟盘的长期表现（跟真实交易一样，这是
在训练你形成正确的交易纪律，不是鼓励你多操作显得"在干活"）。

你会看到：当前持仓明细（含浮动盈亏）、账户现金、候选股当前行情（只有现价/涨跌幅，没有基本面/
新闻）、以及你自己过去几次决策后账户实际的资产变化（这是给你复盘用的——如果最近几次操作后
资产在跌，说明策略需要更保守或调整方向；如果在涨，可以适度延续当前思路，但不要因为涨了就
盲目加大手笔）。

输出格式（必须严格遵守，不要输出这个格式之外的解释性文字混在信号行里）：
先写一段简短的决策理由（100-200字，说清楚这次为什么这么操作，或者为什么选择不动），然后另起一行写：
交易信号：
每条一行，格式为 名称|代码|市场(A/HK/US)|买入或卖出或不动|股数|预计金额(折人民币)
只有"买入"或"卖出"的行会被真实执行，"不动"的行可以省略（没有值得操作的就不用写这行）。
卖出时的股数不能超过你在"当前持仓"里看到的实际持有股数。
"""


def run_cycle(email: str) -> dict:
    """跑一次自主决策循环。没有市场开盘就直接跳过，不调用AI（省调用额度，
    也没有意义——候选股行情在休市时是不变的，AI在这个状态下做决策等于
    看着几个小时前的旧数据拍脑袋）。
    """
    open_markets = _open_markets()
    if not open_markets:
        tracker.log_sim_agent_run(email, [], None, "", "[]", "跳过", "当前没有市场开盘")
        return {"status": "跳过", "note": "当前没有市场开盘"}

    try:
        snapshot = sim_trader.get_sim_snapshot()
    except Exception as e:
        tracker.log_sim_agent_run(email, open_markets, None, "", "[]", "失败", f"读取模拟盘快照失败：{e}")
        return {"status": "失败", "note": str(e)}

    candidates = _build_candidates(open_markets)
    history_lines = _history_context_lines(email)

    holdings_lines = []
    for p in snapshot["positions"]:
        pl_text = f"{p['pl_val']:+,.0f} {p['currency']}" if p.get("pl_val") is not None else "（浮盈亏未知）"
        holdings_lines.append(f"- {p['name']}（{p['code']}）：持有{p['qty']:g}股，浮动盈亏{pl_text}")
    holdings_text = "\n".join(holdings_lines) if holdings_lines else "（当前空仓）"

    cash_lines = [
        f"- {m}市场：{info['assets_native']:,.0f} {info['currency']}（约¥{info['assets_cny']:,.0f}）"
        for m, info in snapshot["markets"].items()
    ]
    cash_text = "\n".join(cash_lines) if cash_lines else "（资金信息暂时获取不到）"

    candidates_text = "\n".join(
        f"- {c['name']}（{c['symbol']}·{c['market']}）：现价{c['price']:.2f}，涨跌幅{c['pct_chg']:+.2f}%"
        if c.get("pct_chg") is not None else f"- {c['name']}（{c['symbol']}·{c['market']}）：现价{c['price']:.2f}"
        for c in candidates
    ) if candidates else "（当前开盘市场暂时没有可用的候选股行情）"

    user_content = (
        f"当前开盘市场：{'、'.join(open_markets)}\n\n"
        f"账户资金（各市场独立资金池，不能互相调用）：\n{cash_text}\n\n"
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
        tracker.log_sim_agent_run(email, open_markets, snapshot["total_assets_cny"], "", "[]", "失败", f"AI调用失败：{e}")
        return {"status": "失败", "note": str(e)}

    if not text.strip():
        tracker.log_sim_agent_run(email, open_markets, snapshot["total_assets_cny"], "", "[]", "失败", "AI返回空内容")
        return {"status": "失败", "note": "AI返回空内容"}

    signals = advisor._parse_trade_signals(text)
    sig_idx = text.find("交易信号：")
    if sig_idx == -1:
        sig_idx = text.find("交易信号:")
    reasoning_text = text[:sig_idx].strip() if sig_idx != -1 else text.strip()

    try:
        exec_results = sim_trader.execute_simulated_trades(email, signals)
    except Exception as e:
        exec_results = []
        reasoning_text += f"\n\n（下单执行失败：{e}）"

    tracker.log_sim_agent_run(
        email, open_markets, snapshot["total_assets_cny"], reasoning_text,
        json.dumps(signals, ensure_ascii=False), "完成",
        f"{len(exec_results)}条信号，其中执行成功{sum(1 for r in exec_results if r.get('status') == '成功')}条",
    )
    return {"status": "完成", "reasoning": reasoning_text, "signals": signals, "executed": exec_results}


if __name__ == "__main__":
    email = advisor._EMAIL
    result = run_cycle(email)
    print(json.dumps(result, ensure_ascii=False, indent=2))
