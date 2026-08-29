"""右下角"AI 咨询"悬浮窗的后端——跟 analysis.py/advisor.py 用同一个千问账号，
但这里是多轮对话（那两个模块都是单轮：一份数据进去，一段分析出来），所以单独
写一个支持完整 messages 列表的流式调用，不复用 analysis._stream_chat（那个
签名是单条 system+user，塞不下多轮历史）。

这个模块只管"怎么跟AI对话"和"把用户能问的数据整理成一段上下文"，不碰
Streamlit 组件渲染——渲染（悬浮按钮/聊天气泡/CSS定位）在 app.py 里，
职责跟 analysis.py/app.py 的分工原则一致。
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor

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
  （0-100分，四个维度加权算出——基本面质量0-40分：盈利能力/增长是否可持续/
  负债水平；价格位置安全边际0-30分：现价相对52周高低点的位置+估值历史分位；
  技术面确认0-15分：技术信号跟基本面判断是不是同向；数据确定性0-15分：财务/
  新闻/技术面这三类数据是否齐全、互相印证，缺得越多分越低——注意"没有估值
  历史分位数据"不算在这里，已经在价格位置那项里扣过了，不重复扣），取前5名
  展示。这个观察池不是固定名单，每天跟着热度/涨跌幅榜变化。次日会核对一次
  涨跌方向对不对，用来算历史一致率——这不是投资建议，是给用户一个可核验的
  参考。
- "回看"页面：用户自己每次点开个股详情页做过的AI交叉验证分析，都会记一条
  "偏多/偏空/中性"的方向标签，一周后核对实际涨跌，算这个用户自己的历史准确率。
- "持仓"页面：用户手动记录自己的实际持仓，AI 会结合基本面/技术面/价格位置
  给出买入/卖出/持有/观望的参考判断（同样不是指令性建议）。
- "行情"页面：三个市场的指数快照、涨跌停池、热门板块，纯数据展示不下结论。

对话规则：
1. 只用下面这段"当前数据"回答涉及具体数字/持仓/历史记录的问题，不能编造
   任何数字。这段数据里没有的，如实说"这个我这边看不到"，不要猜。用户说的
   数字跟这段数据对不上时（比如"我记得我还持有XX"但数据里没有），以这段
   数据为准如实说，不要顺着用户的说法改口附和。
2. 不对个股/大盘未来涨跌做预测、不给加减仓/仓位比例建议、不替用户下判断——
   不只是不说"买/卖"这两个字，"你觉得我该不该加仓""明天大概率涨还是跌"
   这类问法本质也是要一个指令性结论，同样要回避，最多说"网站上AI给这支
   股票的参考判断是XX"，把已有的参考信息给出来，不重新下一个新结论，
   最终决定权归用户自己。
3. 回答要简短直接，像聊天，不要长篇大论、不要"作为AI助手"这类开场白。
4. 用户问的如果跟这个网站完全无关（比如闲聊、其它话题），可以正常聊，
   不用生硬地把话题拉回来。
5. 下面的数据是打开这次对话那一刻查到的快照，之后不会自动刷新。如果用户
   问的是"现在/最新"的数字，且这次对话已经聊了一段时间，提醒一句这是
   刚打开时的快照，建议去对应页面看最新数据，不要让用户误以为是实时的。
6. 游客（没登录）看不到持仓/历史记录，但首页排行榜、三个市场指数这类公开
   数据照样能聊——遇到游客主动问"你都知道什么"，可以提这些公开数据，
   不用被动等游客先问持仓再拒绝。

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
    # 三个市场分别限时8秒，不用一个for循环顺序查——2026-08-28复核实测过：
    # HK这条路径在Futu偶尔卡顿时会掉进_one_index_snapshot的新浪EOD兜底分支，
    # 那条分支本身没有超时保护（见该函数docstring原有的踩坑记录），单个
    # market卡住会拖死整个build_context，用户点开AI浮窗要等一分钟以上。
    # 三个市场互不依赖，各自开一个线程限时8秒，一个卡住不连累另外两个，
    # 卡住的那个直接跳过，不强行等。
    try:
        idx_lines = []
        # 不用with语句——ThreadPoolExecutor的__exit__会调shutdown(wait=True)，
        # 等所有提交的任务全部完成才退出，会把fut.result(timeout=8)这个超时
        # 白白抵消掉（读结果超时了，但退出这个代码块本身还是会卡住等那个
        # 慢线程跑完）。手动shutdown(wait=False)，卡住的线程留在后台自生自
        # 灭（不理想，但这类"卡住的线程杀不掉、只能不等它"是Futu SDK本身
        # 的限制，advisor.py的_run_concurrent_with_deadline也是同一个处理
        # 方式，不是这里独有的妥协）。
        _ex = ThreadPoolExecutor(max_workers=3)
        futures = {_ex.submit(ds.get_multi_index_snapshot_slow, m): m for m in ("A", "HK", "US")}
        for fut in futures:
            market = futures[fut]
            try:
                for row in fut.result(timeout=8):
                    idx_lines.append(
                        f"  {row.get('名称')}（{market}）最新{row.get('最新')}，涨跌幅{row.get('涨跌幅'):.2f}%"
                    )
            except Exception:
                continue
        _ex.shutdown(wait=False)
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
            # 只有分数+动作答不出"为什么排第一"这类问题——之前AI被问到这个
            # 只能干瞪眼说"看不到细节"，其实fundamental_verdict/technical_
            # signal这两个字段advice表里本来就有，只是没喂给AI。摘一段
            # （完整判断可能上千字，只取前200字够回答"为什么"，不用整段塞）。
            def _fmt_board(r):
                verdict_excerpt = (r.get("fundamental_verdict") or "")[:200]
                return (
                    f"  {r.get('name')}（{r.get('symbol')}·{r.get('market')}）"
                    f"{r.get('action')} {r.get('score')}分\n"
                    f"    技术面：{r.get('technical_signal') or '—'}\n"
                    f"    判断依据摘要：{verdict_excerpt}"
                )
            lines = [_fmt_board(r) for r in rows]
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
                line = (
                    f"  {p.get('name') or p.get('symbol')}（{p.get('symbol')}·{p.get('market')}）"
                    f"持有{shares:g}股，平均成本约{avg_cost:.2f}{p.get('currency', 'CNY')}"
                )
                if adv:
                    excerpt = (adv.get("fundamental_verdict") or "")[:200]
                    line += f"\n    AI最新参考：{adv['action']}（{adv.get('created_at', '')[:10]}）\n    依据摘要：{excerpt}"
                return line
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
        # signals_json是结构化的交易信号（标的/方向/股数/金额），之前只喂了
        # analysis_text这段散文摘要，问"具体信号是什么"答不出来——这个字段
        # 已经是结构化数据，格式化成几行文字的成本很低。
        if pf and pf.get("signals_json"):
            try:
                signals = json.loads(pf["signals_json"])
            except Exception:
                signals = []
            action_signals = [s for s in signals if s.get("action") != "不动"]
            if action_signals:
                lines = [
                    f"  {s['name']}：{s['action']} {s['shares']:g}股 · 约¥{s['amount_cny']:,.0f}"
                    for s in action_signals
                ]
                parts.append("组合分析给出的具体交易信号（仅供参考，需自己去券商手动下单）：\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        acc = tracker.get_accuracy_stats(email)
        if acc.get("总数"):
            parts.append(f"用户在\"回看\"页面自己做的个股分析：共{acc['总数']}次，方向一致率{acc['一致率']:.0f}%。")
    except Exception:
        pass

    # 用户问"AI的持仓判断/推荐股准不准"时，前面那个acc（回看页面）答的其实
    # 是另一件事（用户自己在个股详情页做的交叉验证分析），不是"AI给的买卖
    # 判断"本身准不准——两码事，之前一直被误当同一件事回答。这里补上真正
    # 对应的两个数据源：advice表（持仓/推荐股的买卖方向）事后一致率，和
    # 打分体系本身的分数-收益回测。
    try:
        adv_acc = tracker.get_advice_accuracy(email)
        by_source = adv_acc.get("按来源", {})
        lines = []
        for src, label in (("position", "持仓判断"), ("watchlist", "推荐股排行榜")):
            s = by_source.get(src)
            if s and s.get("总数"):
                lines.append(f"  {label}：{s['总数']}次，方向一致率{s['一致率']:.0f}%")
        if lines:
            parts.append("AI买卖判断的事后一致率（这才是回答\"AI推荐准不准\"该用的数据）：\n" + "\n".join(lines))
    except Exception:
        pass

    try:
        bt = tracker.get_score_band_backtest(source="watchlist")
        band_lines = [
            f"  {b['band']}分：{b['count']}条，平均涨跌{b['avg_return_pct']:+.1f}%，上涨占比{b['win_rate_pct']:.0f}%"
            for b in bt.get("bands", [])
            if b.get("avg_return_pct") is not None
        ]
        if band_lines:
            parts.append("推荐股打分体系的事后回测（分数越高是否真的表现越好）：\n" + "\n".join(band_lines))
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
