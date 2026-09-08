# -*- coding: utf-8 -*-
"""盘中导师式推荐——AI解读层。只在mentor_scan.scan()报告有新事件时才被
调用，不掐表运行，不重判整个候选池，只针对"刚发生的这一条事件"给一段
短平快的解读。

2026-09-08新增。跟judge_stock那种完整研报格式的调用完全不是一回事——
那个是8000-12000起步的完整基本面+技术面+新闻综合判断；这里只需要几百到
一两千token，输入是事件本身+早盘Top3报告里已经算好的判断依据，不重新
抓资料、不重新计算价位。
"""
import json
from pathlib import Path

import advisor

_DATA_DIR = Path(__file__).resolve().parent / "data"

_SYSTEM_PROMPT = """你是一位稳健的资深盘中投资导师，风格是给用户解释"这意味着什么"，
不是信号机器人甩一个动作就完事。你会收到一支股票刚刚触发的一条盘中事件，以及
这支票在今天早盘报告里已有的判断依据（评分/结论/关键理由）。

按这个结构回答，总共不超过150字：
1. 发生了什么——用给你的数据说一句话，不要编造没给你的数字
2. 这对早盘那份判断意味着什么——是验证了原来的逻辑，还是被推翻了，还是现在信息还不够判断
3. 建议的下一步——观察/暂缓/按原计划执行/需要重新评估，加一句为什么

不要重新计算或虚构新的止损价/目标价/买入区间，只用事件里给你的数字。
结尾不用加"仅供参考"这类免责声明，那个由调用方统一加。"""


def _load_plan_context(market: str, symbol: str) -> dict | None:
    """从当天该市场的盘前快照里找这支票的完整判断条目（评分/结论/理由），
    找不到就返回None，interpret()会退化成只用事件本身的信息。
    """
    path = _DATA_DIR / f"daily_plan_{market.lower()}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for x in (plan.get("候选池明细") or []) + (plan.get("持仓处理") or []):
        if x.get("代码") == symbol:
            return x
    return None


def interpret(event: dict) -> str:
    """event: mentor_scan.scan()产出的单条事件字典。返回一段导师口吻的
    短解读文本（不含免责声明，调用方拼接）。
    """
    market = event.get("市场", "")
    symbol = event.get("代码", "")
    lines = [
        f"事件类型：{event.get('类型')}",
        f"标的：{event.get('名称')}（{symbol}·{market}）",
        f"现价：{event.get('现价')}，当日涨跌：{event.get('当日涨跌')}%",
    ]
    for k in ("止损", "目标", "买入区间", "量比", "口径说明"):
        v = event.get(k)
        if v is not None:
            lines.append(f"{k}：{v}")

    ctx = _load_plan_context(market, symbol)
    if ctx:
        lines.append("")
        lines.append(f"早盘报告里的判断：{ctx.get('方向')} {ctx.get('评分')}分")
        reason = ctx.get("依据") or ""
        if reason:
            lines.append(f"关键理由：{str(reason)[:300]}")

    user_content = "\n".join(lines)

    advisor._load_secrets_into_env()
    text = advisor.chat_with_failover(
        [{"role": "system", "content": _SYSTEM_PROMPT},
         {"role": "user", "content": user_content}],
        max_tokens=800, temperature=0.3, timeout=60, tag="mentor_event",
    )
    return text.strip()


def interpret_events(events: list[dict]) -> str:
    """多条事件一起解读，拼成一条微信消息。事件数量正常应该很少（同一轮
    扫描触发好几支的情况少见），逐条独立调AI，不合并成一次多标的prompt——
    避免长prompt推高延迟，也让每条解读互不干扰。
    """
    parts = []
    for e in events:
        tag = e.get("紧急度", "")
        try:
            body = interpret(e)
        except Exception:
            # Provider details are operational diagnostics, not investment
            # evidence.  Leaking them to WeChat both looks broken and can make
            # an AI outage sound like a market fact.
            body = (f"事实：{e.get('类型')}，现价 {e.get('现价')}，当日"
                    f"{e.get('当日涨跌')}%。AI解读暂不可用；请按早盘既定风险线复核。")
        parts.append(f"[{tag}] {e.get('名称')}（{e.get('代码')}）\n{body}")
    parts.append("仅供参考，不构成投资建议，请自行判断。")
    return "\n\n".join(parts)
