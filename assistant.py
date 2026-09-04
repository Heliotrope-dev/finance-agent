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

# 2026-09-01切到百炼Token Plan套餐专属端点，理由同advisor.py同一处改动。
_QWEN_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
_MODEL = "qwen3.8-flash"


def get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")


@st.cache_resource(show_spinner=False)
def _client() -> OpenAI:
    """2026-09-02修复聊天卡顿：这里之前每次调用都new一个OpenAI(...)，而
    OpenAI客户端底层是httpx.Client，new一次就意味着一次全新的TCP+TLS
    握手（这个项目连的是百炼cn-beijing这个跨区端点，握手本身就有实打实
    的网络延迟）。analysis.py/advisor.py同样的写法问题不大——那两处是
    用户点一下按钮才触发一次的单轮分析，一次性的握手开销不明显；这里
    是多轮连续对话，一个会话里发10条消息就是10次重新握手，每条消息都要
    白白多等一次连接建立，这才是用户反馈"聊天卡"的真正原因，不是模型
    生成慢。用st.cache_resource把这个客户端缓存成整个app共享的单例，
    底层连接池能被keep-alive复用，同一个会话后续消息不用再重新握手。
    OpenAI/httpx客户端本身是线程安全、可并发复用的，缓存成单例没有
    每次话请求状态互相污染的风险。
    """
    key = get_secret("QWEN_API_KEY")
    if not key:
        raise RuntimeError("未配置 QWEN_API_KEY。")
    # timeout=60/max_retries=0：2026-09-02排查advisor.py那边"千问对特定
    # 持仓票的内容会挂住不返回，一等能等120秒以上"的真实故障时顺带查到，
    # 这个客户端之前也没配超时，只靠SDK默认的600秒兜底——这里是用户直接
    # 盯着看的聊天悬浮窗，真遇到同类卡住，用户会看着"思考中"转60秒都不
    # 知道还是不是坏了，比10分钟好但也不该无限等。60秒对这里max_tokens=
    # 1200的聊天场景（比advisor.py那边8000 tokens的深度判断轻得多）留了
    # 足够余量。max_retries原来是2——同一处真实踩坑：只加timeout不关掉
    # SDK自己的重试，一次60秒的挂起会被原样重试2次，用户实际要等接近
    # 180秒才会看到"回答失败"，这里一并关掉，卡住了就快速失败，好过让
    # 用户对着"思考中"干等三倍时间。
    return OpenAI(api_key=key, base_url=_QWEN_BASE, max_retries=0, timeout=60)


_SYSTEM_PROMPT_TEMPLATE = """你是"投研站"网站里的AI助手，一个小圆形按钮，用户点开
在右下角浮窗里跟你聊天。你有两块职责：一是帮用户看懂这个网站、看懂他自己的数据；
二是像一个见多识广的金融/股票分析师一样，回答任何股票、公司、行业、宏观经济、
财经新闻相关的知识性问题——不局限于这个网站收录的股票或数据，用你自己的知识
和下面提供的工具去回答，不要因为问题超出了网站数据范围就说"看不到"。这一点很
重要：用户明确要求过这个助手要"全能"，遇到网站数据里没有的股票/公司/新闻，
应该正常凭知识回答，需要更精确的实时数据时可以调用下面提供的工具查。

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
1. 涉及"用户自己在这个网站上的数据"（持仓、历史判断记录、组合分析、这个
   网站给出的打分/排行榜）时，只用下面这段"当前数据"回答，不能编造任何
   数字，这段数据里没有的如实说"这个我这边看不到"。用户说的数字跟这段
   数据对不上时（比如"我记得我还持有XX"但数据里没有），以这段数据为准
   如实说，不要顺着用户的说法改口附和。
   但这条规则只管"网站自己的数据"——用户问的是网站数据范围之外的股票/
   公司/行业/新闻/宏观知识性问题时，不受这条约束，正常凭你自己的知识
   和下面的工具回答，该给出具体分析就给，不要因为"当前数据"里没有就
   回避或说看不到。
2. 不对个股/大盘未来涨跌做预测、不给加减仓/仓位比例建议、不替用户下判断——
   不只是不说"买/卖"这两个字，"你觉得我该不该加仓""明天大概率涨还是跌"
   这类问法本质也是要一个指令性结论，同样要回避，最多说"网站上AI给这支
   股票的参考判断是XX"，把已有的参考信息给出来，不重新下一个新结论，
   最终决定权归用户自己。这条对任何股票都成立，不只是网站收录的那些——
   知识性问题（这家公司是做什么的、最近财报怎么样、这个行业趋势如何）
   放开答，"买不买/该不该加仓"这类指令性结论一律回避。
3. 回答要简短直接，像聊天，不要长篇大论、不要"作为AI助手"这类开场白。
4. 用户问的如果跟这个网站完全无关（比如闲聊、其它话题），可以正常聊，
   不用生硬地把话题拉回来。
5. 下面的数据是打开这次对话那一刻查到的快照，之后不会自动刷新。如果用户
   问的是"现在/最新"的数字，且这次对话已经聊了一段时间，提醒一句这是
   刚打开时的快照，建议去对应页面看最新数据，不要让用户误以为是实时的。
   这类提醒放在回答的最后一句，不要开头第一句话就是提醒/免责声明——
   用户是来听答案的，不是来看声明的，先给内容再补充说明。
6. 游客（没登录）看不到持仓/历史记录，但首页排行榜、三个市场指数这类公开
   数据照样能聊——遇到游客主动问"你都知道什么"，可以提这些公开数据，
   不用被动等游客先问持仓再拒绝。
7. 下面提供了几个工具，可以查任意股票（不限于网站收录的）的实时行情、
   财务摘要、相关新闻——遇到需要具体数字/最新消息的问题，该调用就调用，
   不要凭空编数字。工具查不到时如实说查不到，不要编。你自己的知识本身
   有训练数据截止时间，聊到"最近""现在"这类时效性话题、且没有工具能查到
   更新数据时，提醒一句这可能不是最新情况，不要把旧知识当成当前事实
   笃定地讲。
8. 回复里不要出现任何emoji/表情符号（包括提醒/警告类的⚠️这种），这是
   整个网站的硬性规则，用文字表达语气就够，不用符号装饰。
9. 用户问"为什么XX股票没上首页推荐/排行榜"这类问题时，第一步必须先调用
   check_watchlist_pool工具确认它今天在不在候选池里，把这个结果放在
   回答最前面——"没进池子"（当天不够热门，压根没被打分）和"进了池子但
   分数不够"是完全不同的两种原因，答案要先说清楚是哪一种，再展开分析
   基本面/估值这些细节，不要跳过这一步直接讲一通独立的个股分析。

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
    #
    # 2026-08-30修复：原来这里自己开线程池并发查三个市场（8秒截止），
    # 跟首页地图/"行情"tab查的是同一份数据，三处各自另开一条查询路径纯属
    # 重复劳动。现在改成优先读warm_home_cache.py每分钟预热好的共享缓存
    # （load_home_map_cache），三处共用同一份，AI浮窗打开时这里基本秒读，
    # 不用再等最多8秒。缓存没有/太旧（预热脚本没跑上）才退回原来那套
    # 并发查询兜底，行为保持不变，只是从"每次都做"变成"偶尔才需要做"。
    try:
        idx_lines = []
        cached = ds.load_home_map_cache(max_age_sec=90)
        if cached:
            for market, rows in cached["snaps"].items():
                for row in rows:
                    idx_lines.append(
                        f"  {row.get('名称')}（{market}）最新{row.get('最新')}，涨跌幅{row.get('涨跌幅'):.2f}%"
                    )
        else:
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
        # market_quota跟app.py首页那份排行榜保持一致，不然AI聊天里说的
        # 榜单跟用户在网页上看到的对不上。
        board = tracker.get_latest_leaderboard(limit=5, source="watchlist", market_quota={"US": 3, "HK": 2})
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
                    # 200->140：这段是多行的打分明细+基本面长文，每支持仓都带
                    # 200字，持仓一多上下文就撑爆了。压缩换行后取前140字，
                    # 保留结论和最关键的那句，细节让它用工具现查。
                    excerpt = " ".join((adv.get("fundamental_verdict") or "").split())[:140]
                    line += f"\n    AI最新参考：{adv['action']}（{adv.get('created_at', '')[:10]}）\n    依据摘要：{excerpt}"
                return line
            parts.append("用户当前持仓：\n" + "\n".join(_fmt_pos(p) for p in held))
        else:
            parts.append("用户当前没有持仓记录。")

        # 自选（shares<=0，只关注不持仓）。2026-09-04补：之前这里只取
        # shares>0 的真实持仓，自选整个看不见——而这个用户恰好持仓为0、
        # 自选21支，等于助手眼里"这个人没有任何数据"，问"我关注了哪些票"
        # 只能答不知道。自选是这个人主动挑出来的标的，比持仓更能说明他在
        # 看什么，必须给。
        watch = [p for p in positions if (p.get("shares") or 0) <= 0]
        if watch:
            parts.append(
                f"用户的自选（只关注、未持仓，共{len(watch)}支）：\n"
                + "\n".join(
                    f"  {p.get('name') or p.get('symbol')}（{p.get('symbol')}·{p.get('market')}）"
                    + (f" AI最新参考：{pos_advice[p['symbol']]['action']}"
                       if pos_advice.get(p.get("symbol")) else "")
                    for p in watch[:30]
                )
            )
    except Exception:
        pass

    # ── AI模拟盘全景 ────────────────────────────────────────────────────
    # 2026-09-04新增。这是整个网站最活跃的一块（每5分钟一次自主决策、有真实
    # 下单记录和收益曲线），但助手此前对它一无所知——用户问"模拟盘现在多少钱"
    # "刚才为什么买那支"，只能答不知道，而这些数据就在同一个库里。
    # 只取摘要+最近几条，不把三十轮决策全塞进来。
    try:
        import sim_agent as _sa
        _sim_email = _sa.advisor._EMAIL
        cash = tracker.get_sim_virtual_cash(_sim_email)
        snaps = tracker.get_equity_snapshots(_sim_email, limit=1)
        # 优先取真正跑完决策的轮次。非开盘时段会连续记很多条"跳过：当前没有
        # 市场开盘"，直接取最近5条的话夜里问它"模拟盘最近在干嘛"，答的全是
        # 五条跳过，一点信息量都没有。
        _all_runs = tracker.get_sim_agent_runs(_sim_email, limit=30)
        runs = [r for r in _all_runs if r.get("status") != "跳过"][:5] or _all_runs[:2]
        sim_lines = []
        if cash is not None:
            usd = cash / 7.8
            sim_lines.append(f"  虚拟现金约 ${usd:,.0f}（内部按港币记账 HK${cash:,.0f}）")
        if snaps:
            _s0 = snaps[0]
            _net = (_s0.get("net_value_hkd") or 0) / 7.8
            sim_lines.append(
                f"  最近一次资产快照：总额约 ${_net:,.0f}（起始本金 $10,000）"
            )
        if runs:
            sim_lines.append("  最近几次决策：")
            for r in runs:
                _rt = (r.get("reasoning_text") or "").replace("\n", " ")[:120]
                sim_lines.append(
                    f"    {(r.get('run_at') or '')[:16]} {r.get('status')} · {r.get('note','')}"
                    + (f"\n      理由摘要：{_rt}" if _rt else "")
                )
        lessons = tracker.get_sim_agent_lessons(_sim_email, limit=3)
        if lessons:
            sim_lines.append("  跨天复盘经验：")
            for l in lessons:
                sim_lines.append(f"    {(l.get('lesson_text') or '')[:120]}")
        if sim_lines:
            parts.append(
                "AI模拟炒股盘的实时状态（内置AI用虚拟资金自主交易港股/美股，"
                "开盘时段每5分钟决策一次，跟用户自己的持仓是两回事，别混为一谈）：\n"
                + "\n".join(sim_lines)
            )
    except Exception:
        pass

    # ── 用户自己的使用记录 ──────────────────────────────────────────────
    try:
        ov = tracker.get_user_overview(email)
        _bits = [
            f"自选{ov.get('watch_count', 0)}支", f"持仓{ov.get('hold_count', 0)}支",
            f"累计生成过{ov.get('analysis_count', 0)}次个股AI分析",
            f"搜索过{ov.get('search_count', 0)}次",
        ]
        if ov.get("first_seen"):
            _bits.append(f"最早记录在{ov['first_seen'][:10]}")
        parts.append("用户的使用概况：" + "，".join(_bits) + "。")
    except Exception:
        pass

    try:
        hist = tracker.get_search_history(email, limit=8)
        if hist:
            parts.append(
                "用户最近搜索过：" + "、".join(
                    f"{h.get('query')}（{h.get('market')}）" for h in hist
                )
            )
    except Exception:
        pass

    try:
        seen = tracker.get_history(email, limit=8)
        if seen:
            parts.append(
                "用户最近看过并生成过AI分析的标的（含当时的方向判断）：\n"
                + "\n".join(
                    f"  {h.get('name') or h.get('symbol')}（{h.get('symbol')}）"
                    f" {(h.get('created_at') or '')[:10]} 判断：{h.get('verdict') or '—'}"
                    + (f"，综合得分{h['score']}" if h.get("score") is not None else "")
                    for h in seen
                )
            )
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


# 2026-08-30新增：用户明确要求内置AI要像"全能金融专家"，不局限于网站首页
# 排行榜/用户自己持仓里已经收录的那几十支股票——build_context只在打开
# 浮窗那一刻查一次快照，覆盖不到用户聊天中途随口问的任意股票。给千问接
# 几个轻量工具，让它自己判断要不要查、查哪支，而不是把"全市场所有股票的
# 全部数据"提前塞进上下文（那样prompt会大到不现实，而且大部分用户根本
# 不会问到）。只做一轮工具调用（问一次、查、再回答），不做多轮agentic
# 循环——聊天场景要的是"快且够用"，不是"无限深挖"。
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_quote",
            "description": "查任意一支股票的实时行情快照（最新价/涨跌幅/今开/昨收/最高/最低），不限于网站首页收录的股票。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，比如TSLA、00700、600519"},
                    "market": {"type": "string", "enum": ["A", "HK", "US"], "description": "所属市场：A股/港股/美股"},
                },
                "required": ["symbol", "market"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_financials",
            "description": "查任意一支股票的财务摘要指标（营收/利润/负债率等），不限于网站首页收录的股票。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "enum": ["A", "HK", "US"], "description": "所属市场：A股/港股/美股"},
                },
                "required": ["symbol", "market"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_stock_news",
            "description": "按关键词（公司名/股票代码/行业词）搜真实新闻，回答"
            "最近有什么消息/新闻这类问题时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，比如公司名或行业词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_valuation_percentile",
            "description": "查一支股票当前PE/PB相对自己过去三年历史区间处于什么分位（只支持A股/港股）——"
            "回答“现在贵不贵/便宜不便宜”这类估值问题时用，比只看静态PE倍数更有依据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "enum": ["A", "HK"], "description": "只支持A股/港股"},
                },
                "required": ["symbol", "market"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_watchlist_pool",
            "description": "查一支股票今天在不在网站首页'推荐股排行榜'的每日观察池里，"
            "在的话给出真实得分/排名。用户问'为什么XX没上首页推荐/排行榜'这类问题时，"
            "必须先调这个工具确认它今天有没有进入候选池，不要跳过这一步直接凭自己知识分析——"
            "观察池只挑当天最热门的约120支股票，很多基本面扎实的大盘股大部分交易日热度"
            "排不进这个池子，那是'没资格参赛'，跟'进了池子但分数不够'是两个性质完全不同的"
            "原因，答案的第一句话就该说清楚是哪一种。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                },
                "required": ["symbol"],
            },
        },
    },
]


def _execute_tool(name: str, args: dict) -> str:
    """实际执行工具调用，统一吞异常返回文字说明（不让单个工具失败打断整轮
    对话）——跟这个项目一贯的"取不到就如实说取不到，不硬凑"原则一致。
    """
    import data_sources as ds

    try:
        if name == "get_stock_quote":
            spot = ds.get_stock_realtime(args["symbol"], market=args.get("market", "US"))
            if not spot:
                return "查不到这支股票的实时行情，可能代码或市场填错了。"
            return "，".join(f"{k}：{v}" for k, v in spot.items() if v is not None)
        if name == "get_stock_financials":
            fin = ds.get_financial_abstract(args["symbol"], market=args.get("market", "A"))
            if fin is None or fin.empty:
                return "查不到这支股票的财务摘要数据。"
            return fin.head(10).to_string(index=False)
        if name == "search_stock_news":
            news = ds.get_futu_news(args["keyword"], max_count=5)
            if news is None or news.empty:
                return "没搜到相关新闻。"
            lines = [f"{r['日期']} {r['新闻标题']}" for _, r in news.iterrows()]
            return "\n".join(lines)
        if name == "get_valuation_percentile":
            pct = ds.get_valuation_percentile(args["symbol"], market=args.get("market", "A"))
            if not pct:
                return "查不到这支股票的估值历史分位数据。"
            return "，".join(f"{k}：{v}" for k, v in pct.items() if v is not None)
        if name == "check_watchlist_pool":
            import tracker

            v = tracker.get_watchlist_verdict_for_symbol(args["symbol"])
            if not v.get("run_date"):
                return "网站的每日观察池还没有任何历史数据。"
            if not v["in_pool"]:
                return (
                    f"这支股票不在{v['run_date']}这天的观察池里（当天池子共{v['pool_size']}支股票，"
                    "是从全市场当天最热门约120支股票里选的）。不在池子里通常是因为这支股票当天"
                    "热度/涨跌幅没排进前列，不是因为AI给它打了低分——它压根没被打分。"
                )
            return (
                f"{v['run_date']}这天在池子里，共{v['pool_size']}支，排名第{v['rank']}，"
                f"得分{v['score']}分，AI给出的操作建议是{v['action']}。"
                f"判断依据摘要：{v['verdict_excerpt']}"
            )
        return f"未知工具：{name}"
    except Exception as e:
        return f"查询失败：{e}"


def _stream_and_collect(messages: list[dict], max_tokens: int):
    """跑一次流式请求，边收边yield文字增量，同时把可能出现的tool_calls
    分片累积起来，生成器结束时return——调用方用`result = yield from
    _stream_and_collect(...)`拿这个返回值，这是Python生成器的标准用法
    （PEP 380），不是挂在函数对象上的全局属性：如果用函数属性存"上一次
    结果"，多个用户会话并发调用这同一个模块级函数时会互相踩到对方的
    结果，那是真的会出错的写法，这里特意避开。
    """
    stream = _client().chat.completions.create(
        model=_MODEL, messages=messages, temperature=0.4, tools=_TOOLS, stream=True, max_tokens=max_tokens,
    )
    tool_calls_acc: dict[int, dict] = {}
    content_acc = ""
    for chunk in stream:
        # 千问偶尔发不带内容的收尾chunk（choices=[]），见analysis._stream_chat
        # 同一处踩坑记录，这里同样处理。
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content_acc += delta.content
            yield delta.content
        # 流式返回的tool_calls是分片的：第一个分片带id/name，后续分片只带
        # 这一片的arguments字符串，要按index累加拼起来才是完整的JSON参数——
        # 这是OpenAI兼容协议流式工具调用的标准形状，不是千问特有行为。
        if delta.tool_calls:
            for tc in delta.tool_calls:
                slot = tool_calls_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["arguments"] += tc.function.arguments
    return {"content": content_acc, "tool_calls": tool_calls_acc}


def stream_reply(messages: list[dict], context: str, max_tokens: int = 1200):
    """messages：不含system，只有[{"role":"user"/"assistant","content":...}, ...]
    的对话历史（含本轮最新一条用户消息）。max_tokens给1200——聊天场景要求
    "简短直接"，不需要像交叉验证分析那样留几千字的预算。

    2026-08-30重写：原来是先做一轮不流式的探测请求判断要不要调用工具，
    探测本身实测要4-10秒，这段时间界面上什么都不出，用户反馈"跟卡死一样"。
    改成直接发一次带tools的流式请求——多数问题模型不需要工具，会直接
    一边生成一边吐字，第一个字出来的时间跟完全不接工具那版一样快；
    真要调用工具时，流里不会有正文只有分片的tool_calls，累积到流结束后
    再执行工具、发第二次流式请求要最终答案。只做一轮工具调用（不允许
    模型拿到结果后又申请再查一轮），避免聊天场景被拖成多轮agentic循环。
    """
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(context=context)
    base_messages = [{"role": "system", "content": system_prompt}] + messages

    first = yield from _stream_and_collect(base_messages, max_tokens)
    if not first["tool_calls"]:
        return

    # tool_calls_acc是按index累积的分片字典，转成发请求要用的标准格式。
    tool_calls_list = [
        {"id": slot["id"], "type": "function", "function": {"name": slot["name"], "arguments": slot["arguments"]}}
        for slot in first["tool_calls"].values()
    ]
    tool_msgs = [{"role": "assistant", "content": first["content"] or "", "tool_calls": tool_calls_list}]
    for call in tool_calls_list[:4]:  # 单轮最多执行4个工具调用，避免模型一次申请一大堆查询拖慢响应
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except Exception:
            args = {}
        result = _execute_tool(call["function"]["name"], args)
        tool_msgs.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    stream = _client().chat.completions.create(
        model=_MODEL,
        messages=base_messages + tool_msgs,
        temperature=0.4,
        stream=True,
        max_tokens=max_tokens,
    )
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
