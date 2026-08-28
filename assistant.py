"""右下角"AI 咨询"悬浮窗的后端——跟 analysis.py/advisor.py 用同一个千问账号，
但这里是多轮对话（那两个模块都是单轮：一份数据进去，一段分析出来），所以单独
写一个支持完整 messages 列表的流式调用，不复用 analysis._stream_chat（那个
签名是单条 system+user，塞不下多轮历史）。

这个模块只管"怎么跟AI对话"和"把用户能问的数据整理成一段上下文"，不碰
Streamlit 组件渲染——渲染（悬浮按钮/聊天气泡/CSS定位）在 app.py 里，
职责跟 analysis.py/app.py 的分工原则一致。
"""

import os

import streamlit as st
from openai import OpenAI

_QWEN_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_MODEL = "qwen3.7-flash"


def get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")


def _client() -> OpenAI:
    key = get_secret("QWEN_API_KEY")
    if not key:
        raise RuntimeError("未配置 QWEN_API_KEY。")
    return OpenAI(api_key=key, base_url=_QWEN_BASE, max_retries=2)


_SYSTEM_PROMPT_TEMPLATE = """你是"投研站"网站里的AI助手，一个小圆形按钮，用户点开
在右下角浮窗里跟你聊天。你的任务是帮用户看懂这个网站、看懂他自己的数据，不是
提供独立的投资建议。

网站功能说明（回答"这是什么/怎么用"这类问题时依据这个）：
- 首页"推荐股排行榜"：AI每天从美股/港股/A股当天最热门的约50支股票里逐一打分
  （0-100分，四个维度加权：基本面质量、价格位置安全边际、技术面确认、数据
  确定性），取前5名展示。这个观察池不是固定名单，每天跟着热度/涨跌幅榜变化。
  次日会核对一次涨跌方向对不对，用来算历史一致率——这不是投资建议，是给用户
  一个可核验的参考。
- "回看"页面：用户自己每次点开个股详情页做过的AI交叉验证分析，都会记一条
  "偏多/偏空/中性"的方向标签，一周后核对实际涨跌，算这个用户自己的历史准确率。
- "持仓"页面：用户手动记录自己的实际持仓，AI 会结合基本面/技术面/价格位置
  给出买入/卖出/持有/观望的参考判断（同样不是指令性建议）。
- "行情"页面：三个市场的指数快照、涨跌停池、热门板块，纯数据展示不下结论。

对话规则：
1. 只用下面这段"当前数据"回答涉及具体数字/持仓/历史记录的问题，不能编造
   任何数字。这段数据里没有的，如实说"这个我这边看不到"，不要猜。
2. 不给"买/卖"这类指令性投资建议，最多说"网站上AI对这支股票的参考判断是
   XX"，最终决定权归用户自己。
3. 回答要简短直接，像聊天，不要长篇大论、不要"作为AI助手"这类开场白。
4. 用户问的如果跟这个网站完全无关（比如闲聊、其它话题），可以正常聊，
   不用生硬地把话题拉回来。

当前数据（截至本次对话开始时）：
{context}
"""


def build_context(email: str | None) -> str:
    """整理AI助手能看到的数据——email为None（游客）时只给全站公开数据。
    这里只做数据整理/格式化，不碰任何AI调用，方便调用方在打开浮窗时就先
    算好（用户还没打第一个字之前），不用每轮对话都重新查一遍数据库。
    """
    import tracker
    import data_sources as ds

    parts = []

    # 三个市场的核心指数快照+首页世界地图那几个国际指数——用户明确反馈过
    # "问恒生科技指数最新数据答不上来，网站'行情'页明明就有"，说明这类
    # 公开行情数据也得算进"所有数据"里，不能只顾着账号相关的数据。
    try:
        idx_lines = []
        for market in ("A", "HK", "US"):
            for row in ds.get_multi_index_snapshot(market):
                idx_lines.append(
                    f"  {row.get('名称')}（{market}）最新{row.get('最新')}，涨跌幅{row.get('涨跌幅'):.2f}%"
                )
        if idx_lines:
            parts.append("三个市场核心指数快照：\n" + "\n".join(idx_lines))
    except Exception:
        pass

    try:
        gidx = ds.get_global_indices()
        if gidx:
            lines = [f"  {name}：最新{v.get('最新')}，涨跌幅{v.get('涨跌幅'):.2f}%" for name, v in gidx.items()]
            parts.append("首页世界地图上的国际指数：\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        board = tracker.get_latest_leaderboard(limit=5, source="watchlist")
        rows = board.get("leaderboard") or []
        if rows:
            lines = [
                f"  {r.get('name')}（{r.get('symbol')}·{r.get('market')}）"
                f"{r.get('action')} {r.get('score')}分"
                for r in rows
            ]
            parts.append(f"首页推荐股排行榜（{board.get('run_date')}更新）：\n" + "\n".join(lines))
    except Exception:
        pass

    if not email:
        parts.append("当前是游客访问，没有登录，看不到任何个人持仓/历史记录数据。")
        return "\n\n".join(parts) if parts else "（暂无可用数据）"

    try:
        positions = tracker.get_positions(email)
        held = [p for p in positions if (p.get("shares") or 0) > 0]
        if held:
            # 每支持仓都带上AI最新一次的买入/卖出/持有/观望判断——用户明确
            # 要求"所有数据"、不用自己再手动说一遍持仓情况，这里连AI对
            # 每支持仓的最新参考意见也一并给，问"我该不该卖XX"能直接答。
            pos_advice = tracker.get_position_advice(email)

            def _fmt_pos(p):
                shares = p["shares"]
                avg_cost = (p.get("cost_total") or 0) / shares if shares else 0
                adv = pos_advice.get(p["symbol"])
                adv_text = f"，AI最新参考：{adv['action']}" if adv else ""
                return (
                    f"  {p.get('name') or p.get('symbol')}（{p.get('symbol')}·{p.get('market')}）"
                    f"持有{shares:g}股，平均成本约{avg_cost:.2f}{p.get('currency', 'CNY')}{adv_text}"
                )
            parts.append("用户当前持仓：\n" + "\n".join(_fmt_pos(p) for p in held))
        else:
            parts.append("用户当前没有持仓记录。")
    except Exception:
        pass

    try:
        pf = tracker.get_latest_portfolio_advice(email)
        if pf and pf.get("analysis_text"):
            excerpt = pf["analysis_text"][:400]
            parts.append(
                f"用户最近一次组合分析（{pf.get('created_at', '')[:10]}，总市值约"
                f"{pf.get('total_value_cny') or '—'}元）摘要：\n{excerpt}"
                + ("…（后面还有，用户问细节时如实说这段是摘要）" if len(pf["analysis_text"]) > 400 else "")
            )
    except Exception:
        pass

    try:
        acc = tracker.get_accuracy_stats(email)
        if acc.get("总数"):
            parts.append(f"用户在\"回看\"页面的历史判断：共{acc['总数']}次，方向一致率{acc['一致率']:.0f}%。")
    except Exception:
        pass

    return "\n\n".join(parts) if parts else "（暂无可用数据）"


def stream_reply(messages: list[dict], context: str, max_tokens: int = 1200):
    """messages：不含system，只有[{"role":"user"/"assistant","content":...}, ...]
    的对话历史（含本轮最新一条用户消息）。max_tokens给1200——聊天场景要求
    "简短直接"，不需要像交叉验证分析那样留几千字的预算。
    """
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(context=context)
    stream = _client().chat.completions.create(
        model=_MODEL,
        messages=[{"role": "system", "content": system_prompt}] + messages,
        temperature=0.4,
        stream=True,
        max_tokens=max_tokens,
    )
    for chunk in stream:
        # 千问偶尔发不带内容的收尾chunk（choices=[]），见analysis._stream_chat
        # 同一处踩坑记录，这里同样处理。
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
