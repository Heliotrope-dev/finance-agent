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
# 一次的节奏耗不起），这里只给"名称/代码/现价/涨跌幅/量比/换手率"这种一眼
# 行情，AI基于盘面动量+已经知道的持仓上下文做决策，不是基本面深挖。量比/
# 换手率是2026-09-01补的——之前只给涨跌幅，AI有一次决策说某支票"成交活跃"，
# 拿真实Futu数据核对发现那支票当天量比只有0.8（低于日均量），"活跃"是编的，
# 手上压根没有能验证"活不活跃"的数字。现在给了真数据，AI再说"活跃"就该是
# 从这两个数字看出来的，不是凭感觉。
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
                # 量比/换手率——给AI判断"是不是真的活跃"用真数据，不是只看
                # 涨跌幅自己脑补，见data_sources.get_stock_realtime_futu里
                # 这两个字段的说明。
                "volume_ratio": spot.get("量比"), "turnover_rate": spot.get("换手率"),
                # 52周高低/PE/PB——给AI判断"现在这个位置贵不贵、追高风险大不大"
                # 用的，同样是之前拿得到但没往上传的数据。
                "high_52w": spot.get("52周最高"), "low_52w": spot.get("52周最低"),
                "pe_ttm": spot.get("PE_TTM"), "pb": spot.get("PB"),
            })
    return candidates


def _fx_rates() -> tuple[float, float]:
    """(USD兑HKD, CNY兑HKD)固定汇率——用sim_trader.py里唯一的那份定义
    (USD_HKD_RATE/CNY_HKD_RATE)，不在这里另外写一份，避免两处汇率数字
    以后改一个忘了改另一个。"""
    return sim_trader.USD_HKD_RATE, sim_trader.CNY_HKD_RATE


def _estimate_amount_hkd(signal: dict, shares: float, price_map: dict, usd_hkd: float, cny_hkd: float) -> float:
    """一笔买卖预计涉及的金额(折HKD)——优先用candidates里这次决策时真实
    拉到的实时价格(比AI自己估的amount_cny准)，candidates里没有这个标的
    (AI选了候选池之外的股票)才退回用amount_cny粗略折算。"""
    price = price_map.get((signal["symbol"], signal["market"]))
    if price:
        rate = 1.0 if signal["market"] == "HK" else usd_hkd
        return price * shares * rate
    return (signal.get("amount_cny") or 0) * cny_hkd


def _symbol_from_code(code: str) -> str:
    """持仓快照里的code带市场前缀（如"US.META"/"HK.02685"），信号/候选股里
    的symbol不带前缀（"META"/"02685"）——两边要按symbol对齐时统一去掉前缀，
    跟sim_trader._to_futu_code反过来的逻辑对应。"""
    for prefix in ("HK.", "US.", "SH.", "SZ."):
        if code.upper().startswith(prefix):
            return code[len(prefix):]
    return code


# 单一标的的持仓市值（旧仓+这次新买）不能超过虚拟预算的这个比例——真实
# 故障(2026-09-02复盘发现)：_apply_budget_limit原来只拦"这一笔买入会不会
# 花超剩余额度"，没有拦"这一笔买完之后这支票会不会占了整个账户的大头"。
# 实测复盘时发现：META这支票不是靠一笔大单买的，是靠2026-09-01那天好几次
# 决策里分别各买1-4股这种小单，每笔单独看都在"不超过剩余额度30-40%"的
# 规则以内，但因为是同一支票反复加，累计下来占到了当时账户市值的四成多，
# 明显违背"不能全仓押单一标的"这条第五步里已经写的原则——问题是原有的
# 那条规则只管住了"单次下手的力度"，没管住"这支票累计占比"，两者不是一回事，
# 前者防的是"一把梭"，后者防的是"蚂蚁搬家式的隐性集中"，都要拦。
_MAX_SINGLE_POSITION_PCT = 0.30


def _apply_budget_limit(
    signals: list[dict], candidates: list[dict], holdings_value_hkd: float, virtual_cash_hkd: float,
    positions: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """买入信号按_VIRTUAL_BUDGET_HKD做硬性拦截，卖出/不动不受影响（卖出只
    会腾出额度，不会超支）。按AI给出的顺序依次核对，额度不够了后面的买入
    直接丢弃，不是按金额排序挑"划算的"——AI给信号的顺序本身通常已经体现了
    优先级，这里不该越俎代庖重新排序。

    真实故障(2026-09-02)：原来这里只按"100k减去当前持仓市值"算剩余额度，
    完全没看virtual_cash_hkd这个真实现金余额还剩多少——持仓市值会跟着
    真实行情涨跌波动，哪怕现金已经花超了、余额已经是负数，只要"持仓市值
    暂时低于100k"，这个检查就会继续放行新的买入，实测虚拟现金被这样
    一路买成了-2.6万，账户彻底失真。现在同时看两个上限，取更紧的那个：
    一是不能超过100k的名义总预算，二是不能超过账上真实还有的现金——
    后者是这次真正缺的那道闸，没有它，"名义预算没超"不等于"真的有钱付"。

    同一天(2026-09-02)复盘又发现第二个缺口，一并补上：单支标的的累计持仓
    也要设上限（_MAX_SINGLE_POSITION_PCT），不能只看"这一笔够不够钱买"，
    见上面这个常量的注释。这里按symbol+market把已有持仓市值(HKD)加总，
    买入信号如果会让这支票的（旧仓+这笔）总市值超过上限，直接拦截——
    跟总预算拦截同一个原则，不能只信AI自己说"我会分散"，代码层面要真的
    核实。positions传None（比如老调用方还没升级）时这层检查直接跳过，
    不影响原有的总预算拦截继续生效。
    """
    price_map = {(c["symbol"], c["market"]): c["price"] for c in candidates}
    usd_hkd, cny_hkd = _fx_rates()

    existing_by_symbol: dict[tuple[str, str], float] = {}
    if positions:
        for p in positions:
            sym = _symbol_from_code(p.get("code", ""))
            key = (sym, p.get("market"))
            existing_by_symbol[key] = existing_by_symbol.get(key, 0.0) + (p.get("market_val_hkd") or 0.0)

    max_single_position_hkd = _VIRTUAL_BUDGET_HKD * _MAX_SINGLE_POSITION_PCT
    remaining = min(_VIRTUAL_BUDGET_HKD - holdings_value_hkd, virtual_cash_hkd)
    kept, dropped = [], []
    for s in signals:
        if s.get("action") != "买入":
            kept.append(s)
            continue
        est_cost_hkd = _estimate_amount_hkd(s, s["shares"], price_map, usd_hkd, cny_hkd)
        key = (s["symbol"], s["market"])
        projected_position_hkd = existing_by_symbol.get(key, 0.0) + est_cost_hkd
        if projected_position_hkd > max_single_position_hkd:
            # 浅拷贝加个提示字段，不改动signals里的原始dict（那份原样要
            # 完整写进log_sim_agent_run，见调用方注释）。
            dropped.append({**s, "_drop_reason": "集中度"})
            continue
        if est_cost_hkd <= remaining:
            kept.append(s)
            remaining -= est_cost_hkd
            existing_by_symbol[key] = projected_position_hkd
        else:
            dropped.append({**s, "_drop_reason": "预算"})
    return kept, dropped


def _settle_virtual_cash(email: str, kept_signals: list[dict], exec_results: list[dict], candidates: list[dict]) -> tuple[float, float]:
    """只对真正下单成功的信号结算虚拟现金——预算检查只是"预计"，真正扣钱
    以是否成功下单为准，不能纸上谈兵（比如lot_size取整后不足一手被
    sim_trader跳过的买入，不该扣虚拟现金，那笔钱根本没花出去）。用
    exec_results里的shares_ordered(真实下单股数，可能因取整跟AI原话不完全
    一致)而不是AI原始给的shares，金额算得更准。用户明确要求"加上平台附带的
    手续费其他费用"——每笔成交额外按sim_trader.calc_fee_hkd扣富途真实费率
    (佣金/平台费/印花税/交收费等)，买入是"花的钱+手续费"，卖出是"收的钱-
    手续费"，跟真实交易的现金流向一致。返回(结算后的新虚拟现金余额, 这次
    结算总共扣的手续费)——现金余额已经写入数据库，调用方不用再存一次；
    手续费只是拿去展示/记录用，不用额外存。
    """
    usd_hkd, cny_hkd = _fx_rates()
    price_map = {(c["symbol"], c["market"]): c["price"] for c in candidates}
    signal_map = {s["symbol"]: s for s in kept_signals}

    cash = tracker.get_sim_virtual_cash(email)
    if cash is None:
        cash = _VIRTUAL_BUDGET_HKD

    total_fee_hkd = 0.0
    for r in exec_results:
        if r.get("status") != "成功":
            continue
        s = signal_map.get(r["symbol"])
        if not s:
            continue
        shares = r.get("shares") or 0
        amount_hkd = _estimate_amount_hkd(s, shares, price_map, usd_hkd, cny_hkd)
        price = price_map.get((s["symbol"], s["market"])) or 0.0
        fee_hkd = sim_trader.calc_fee_hkd(s["market"], shares, price, s["action"], usd_hkd)
        total_fee_hkd += fee_hkd
        if s["action"] == "买入":
            cash -= (amount_hkd + fee_hkd)
        elif s["action"] == "卖出":
            cash += (amount_hkd - fee_hkd)

    tracker.set_sim_virtual_cash(email, cash)
    return cash, total_fee_hkd


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


def _performance_scoreboard(email: str, holdings_value_hkd: float) -> str:
    """一句话战绩摘要（胜率+平均涨跌），放在历史记录最前面。

    用户明确要求"给他强化学习"——老实说，调的是千问的API，碰不到模型
    参数，做不到真正意义上的强化学习（根据reward更新权重）。这里做的是
    退而求其次但确实有用的事：把"最近这些操作到底表现怎么样"从一堆散落
    的文字复盘里提炼成一个具体的量化数字（胜率/平均涨跌），当成每次决策
    prompt里最先看到的"考卷分数"——不是让模型自己变聪明，是让它每次决策
    前先看到一个不能回避的、量化的、真实的战绩，比純文字复盘更难被自己
    的话术粉饰过去（比如"这次虽然跌了但逻辑是对的"这种自我安慰）。
    """
    runs = tracker.get_sim_agent_runs(email, limit=_HISTORY_CONTEXT_SIZE)
    current_assets = _virtual_net_value(email, holdings_value_hkd)
    results = []
    for r in runs:
        before = r.get("assets_hkd_before")
        try:
            sigs = json.loads(r.get("signals_json") or "[]")
        except Exception:
            sigs = []
        acted = any(s.get("action") in ("买入", "卖出") for s in sigs)
        if acted and before and current_assets:
            results.append((current_assets - before) / before * 100)
    if not results:
        return "（还没有可统计的操作记录，暂无战绩）"
    win_rate = sum(1 for x in results if x > 0) / len(results) * 100
    avg = sum(results) / len(results)
    return f"最近{len(results)}次有实际操作的决策：胜率{win_rate:.0f}%，平均单次累计变化{avg:+.2f}%"


def _history_context_lines(email: str, holdings_value_hkd: float) -> list[str]:
    """把最近几次运行的"当时买卖了什么 + 决策前资产 vs 现在实际资产"摘要拼成
    几行文字，供AI在这次决策时参考——这是"学习试错"这个说法在LLM agent场景下
    能落地的实现方式，见文件头部说明，不是训练模型参数。

    用户明确要求过"不是一直都是散户思维，要边炒股边总结套路、越买越像专家"——
    只给一个笼统的"总资产变化了多少%"不够，那是现金+全部持仓混在一起的结果，
    AI没法从里面反推"我当时挑的那几支票到底判断得准不准"。这里把当时具体
    买卖了哪些标的一起带上（从signals_json里挑买入/卖出的行），让AI能把
    "自己当时的判断"和"后续这段时间资产的实际走向"对上号，才有真的复盘的
    基础，而不是空对空喊"我要更谨慎"或"我要更激进"。
    """
    runs = tracker.get_sim_agent_runs(email, limit=_HISTORY_CONTEXT_SIZE)
    if not runs:
        return ["（还没有历史运行记录，这是第一次决策，没有过去战绩可参考）"]

    current_assets = _virtual_net_value(email, holdings_value_hkd)

    lines = []
    for r in runs:
        before = r.get("assets_hkd_before")
        when = _to_cn_time_str(r.get("run_at"))
        try:
            sigs = json.loads(r.get("signals_json") or "[]")
        except Exception:
            sigs = []
        acted = [s for s in sigs if s.get("action") in ("买入", "卖出")]
        picks_text = "、".join(f"{s['action']}{s.get('name', s.get('symbol', ''))}" for s in acted) or "未操作"
        if before and current_assets:
            change_pct = (current_assets - before) / before * 100
            lines.append(
                f"- {when}（HK${before:,.0f}起，当时{picks_text}）：截至现在累计变化{change_pct:+.2f}%，"
                f"当时的判断：{(r.get('reasoning_text') or '（无记录）')[:80]}"
            )
        else:
            lines.append(f"- {when}（当时{picks_text}）：{(r.get('reasoning_text') or '（无记录）')[:80]}")
    return lines


_AGENT_SYSTEM = f"""你是一个正在用虚拟资金自主管理富途模拟盘的投资agent，只交易港股和美股（A股
不在你的操作范围内，就算候选或持仓信息里出现也不要碰）。这是一个纯测试环境，用户明确要求
"别拿稳定的股票，要激进点，看看AI的判断能力"——目标不是追求稳健跑赢大盘，是尽可能体现出你
自己真实的选股和择时判断力。优先在候选股里挑波动性大、题材性强、当日成交活跃（涨跌幅明显、
不是那种全天纹丝不动的股票）的标的，主动一些去建仓和调仓，不要总是"不动"——高股息、低波动
的防御型蓝筹（比如银行、能源这类"压舱石"风格）不是你这个测试场景该优先考虑的对象，除非你
判断它当下确实有明确的短期机会。当然，"激进"不等于"乱来"——每次操作还是要基于候选股行情
和你自己的判断给出理由，不能纯粹为了操作而操作。

你的虚拟预算是港币{_VIRTUAL_BUDGET_HKD:,.0f}元，这是你能动用的总规模上限（已建仓位市值+
还没用的现金合计不能超过这个数），不是账户里显示的全部资金——账户本身可能显示有更多余额，
那是系统给的，不属于你的可用范围，你只用把自己当成手里只有这么多钱在做决策就行。超出预算
的买入会被系统直接拦截不执行，所以你自己也要心里有数，不要开出明显超预算的买入。

你会看到：当前持仓明细（含浮动盈亏）、你的虚拟预算使用情况（已用多少、还剩多少额度）、
候选股当前行情（现价/涨跌幅/量比/换手率/52周区间位置/PE(TTM)/PB，没有新闻/研报这类
定性信息）、以及你自己过去几次决策——当时具体买卖了哪些标的、当时的判断理由、以及截至
现在账户资产的实际变化。所有判断只能基于这些真实给出的数字，不能装作自己还查得到分析师
评级、历史估值分位数、财报细节这些没给你的信息——没有的数据就是没有，不能编。

用户明确要求"写一个完整的SOP，覆盖专家应该看的数据维度，全面思考周到再做决定"——下面
这套流程是这个测试场景里明确要求你遵守的标准作业程序（不是建议，是硬性要求），对候选股
清单里每一个你认真考虑的标的，都按这个顺序过一遍，而不是只看涨跌幅一个维度就下判断：

全局判断（做单支决策之前先看）——两件事：
一是市场情绪，"今日候选股整体涨跌情况"这一行给出了候选池里涨跌家数比例，多数在跌的时候
整体应该更保守（哪怕单支信号看着不错，也要打个折扣，因为大环境不配合），多数在涨的时候
可以适度放开一点，但仍要逐支过下面的位置/估值/动能检查，不能因为"大家都在涨"就降低标准。
二是相关性/集中度，如果这一轮打算同时买入多支候选，看看它们是不是同一个板块/题材（比如
都是芯片股、都是同一条产业链）——即使每支单独看仓位都不大，扎堆买同一类标的等于实际风险
敞口远超表面的仓位百分比，同类板块一起跌的时候会同时中招，尽量在不同类型的标的之间分散，
不要用"仓位都很小"当借口掩盖集中度过高的问题。

第一步·位置研判——现价在52周区间的位置（给出的百分比，0%=52周最低、100%=52周最高）。
接近高位（比如>85%）说明追高空间有限、一旦回调没有缓冲；接近低位（比如<30%）说明有
潜在安全边际，但也可能是趋势走弱、无人问津，要结合当天动能一起看，不能只看位置就下结论。

第二步·估值研判——PE(TTM)/PB这两个绝对倍数。只有这两个数字，没有这支股票自己的历史
分位数据，所以只能做"这个倍数高不高"的粗判断（比如同类型公司常见范围），不能装作知道
"处于历史XX%分位"这种你实际算不出来的精确结论。位置研判+估值研判合起来看："高位+高
估值"叠加是风险最大的追涨区间，"低位+低估值"更像左侧布局但要有动能佐证才值得进场，
不是躺在低位就该买。

第三步·动能确认——涨跌幅+量比+换手率。量比明显大于1（放量）才是真的有资金在动，涨跌幅
好看但量比平平甚至小于1（缩量）的，大概率是情绪化的假拉升，不该追。如果你的理由里提到
"成交活跃"/"资金关注"/"量能配合"这类说法，必须能对应上这里给出的量比/换手率数字，说了
就要站得住，不能只看涨跌幅顺嘴编一个"活跃"当理由。

第四步·持仓复核与止损——买新的之前，先把"当前持仓"里每一支现有仓位过一遍，看浮动盈亏。
切记不要追涨杀跌：追涨指的是一支股票已经涨了一大截、你没有等到真正的确认信号（放量、
突破关键位）就因为"怕错过"冲进去；杀跌指的是仅仅因为价格一时下跌就恐慌性抛售，没有
真正判断这是不是短期正常波动。这两个都是要避免的散户式冲动反应。但止损不是杀跌——如果
一支持仓明显在验证你当初的判断是错的（比如放量跌破买入时设想的关键位、或者当初买入的
理由本身已经被证伪，比如原本判断的"放量"现在变成了缩量走弱），就应该果断卖出锁定亏损，
不能因为"已经跌了不甘心""再等等说不定会涨回来"就一直拖着装死——那才是真正伤害账户的
行为，纪律性止损跟情绪化杀跌是两回事，前者是理性执行、后者是恐慌反应。

第五步·仓位与风险控制（新买入）——单笔买入不能一口气用掉可用额度的大头（原则上不超过
还剩额度的30-40%），一次判断错了不该伤筋动骨；候选股再有吸引力也不能全仓押单一标的。
如果"综合战绩"里的胜率明显偏低（比如低于40%），这个上限要主动收得更紧（比如降到20%
以内）——判断力正在被验证不太靠谱的阶段，仓位应该跟着降，而不是维持原样甚至加大去
"翻本"，后者是典型的赌徒心态。同时在理由里隐含地想清楚"如果这个判断错了，会在什么
位置/什么信号下先被证伪"（比如跌破今天低点、放量下跌）——哪怕系统层面不支持你自己挂
止损单，这个思考过程也要体现在reasoning里，方便下一轮复盘时对照"当时设想的证伪信号
出现了没有"，也方便下一轮真正执行第四步的止损检查。

真实教训(2026-09-02复盘)：不要只盯着"这一笔买多大"，还要看"这支票加完这一笔之后累计
占了多大比例"——之前出现过对同一支标的连续好几轮各买一小笔，每笔单独看都不大、都在
上面这条单笔上限以内，但因为反复加仓同一支票，几天下来这一支票累计占到了账户四成多，
实质上就是变相的全仓单押，跟一次性重仓没有本质区别，只是过程更隐蔽。所以决定买入前，
先看一眼"当前持仓"里这支票是不是已经有仓位、有多大——如果加上这一笔会让这支票的
累计持仓明显超过虚拟预算的三成，就不该再加，哪怕这一笔本身金额不大、哪怕这支票这次
的信号看起来很强；这时候更合理的选择是要么不加仓、要么去看看别的标的分散一下。系统
层面也会做这道硬性拦截（超过三成会被直接拒绝执行），但你自己判断时就该把这条规则
内化进去，不要每次都被系统事后拦下来才知道，那样浪费一次本可以更有效使用的决策机会。

每笔交易都有真实的富途手续费（佣金/平台费/印花税等，港股大约几十港币起，美股也有固定
费用），频繁小额进出会被手续费明显侵蚀收益——不要为了"保持活跃"去做金额很小、预期
波动空间也很小的交易，那种交易手续费占比过高，赢面再大也可能是亏的。

第六步·拒绝"为了操作而操作"——前面几步走完，如果这一轮候选股里没有哪个真正有说服力
（比如位置已经很高、估值不便宜、量能又没跟上），"不动"本身就是一个合格的决策，不用为了
显得"活跃"硬凑一笔操作。

用户明确要求这不是"每次决策互不相关地随便选选"，而是要"边炒股边学习，哪里踩过坑、摸清楚
套路，越操作越像一个真正懂行情的专家，不能一直是散户思维"。具体到每次决策，你应该先花几句
话真正复盘历史记录，而不是走过场——但复盘的前提是真的有历史可复盘：如果下面"综合战绩"和
"过去几次决策"这两部分给出的内容明确说"还没有历史运行记录/暂无战绩"，你的理由就必须如实
承认"这是重置后的第一次决策，没有历史战绩可参考"，绝对不能凭空编一段"近N次胜率X%"这种
听起来像模像样、但下面数据根本不支持的复盘叙事——编造不存在的历史表现比"没有历史"本身
严重得多，是在编数据，不是在分析数据。
- 你会先看到一行"综合战绩"（最近N次有实际操作的决策里，胜率多少、平均涨跌多少）——这是
  一个不能回避的量化分数，如果胜率明显低于50%或平均是负的，说明最近的判断框架整体有问题，
  这一轮必须认真反思到底是哪类判断系统性地错了，不能只挑一两笔亏损轻描淡写带过。
- 对上一轮/前几轮买的标的，现在看是判断对了还是错了？如果错了，当时的理由错在哪个环节
  （比如：把短期情绪波动当成了趋势启动、追高了明显缺乏安全边际的位置、选的标的其实成交
  不够活跃只是看起来热门）？
- 有没有反复出现的失误模式（比如总是在同一类题材/同一个价格区间判断失误）？如果有，这一轮
  要明确避开，不要重蹈覆辙。
- 有没有已经验证有效的判断路径（比如某类动能信号确实带来了正收益）？如果有，可以延续、
  但不能盲目照搬（市场状态会变，同一招不会永远有效）。
- "最近几天的长期复盘记录"是跨天的教训，跟上面当次决策的短期战绩是两个时间尺度——重点
  找那种短期战绩看不出来、只有拉长了看才会暴露的规律，比如同一支股票在好几天的复盘里都
  被点名是"最失败的判断"，这种情况哪怕今天它现价/量比看起来又符合买入条件，也要提高警惕、
  想清楚这次跟前几次真的不一样在哪里，而不是因为"这次数据看着不错"就重蹈覆辙。
散户思维的典型表现是：每次决策都从零开始、跟着当天最热的新闻情绪追涨杀跌、赢了归功于自己
判断准、亏了就归咎于运气不好而不去找真实原因。你要做的是相反的事——把每一轮的结果都当成
一次真实的市场反馈，持续修正自己的判断框架，而不是每次都用同一套朴素的直觉重新来一遍。

输出格式（必须严格遵守，不要输出这个格式之外的解释性文字混在信号行里）：
先写一段简短的决策理由（100-200字，说清楚这次为什么这么操作，或者为什么选择不动），然后另起一行写：
交易信号：
每条一行，格式为 名称|代码|市场(HK或US)|买入或卖出或不动|股数|预计金额(折人民币，就算你是用港币
思考决策的，这一列的数字也按人民币估算填，系统内部会自动处理，不用你自己换算成港币)
只有"买入"或"卖出"的行会被真实执行，"不动"的行可以省略（没有值得操作的就不用写这行）。
卖出时的股数不能超过你在"当前持仓"里看到的实际持有股数。

真实故障(2026-09-01)：出现过好几次决策理由里明确说"小仓加仓XX"/"小仓试探XX"，
但下面交易信号部分完全没写这一行、最终什么都没执行——理由文字和实际信号
对不上。写理由的时候不要一边权衡一边把"倾向性想法"当成结论写出来，只写你
最终真正决定要做的事：如果最终决定不操作某支候选，理由里也只能说"不操作
XX，因为……"，不能说"小仓买入XX"这种听起来已经决定、但下面信号里其实
没有对应那一行的话——理由文字本身就是你最终决定的准确描述，不是思考过程
的实况转播。
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
    # "AI模拟炒股"页的"AI自主模拟交易"开关——2026-09-01真实故障纠偏：这里
    # 之前完全不检查任何开关，openclaw那个cron只要是enabled状态就会一直
    # 跑，导致页面上的开关是摆设，关了也没用。见tracker.py里
    # sim_agent_enabled字段的注释。
    if not tracker.get_sim_agent_enabled(email):
        tracker.log_sim_agent_run(email, [], None, "", "[]", "跳过", "AI自主模拟交易已关闭")
        return {"status": "跳过", "note": "AI自主模拟交易已关闭"}

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
    # 这次决策"开始前"的净值快照——必须在AI调用/下单之前就固定下来，不能
    # 等这次交易执行完再算。之前的bug：交易执行完之后拿"决策前的持仓市值"
    # (这时还不包含刚买的这笔)去加"结算后的现金"(已经扣了这笔买入的钱)，
    # 一来一回把这笔新建仓的市值凭空漏掉了，净值平白无故"蒸发"一截，下一次
    # 运行时因为重新查到了真实持仓市值又"恢复"正常，图表上看起来像坐了
    # 一次过山车，实际上是记账时点错位导致的假象，不是真的涨跌。改成统一
    # 只记"决策前"这一个时点的净值，每次运行前后台账各自独立结清，不会
    # 有"半路记录一个既扣了钱又没算上货"的中间态。
    net_value_before = _virtual_net_value(email, holdings_value_hkd)
    scoreboard_text = _performance_scoreboard(email, holdings_value_hkd)
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

    def _fmt_candidate(c: dict) -> str:
        parts = [f"现价{c['price']:.2f}"]
        if c.get("pct_chg") is not None:
            parts.append(f"涨跌幅{c['pct_chg']:+.2f}%")
        if c.get("volume_ratio") is not None:
            parts.append(f"量比{c['volume_ratio']:.2f}")
        if c.get("turnover_rate") is not None:
            parts.append(f"换手率{c['turnover_rate']:.2f}%")
        high_52w, low_52w = c.get("high_52w"), c.get("low_52w")
        if high_52w and low_52w and high_52w > low_52w:
            pos_pct = (c["price"] - low_52w) / (high_52w - low_52w) * 100
            parts.append(f"52周区间位置{pos_pct:.0f}%（52周高{high_52w:.2f}/低{low_52w:.2f}）")
        if c.get("pe_ttm") is not None:
            parts.append(f"PE(TTM){c['pe_ttm']:.1f}倍")
        if c.get("pb") is not None:
            parts.append(f"PB{c['pb']:.1f}倍")
        return f"- {c['name']}（{c['symbol']}·{c['market']}）：" + "，".join(parts)

    candidates_text = "\n".join(_fmt_candidate(c) for c in candidates) if candidates else "（当前开盘市场暂时没有可用的候选股行情）"

    # 粗粒度市场情绪——候选股本身就是当天热门/活跃股，涨跌比例能大致反映
    # "今天这个市场整体是risk-on还是risk-off"，不用额外接指数数据源。只是
    # 参考背景，不是决策的唯一依据（候选池本身就偏向活跃股，不是市场的
    # 无偏样本，比例只能看个大概方向）。
    up_count = sum(1 for c in candidates if (c.get("pct_chg") or 0) > 0)
    down_count = sum(1 for c in candidates if (c.get("pct_chg") or 0) < 0)
    breadth_text = f"候选股里{up_count}涨{down_count}跌（仅供参考，候选池本身偏向活跃股，不是全市场无偏样本）" if candidates else "（无候选股，无法判断）"

    # 长期经验——2026-09-02用户明确要求"他也要学习，想让他变强大"。这是
    # sim_agent_report.py每天用真实数据机械算出来的日度复盘，不是AI临时
    # 编的，见该文件头部说明。跟上面"综合战绩"(最近5次决策的短期战绩)是
    # 两个不同的时间尺度——这个是跨天的，能看到反复出现的问题模式。
    lessons = tracker.get_sim_agent_lessons(email, limit=5)
    lessons_text = "\n".join(f"- {l['lesson_text']}" for l in lessons) if lessons else "（还没有跨天的长期复盘记录）"

    user_content = (
        f"当前开盘市场：{'、'.join(open_markets)}\n\n"
        f"{budget_text}\n\n"
        f"当前持仓：\n{holdings_text}\n\n"
        f"候选股行情（仅供参考，也可以选择不在这些里面操作，只要是当前开盘市场的股票都可以）：\n{candidates_text}\n\n"
        f"今日候选股整体涨跌情况：{breadth_text}\n\n"
        f"综合战绩：{scoreboard_text}\n\n"
        f"你过去几次决策后的实际战绩（用于复盘）：\n" + "\n".join(history_lines) + "\n\n"
        f"最近几天的长期复盘记录（每天一条，跨天规律用这个找，比如同一支票反复亏钱）：\n{lessons_text}"
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
        tracker.log_sim_agent_run(email, open_markets, net_value_before, "", "[]", "失败", f"AI调用失败：{e}")
        return {"status": "失败", "note": str(e)}

    if not text.strip():
        tracker.log_sim_agent_run(email, open_markets, net_value_before, "", "[]", "失败", "AI返回空内容")
        return {"status": "失败", "note": "AI返回空内容"}

    signals = advisor._parse_trade_signals(text)
    sig_idx = text.find("交易信号：")
    if sig_idx == -1:
        sig_idx = text.find("交易信号:")
    reasoning_text = text[:sig_idx].strip() if sig_idx != -1 else text.strip()

    # 真实故障纠偏（2026-09-01）：这里原来只拦A股（market not in _AGENT_MARKETS，
    # 即不在("HK","US")里），漏了一种情况——AI手上还持有一支HK股票，但这一轮
    # 只有US开盘（HK已收盘），AI对着这支HK持仓给了"卖出"信号，因为HK本身在
    # _AGENT_MARKETS里，没被这道拦截挡住，直接送进了execute_simulated_trades，
    # Futu对已收盘市场下的市价单不会真的成交（一直挂在SUBMITTED），但
    # place_order本身返回RET_OK，代码把"下单被接受"当成"成交"处理，
    # 结果虚拟现金台账多记了一笔根本没花出去/没收回来的钱，真实持仓也没变。
    # 跟下面预算拦截同一个道理——不能只信prompt里"只交易港股和美股"这句话，
    # 这里必须拦住"当前不在这一轮open_markets里"的市场，不能只拦A股。
    off_market_signals = [s for s in signals if s.get("market") not in open_markets and s.get("action") in ("买入", "卖出")]
    if off_market_signals:
        _a_text = "、".join(f"{s['name']}（{s['symbol']}·{s.get('market')}）" for s in off_market_signals)
        reasoning_text += f"\n\n（系统提示：以下不属于本轮开盘市场（{', '.join(open_markets)}），已被拦截未执行：{_a_text}）"
    # 不直接给signals重新赋值——跟下面预算拦截同一个原则，signals这个
    # 变量最后要完整原样写进log_sim_agent_run（含被拦截的这些），方便
    # 在"AI每次决策记录"里如实看到"AI试图操作了什么、系统拦了什么"，
    # 只是不让这些进入下面真正下单的执行链路。
    tradeable_signals = [s for s in signals if s not in off_market_signals]

    # 硬性预算拦截——不能只信AI自己说的"我会控制在预算内"，必须代码层面
    # 真的核实过再放行，见_apply_budget_limit的docstring（现在同时看真实
    # 现金余额，不能只看"持仓市值有没有超过100k"）。
    current_cash = tracker.get_sim_virtual_cash(email)
    if current_cash is None:
        current_cash = _VIRTUAL_BUDGET_HKD
    kept_signals, dropped_signals = _apply_budget_limit(
        tradeable_signals, candidates, holdings_value_hkd, current_cash, snapshot.get("positions"),
    )
    if dropped_signals:
        _budget_dropped = [s for s in dropped_signals if s.get("_drop_reason") != "集中度"]
        _concentration_dropped = [s for s in dropped_signals if s.get("_drop_reason") == "集中度"]
        if _budget_dropped:
            _dropped_text = "、".join(f"{s['name']}（{s['symbol']}）{s['shares']:g}股" for s in _budget_dropped)
            reasoning_text += f"\n\n（系统提示：以下买入超出十万港币虚拟预算，已被拦截未执行：{_dropped_text}）"
        if _concentration_dropped:
            # 见_apply_budget_limit/_MAX_SINGLE_POSITION_PCT的docstring——这个
            # 提示专门跟预算超支分开说，因为对AI来说这是两类完全不同的教训：
            # 预算超支是"钱不够"，集中度超限是"钱够但不该全押这一支"，混在
            # 一起讲AI没法从提示里学到正确的那条规律。
            _c_text = "、".join(f"{s['name']}（{s['symbol']}）{s['shares']:g}股" for s in _concentration_dropped)
            reasoning_text += (
                f"\n\n（系统提示：以下买入会让该标的累计持仓超过虚拟预算的"
                f"{_MAX_SINGLE_POSITION_PCT:.0%}（单一标的集中度上限），已被拦截未执行：{_c_text}）"
            )

    try:
        exec_results = sim_trader.execute_simulated_trades(email, kept_signals)
    except Exception as e:
        exec_results = []
        reasoning_text += f"\n\n（下单执行失败：{e}）"

    # 结算虚拟现金供下一轮用——这次记录的净值快照用net_value_before（这次
    # 决策开始前的状态），不用结算后的值，理由见上面net_value_before那段
    # 注释。
    _, total_fee_hkd = _settle_virtual_cash(email, kept_signals, exec_results, candidates)

    tracker.log_sim_agent_run(
        email, open_markets, net_value_before, reasoning_text,
        json.dumps(signals, ensure_ascii=False), "完成",
        f"{len(exec_results)}条信号，其中执行成功{sum(1 for r in exec_results if r.get('status') == '成功')}条"
        + (f"，{len(dropped_signals)}条超预算被拦截" if dropped_signals else "")
        + (f"，本轮手续费共HK${total_fee_hkd:,.2f}" if total_fee_hkd > 0 else ""),
    )
    return {"status": "完成", "reasoning": reasoning_text, "signals": signals, "dropped": dropped_signals, "executed": exec_results}


if __name__ == "__main__":
    email = advisor._EMAIL
    result = run_cycle(email)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Futu SDK建立连接开的线程不是daemon线程（跟advisor.py同一个老坑，
    # 见advisor.py那边的注释）——不强制退出的话，main()跑完正常逻辑后
    # 进程会一直挂着不退出，直到cron的超时限制才被杀死。这也是刚才
    # 5次连续误判"上一轮还没跑完"的直接原因：进程早就干完活了，只是
    # 迟迟不退出，一直占着并发锁。os已经在文件顶部import过了。
    os._exit(0)
