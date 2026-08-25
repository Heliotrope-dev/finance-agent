"""分析层 —— 用千问做"新闻 vs 财务数据"交叉验证，不直接下买卖结论。"""

import os

import streamlit as st
from openai import OpenAI

# 2026-08-22从DeepSeek切到千问——五个维度真实同题测过千问全面不输且更便宜，
# 详细对比记录见advisor.py同一处改动的注释，这里不重复。
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


def _stream_chat(system_prompt: str, user_content: str, max_tokens: int = 2000):
    """所有AI模块共用的流式调用——之前是等DeepSeek整段返回完了才一次性显示，
    用户反馈"一下子蹦出来"不像在实时生成。改成stream=True，一个字一个字yield出来，
    配合Streamlit的st.write_stream()用，视觉上跟打字机一样，边生成边显示。

    max_tokens给个上限，不是为了截断正常输出，是防止极端情况下模型收不住尾巴、
    生成远超提示词要求的长度，拖慢总耗时——提示词里已经写了字数要求，这里只是
    兜底不让它跑飞。

    **重要踩坑记录**：deepseek-v4-flash会在正式回答前先输出一段隐藏的思考过程
    （delta.reasoning_content，不是delta.content），这部分同样计入max_tokens。
    之前这里默认给700、部分调用点给350/180这种"刚好够正文字数"的预算，完全没
    留思考过程的余地——实测同一个请求思考过程有时候几十字，有时候能吃掉几百字，
    运气不好时思考直接把预算全部吃完，content一个字都没剩，finish_reason变成
    "length"，页面上就是"AI没有返回任何内容"（不是必现，是概率性失败，取决于
    这次模型想了多久）。改成默认2000，给思考过程留足够空间，不再靠运气。
    """
    stream = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.3,
        stream=True,
        max_tokens=max_tokens,
    )
    for chunk in stream:
        # 切到千问后实测发现的真实兼容性差异：DeepSeek的流式chunk里choices
        # 从来不会是空列表，千问偶尔会发一个choices=[]的chunk（大概率是携带
        # usage统计信息、不带实际内容的收尾chunk）——原来这里直接
        # chunk.choices[0]会在这种chunk上抛IndexError，导致整个流式生成
        # 中途崩溃、页面上的AI分析直接报错。空choices的chunk没有内容可
        # 产出，跳过即可。
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


_SYSTEM_PROMPT = """你是一个严谨的财经数据分析助手。你的任务不是给出"买/卖"建议，
而是把新闻里的说法、真实财务/行情数据、和本地算好的技术面信号做交叉核实，找出：
1. 新闻声称的内容，数据能不能支撑
2. 数据里有没有新闻没提到、但值得注意的信号——财务数据里如果出现同比大幅
   波动（不管是暴增还是暴跌，尤其三位数百分比这种），不能只报数字，要主动
   判断这更像是低基数效应/一次性损益（资产处置、政府补贴、汇兑损益、上年
   同期基数极低）导致的，还是主营业务的持续性变化——毛利率、经营现金流有没
   有同步同向变化，是可以用来交叉验证的线索。判断不出来就如实说"数据不足以
   判断这个波动是一次性还是持续性的"，不能把一个大幅波动的数字直接当成
   "基本面反转/恶化"来写，这是财务分析里最容易踩的坑。
3. 技术面信号（均线/MACD等日线级别信号，已经本地算好给你）跟消息面/基本面判断是
   一致还是背离——必须明确写一句"技术面与消息面：一致"或"技术面与消息面：出现
   背离"，并说明具体是哪里一致/背离（比如"新闻偏利好但技术面死叉，出现背离，
   需谨慎"）
4. 技术面信号里还带了一段"盘中实时信号"（现价相对今日开盘/最高/最低的位置、
   短期动量方向），这部分必须单独引用、写进分析里，不能只字不提或者一笔带过——
   用户想看的是"今天这一刻具体在发生什么"，不是只有日线级别的宏观趋势判断，
   把现价的具体数字、今日振幅位置、盘中动量方向都写进去
5. 明确列出你的判断依据（引用具体数字和新闻来源）

输出格式必须是结构清晰的中文分析，包含"新闻核实""数据信号""技术面对照（含盘中
实时信号）""不确定/需谨慎对待的点"四部分。不要输出"建议买入/卖出"这类直接指令性
结论，只呈现事实和依据，让用户自己判断。

排版要求：正文用 Markdown 加粗（**文字**）标出关键数字和关键结论性判断
（比如具体的涨跌幅、增速数字，或者"数据不支撑该说法"这类结论句），
不要整段整段地加粗，只标最核心的那几处，方便用户一眼扫到重点。

最后必须单独另起一行，输出一个机器可解析的方向倾向标签，格式严格为：
[方向倾向: 偏多] 或 [方向倾向: 偏空] 或 [方向倾向: 中性]
这个标签是给客观历史记录用的，不是投资建议，判断依据是"综合数据信号，
短期内哪个方向的证据更充分"，不确定就用"中性"，不要为了给出结论而勉强选边。

语气要求：像分析师写研判笔记一样直接说事，不要"作为一个AI"这类自我介绍开场，
不要堆砌"值得注意的是""综合来看""不难看出"这类填充语，句子要有信息量。"""

_FINANCIAL_SUMMARY_PROMPT = """你是财经数据分析助手。下面是一家上市公司的原始财务摘要表格
（营收、净利润、毛利率等指标的历史数据），请用大白话写一段简短总结（150字以内），
讲清楚：营收和利润是增长还是下滑、趋势如何、毛利率/净利率处于什么水平、有没有
明显异常的地方。不要给投资建议，只客观转述数据说明的情况。关键数字用 Markdown
加粗标出。直接说结论，不要"作为财经助手"这类开场白，不堆砌"值得注意的是"之类的填充语。"""


def cross_validate(symbol: str, history_summary: str, financial_summary: str, news_summary: str, technical_summary: str = ""):
    """把行情+财务+新闻+本地算好的技术面信号丢给 DeepSeek，产出带依据链的交叉验证分析。
    流式生成器，配合 st.write_stream() 使用。

    max_tokens=4000——"AI分析概率性返回空内容"那个老坑（见README"踩过的坑"，
    summarize_overall那个调用点也踩过一次）在这里复现了：这是所有AI模块里
    唯一要求输出"新闻核实/数据信号/技术面对照(含盘中实时信号)/不确定点"四段
    完整结构化分析、还要带方向倾向标签的调用点，正文本身就比其它模块长得多，
    之前一直留在跟summarize_financials这种150字短总结一样的默认2000上，
    隐藏的思考过程(reasoning_content)一旦吃得多，正文预算就不够写完四段，
    出现"AI没有返回任何内容"。调大到4000（比summarize_overall的3000更多，
    因为这里连正文目标长度本身也比它长，不只是思考余量）。
    """
    user_prompt = f"""股票代码：{symbol}

【近期行情摘要】
{history_summary}

【财务摘要】
{financial_summary}

【相关新闻】
{news_summary}

【本地计算的技术面信号（均线/MACD，非AI判断，仅供你核对是否与消息面一致）】
{technical_summary or "暂无技术面数据"}

请按系统提示的结构做交叉验证分析，别忘了最后的方向倾向标签。"""

    yield from _stream_chat(_SYSTEM_PROMPT, user_prompt, max_tokens=4000)


def extract_verdict(analysis_text: str) -> str:
    """从cross_validate的输出里把[方向倾向: 偏多/偏空/中性]这个标签解析出来，
    用于客观历史记录（不是投资建议，只是给回看页面用的分类标记）。
    """
    import re
    m = re.search(r"\[方向倾向[：:]\s*(偏多|偏空|中性)\]", analysis_text)
    return m.group(1) if m else "中性"


def summarize_financials(symbol: str, financial_summary: str):
    """把财务摘要那张几十行的原始表格，转成一段人话总结，摆在表格下面。流式生成器。"""
    yield from _stream_chat(
        _FINANCIAL_SUMMARY_PROMPT, f"股票代码：{symbol}\n\n财务摘要原始数据：\n{financial_summary}",
        max_tokens=1200,
    )


_NEWS_SUMMARY_PROMPT = """你是财经资讯助手。下面是跟一家上市公司相关的最新新闻/公告列表，
每条都带日期和分类。写一段有实质内容、信息密度高的总结（400字以内，不要为了凑数字硬写，
但也别过于精简——把新闻里能提取的具体信息都用上），不要只给"整体偏利好/利空"这种空泛
结论——要点名具体是哪几条新闻说了什么事（比如具体的分红金额、回购数量、业绩预告方向、
监管动作、产品/技术进展、人事变动），把新闻里提到的具体数字、时间、事件原样带出来，
按时间或主题分组说清楚，让人看完总结就知道最近发生了什么，不用再点开每条新闻。
如果多条新闻是同一类事件（比如都是股东大会相关公告），可以合并成一句带过，把篇幅留给
真正有信息量的条目。如果新闻列表明显跟这家公司关系不大（只是通用大盘资讯），要诚实
说明"没有直接相关新闻，以下是大盘概况"，不要硬扯关系。不要给投资建议。直接说结论，
不要"根据以上新闻"这类过渡句开场，不要堆砌"值得注意的是""整体来看"这类填充语。"""

_INDEX_NEWS_SUMMARY_PROMPT = """你是财经资讯助手。下面是跟一个大盘指数相关的最新新闻列表，
每条都带日期和分类。写一段有实质内容、信息密度高的总结（400字以内），按时间顺序或主题
说清楚这段时间大盘经历了什么（具体的涨跌幅数字、板块轮动、带动大盘的具体事件），不要只给
"整体偏利好/利空"这种空泛结论。指数本身就是大盘的映射，这些新闻天然就是相关的，
不要说"没有直接相关个股新闻"这种话——指数没有"个股新闻"这个概念，不需要强调这点，
直接总结大盘发生了什么就行。不要给投资建议。直接说结论，不要"根据以上新闻"这类过渡句
开场，不要堆砌"值得注意的是""整体来看"这类填充语。"""

_BENCHMARK_SUMMARY_PROMPT = """你是财经数据分析助手。下面给你一只股票和基准指数在同一段时间的
涨跌幅数据，请用一两句话（80字以内）说清楚：这只股票跑赢还是跑输了基准，差距大不大。
不要给投资建议，只客观描述数据对比结果。直接说数字和结论，不要铺垫。"""


def summarize_news(symbol: str, news_summary: str):
    """新闻资讯模块的独立AI总结，跟财务摘要/数据分析是分开的按需调用。流式生成器。"""
    yield from _stream_chat(_NEWS_SUMMARY_PROMPT, f"股票代码：{symbol}\n\n新闻列表：\n{news_summary}")


def summarize_index_news(name: str, news_summary: str):
    """指数版的新闻总结，跟summarize_news分开——指数没有"个股新闻"这个概念，
    用同一套针对个股写的提示词，AI会习惯性地说"没有直接相关个股新闻"，读起来
    很突兀也很"AI"（指数本来就该看大盘新闻，不存在"没有相关新闻"这回事）。
    流式生成器。
    """
    yield from _stream_chat(_INDEX_NEWS_SUMMARY_PROMPT, f"指数：{name}\n\n新闻列表：\n{news_summary}")


def summarize_benchmark(symbol: str, stock_pct: float, benchmark_name: str, benchmark_pct: float):
    """对比大盘模块的独立AI总结。流式生成器。

    max_tokens原来是800，比其它同类调用点（summarize_financials/analyze_index都是
    1200，summarize_overall是3000）更紧张——"AI分析概率性返回空内容"这个坑（见
    README"踩过的坑"）的本质是隐藏思考过程(reasoning_content)吃掉的预算跟正文
    目标长度无关、纯看运气，这里正文虽然只要求80字以内，但一样可能被思考过程
    吃满预算。调到跟其它短总结类调用点一致的1200，不再是全部调用点里最紧张的那个。
    """
    yield from _stream_chat(
        _BENCHMARK_SUMMARY_PROMPT,
        f"股票 {symbol} 区间涨跌幅：{stock_pct:+.2f}%\n{benchmark_name} 同期涨跌幅：{benchmark_pct:+.2f}%",
        max_tokens=1200,
    )


_INDEX_ANALYSIS_PROMPT = """你是财经数据分析助手，分析对象是一个大盘指数（不是个股，没有财务报表
这回事）。给你技术面信号（本地算好的均线/MACD，以及一段"盘中实时信号"——现价相对
今日开盘/最高/最低的位置、短期动量方向）和近期相关新闻，写一段分析（200字以内），
说清楚：技术面信号说明什么、新闻面整体偏向是什么、两者是否吻合。"盘中实时信号"部分
必须明确引用（今天现价具体多少、今日振幅位置、盘中动量方向），不能只谈日线级别的
宏观趋势，要让人看完知道"现在这一刻大盘具体在怎么走"。像写一段给同事看的研判笔记
那样直接说结论和依据，不要"作为一个AI"这类自我介绍，不要"首先...其次...最后"
这种僵硬的分段套话，也别堆砌"值得注意的是""综合来看"这类填充语，有话直说。
不给买卖建议，关键判断用 Markdown 加粗标出。"""


def analyze_index(name: str, technical_summary: str, news_summary: str):
    """指数版的综合分析——没有财务、没有个股新闻，只有技术面+大盘相关资讯两条线。流式生成器。

    这里之前是max_tokens=400，是修"AI分析概率性返回空内容"那次漏掉的一处——
    deepseek-v4-flash的隐藏思考过程(reasoning_content)跟正式回答共用同一个
    token预算，400的预算比其它几处调大过的地方（1200/800/默认2000）都更
    紧张，运气不好思考过程就把预算吃完了，正式回答一个字都没剩。这次一并
    调大，跟其它调用点统一到差不多的量级。
    """
    yield from _stream_chat(
        _INDEX_ANALYSIS_PROMPT,
        f"指数：{name}\n\n技术面信号：\n{technical_summary}\n\n相关新闻：\n{news_summary}",
        max_tokens=1200,
    )


_OVERALL_SUMMARY_PROMPT = """你是财经数据分析助手。下面是同一只标的的几段独立分析结果
（资讯解读、财务摘要、大盘对比、技术面与消息面交叉验证——不是同一个视角，是几个
分开跑的独立判断），请写一段总结性分析（150字以内），把这几条线综合起来给一个
理性、克制的整体判断：几条线的结论是互相印证还是有冲突，整体偏向是什么，最大的
不确定性在哪。不要重复罗列前面每一段说过的内容，要真正综合、提炼出更高层的判断。
不要给"买入/卖出/持有"这类操作指令。语气像分析师给同事的一句话总结，不要"综合以上
分析可以看出"这类套话开场，直接说结论。最后必须单独一行加上：本分析仅供参考，不构成投资建议。

除了文字总结，还必须单独另起一行，给一个0-100的综合评分，格式严格为：
[综合评分: 数字]
评分含义：0分=证据高度一致指向看空、风险很高，50分=中性/证据混杂对冲，
100分=证据高度一致指向看多。打分依据是"各条独立证据链的方向是否一致、
一致的强度有多大"，不是你自己对这只股票的主观看好程度——比如技术面死叉、
消息面利空、财务恶化三条线都指向看空，就该打很低的分；如果三条线互相矛盾
或者信号都很弱，就打接近50分的中性分；不要为了避免极端而习惯性打中间分，
证据确实一致的时候要敢打到20分以下或80分以上。"""


def summarize_overall(symbol: str, section_texts: dict):
    """总结性分析——把前面几个独立模块已经产出的AI文本再综合一次，不重新拉数据，
    只是站在更高层面把几条独立证据链拧成一个判断，给用户一个"看这一段就够"的收尾。
    末尾带一个机器可解析的[综合评分: 数字]标签，用 extract_score 解析出来，
    展示层面转成一个可视化打分条，比纯文字更直观。流式生成器。
    """
    # max_tokens从1200调到3000——这里是"AI分析概率性返回空内容"那个老坑
    # (见README"踩过的坑")在summarize_overall这个调用点复现了：这个函数要把
    # 前面4个模块(资讯/财务/对比大盘/交叉验证)的完整文本一起喂给模型再综合
    # 一次，输入信息密度比其它单一模块的调用点(analyze_index等)高得多，
    # 隐藏的思考过程(reasoning_content)需要的预算也相应更多，1200不够时
    # 思考过程会把预算吃完，正式回答（其实只要求150字以内）一个字都不剩。
    # 调大预算不影响正常情况下的耗时/成本——模型该多短就多短，这只是把
    # "万一想得久一点"的安全余量留够，不是把回答故意拉长。
    sections = "\n\n".join(f"【{k}】\n{v}" for k, v in section_texts.items() if v)
    yield from _stream_chat(_OVERALL_SUMMARY_PROMPT, f"标的：{symbol}\n\n{sections}", max_tokens=3000)


def extract_score(analysis_text: str) -> int | None:
    """从summarize_overall的输出里解析[综合评分: 数字]标签，解析不到返回None
    （比如AI这次没按格式输出），调用方要能处理拿不到分数、只展示文字的情况。
    """
    import re
    m = re.search(r"\[综合评分[：:]\s*(\d{1,3})\]", analysis_text)
    if not m:
        return None
    score = int(m.group(1))
    return max(0, min(100, score))
