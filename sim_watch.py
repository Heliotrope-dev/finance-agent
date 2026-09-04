# -*- coding: utf-8 -*-
"""AI模拟盘的轻量盯盘器：只看行情，决定这一刻要不要叫醒AI做决策。

原来的做法是让 sim_agent.py 固定每5分钟跑一轮完整决策。用户指出这个节奏
两头不讨好：行情平淡的时候每5分钟白烧一次AI调用，真出现急涨急跌的时候
又要干等最多5分钟才反应得过来，"不然就晚了"。

这个脚本把"盯盘"和"决策"拆开：

  盯盘（这里）  跑得勤（每2分钟），但只发一次批量行情请求，不碰AI，
                成本几乎为零。富途的 get_market_snapshot 一次能查一批，
                实测批量报价0.14秒。
  决策（sim_agent） 只在真正需要的时候才叫醒，一次几十秒的AI调用。

叫醒条件三选一：
  1. 距离上一轮决策超过 _BASELINE_SEC —— 保底节奏，不能因为行情太平就
     整天不看盘（也是持仓止盈止损的兜底检查）。
  2. 关注池里任何一支相对上次观测跳动超过 _JUMP_PCT —— 这是"急涨急跌
     立刻反应"那条，反应延迟从最多5分钟缩短到最多2分钟。
  3. 持仓股当日涨跌幅超过 _SWING_PCT —— 自己持有的票大幅波动，比池子里
     随便一支的跳动更值得立刻处理。

净效果是AI调用次数不升反降（保底从5分钟拉长到15分钟），同时对异动的
反应速度快了一倍多。

并发安全：真正的决策仍然走 sim_agent.run_cycle()，它自己有文件锁，
所以这里不需要额外加锁——上一轮还在跑的时候叫醒会被它自己挡掉。
"""
import json
import os
import sys
import time
from pathlib import Path

import advisor
import sim_agent
import tracker

_STATE_PATH = Path(__file__).resolve().parent / "data" / "sim_watch_state.json"

# 保底决策间隔：行情再平淡也至少这么久跑一轮。
# 原来是固定300秒，这里放宽到900秒——盯盘器已经能捕捉异动了，保底轮的
# 作用退化成"定期复查持仓止盈止损"，不需要那么频繁。
_BASELINE_SEC = 900

# 相对上一次观测价的跳动阈值。2%对港美股单只个股的2分钟窗口来说是明显
# 异动（不是噪音），又不至于宽到整天触发不了。
_JUMP_PCT = 2.0

# 持仓股当日涨跌幅阈值。自己有仓位的票走到这个幅度，不管是止盈还是止损
# 都该让AI重新看一眼。
_SWING_PCT = 4.0

# 关注池上限——批量报价是一次调用查一批，但清单太长会拖慢单次请求，
# 而且远超AI一轮能处理的候选数量，没有意义。
_MAX_WATCH = 60


def _load_state() -> dict:
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"[watch] 状态写入失败（不影响本轮判断）: {e}")


def _monitored(email: str, open_markets: list[str]) -> list[tuple[str, str, bool]]:
    """要盯的标的：(代码, 市场, 是否持仓)。

    数据全部来自本地SQLite，不额外查富途账户——查账户要为每个市场各开一个
    交易连接，那是这个每2分钟就跑一次的脚本负担不起的开销。持仓的准确清单
    由被叫醒之后的 sim_agent 自己去富途取，这里只需要一个"该盯哪些"的近似
    集合，宁可多盯几支也不要为了精确而变重。
    """
    held: set[tuple[str, str]] = set()
    for o in tracker.get_simulated_orders(email, limit=50):
        if o.get("status") == "成功" and o.get("market") in open_markets:
            held.add((str(o.get("symbol")), o.get("market")))

    items: list[tuple[str, str, bool]] = [(s, m, True) for s, m in held]
    seen = set(held)
    for p in tracker.get_positions(email):
        key = (str(p.get("symbol")), p.get("market"))
        if key[1] in open_markets and key not in seen:
            seen.add(key)
            items.append((key[0], key[1], float(p.get("shares") or 0) > 0))
    return items[:_MAX_WATCH]


def check() -> dict:
    email = advisor._EMAIL

    if not tracker.get_sim_agent_enabled(email):
        return {"status": "跳过", "note": "AI自主模拟交易已关闭"}

    open_markets = sim_agent._open_markets()
    if not open_markets:
        return {"status": "跳过", "note": "当前没有市场开盘"}

    state = _load_state()
    last_prices: dict = state.get("prices") or {}
    last_cycle = float(state.get("last_cycle_ts") or 0)
    now = time.time()

    watched = _monitored(email, open_markets)
    if not watched:
        return {"status": "跳过", "note": "关注池为空"}

    import data_sources as ds

    quotes = ds.get_stock_realtime_futu_batch([(s, m) for s, m, _ in watched])
    if not quotes:
        return {"status": "跳过", "note": "行情取不到，本轮不判断"}

    reasons: list[str] = []
    prices: dict = {}
    for symbol, market, is_held in watched:
        q = quotes.get((symbol, market)) or {}
        # 键名是中文。data_sources._futu_snapshot_row_to_dict 把富途返回的
        # DataFrame 转成了中文键的字典（最新价/昨收/涨跌幅…），不是原始的
        # last_price/prev_close_price。第一版这里照着 DataFrame 的英文列名写，
        # 取到的永远是 None——盯盘器从上线起就是瞎的，一次异动都触发不了，
        # 只有15分钟的保底轮在跑，日志里表现为"盯盘0支无异动"。
        last = q.get("最新价")
        if not last:
            continue
        key = f"{market}:{symbol}"
        prices[key] = last

        prev_seen = last_prices.get(key)
        if prev_seen:
            jump = (last - prev_seen) / prev_seen * 100
            if abs(jump) >= _JUMP_PCT:
                reasons.append(f"{symbol}({market}) 较上次观测{jump:+.1f}%")

        if is_held:
            prev_close = q.get("昨收")
            if prev_close:
                day = (last - prev_close) / prev_close * 100
                if abs(day) >= _SWING_PCT:
                    reasons.append(f"持仓{symbol}({market}) 当日{day:+.1f}%")

    elapsed = now - last_cycle
    baseline_due = elapsed >= _BASELINE_SEC

    state["prices"] = prices
    state["last_check_ts"] = now

    if not reasons and not baseline_due:
        _save_state(state)
        return {
            "status": "观望", "note": f"盯盘{len(prices)}支无异动，距上轮决策{int(elapsed)}秒",
        }

    trigger = "异动" if reasons else "保底轮"
    # 先写状态再跑决策：决策要几十秒，中途万一被外层超时杀掉，至少这次
    # 观测到的价格已经落盘，下一轮的跳动比较不会因为基准丢失而失真。
    state["last_cycle_ts"] = now
    _save_state(state)

    print(f"[watch] 触发决策（{trigger}）: {'; '.join(reasons[:5]) if reasons else '到达保底间隔'}")
    result = sim_agent.run_cycle(email)
    result["_trigger"] = trigger
    result["_reasons"] = reasons[:5]
    return result


if __name__ == "__main__":
    out = check()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # 富途SDK的线程不是daemon线程，不强制退出进程会挂住（项目里反复踩过
    # 的老坑）。os._exit会跳过stdout缓冲区刷新，管道输出时内容会整个丢
    # 掉，所以必须先flush再退出。
    sys.stdout.flush()
    os._exit(0)
