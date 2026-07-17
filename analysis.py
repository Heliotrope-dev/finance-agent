"""分析层 —— 用 DeepSeek 做"新闻 vs 财务数据"交叉验证，不直接下买卖结论。"""

import os

import streamlit as st
from openai import OpenAI

_DEEPSEEK_BASE = "https://api.deepseek.com"
_MODEL = "deepseek-v4-flash"


def get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")


def _client() -> OpenAI:
    key = get_secret("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY。")
    return OpenAI(api_key=key, base_url=_DEEPSEEK_BASE, max_retries=2)


_SYSTEM_PROMPT = """你是一个严谨的财经数据分析助手。你的任务不是给出"买/卖"建议，
而是把新闻里的说法、真实财务/行情数据、和本地算好的技术面信号做交叉核实，找出：
1. 新闻声称的内容，数据能不能支撑
2. 数据里有没有新闻没提到、但值得注意的信号
3. 技术面信号（均线/MACD，已经本地算好给你）跟消息面/基本面判断是一致还是背离——
   必须明确写一句"技术面与消息面：一致"或"技术面与消息面：出现背离"，并说明具体
   是哪里一致/背离（比如"新闻偏利好但技术面死叉，出现背离，需谨慎"）
4. 明确列出你的判断依据（引用具体数字和新闻来源）

输出格式必须是结构清晰的中文分析，包含"新闻核实""数据信号""技术面对照""不确定/
需谨慎对待的点"四部分。不要输出"建议买入/卖出"这类直接指令性结论，只呈现事实和
依据，让用户自己判断。

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


def cross_validate(symbol: str, history_summary: str, financial_summary: str, news_summary: str, technical_summary: str = "") -> str:
    """把行情+财务+新闻+本地算好的技术面信号丢给 DeepSeek，产出带依据链的交叉验证分析。"""
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

    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


def extract_verdict(analysis_text: str) -> str:
    """从cross_validate的输出里把[方向倾向: 偏多/偏空/中性]这个标签解析出来，
    用于客观历史记录（不是投资建议，只是给回看页面用的分类标记）。
    """
    import re
    m = re.search(r"\[方向倾向[：:]\s*(偏多|偏空|中性)\]", analysis_text)
    return m.group(1) if m else "中性"


def summarize_financials(symbol: str, financial_summary: str) -> str:
    """把财务摘要那张几十行的原始表格，转成一段人话总结，摆在表格下面。"""
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _FINANCIAL_SUMMARY_PROMPT},
            {"role": "user", "content": f"股票代码：{symbol}\n\n财务摘要原始数据：\n{financial_summary}"},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


_NEWS_SUMMARY_PROMPT = """你是财经资讯助手。下面是跟一家上市公司相关（或市场大盘相关）的
最新新闻列表，请用大白话写一段简短总结（150字以内），讲清楚这些新闻整体上偏利好还是
利空、有没有值得关注的具体事件。如果新闻列表明显跟这家公司关系不大（只是通用大盘资讯），
要诚实说明"没有直接相关新闻，以下是大盘概况"，不要硬扯关系。不要给投资建议。
直接说结论，不要"根据以上新闻"这类过渡句开场。"""

_BENCHMARK_SUMMARY_PROMPT = """你是财经数据分析助手。下面给你一只股票和基准指数在同一段时间的
涨跌幅数据，请用一两句话（80字以内）说清楚：这只股票跑赢还是跑输了基准，差距大不大。
不要给投资建议，只客观描述数据对比结果。直接说数字和结论，不要铺垫。"""


def summarize_news(symbol: str, news_summary: str) -> str:
    """新闻资讯模块的独立AI总结，跟财务摘要/数据分析是分开的按需调用。"""
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _NEWS_SUMMARY_PROMPT},
            {"role": "user", "content": f"股票代码：{symbol}\n\n新闻列表：\n{news_summary}"},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


def summarize_benchmark(symbol: str, stock_pct: float, benchmark_name: str, benchmark_pct: float) -> str:
    """对比大盘模块的独立AI总结。"""
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _BENCHMARK_SUMMARY_PROMPT},
            {
                "role": "user",
                "content": f"股票 {symbol} 区间涨跌幅：{stock_pct:+.2f}%\n{benchmark_name} 同期涨跌幅：{benchmark_pct:+.2f}%",
            },
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


_INDEX_ANALYSIS_PROMPT = """你是财经数据分析助手，分析对象是一个大盘指数（不是个股，没有财务报表
这回事）。给你技术面信号（本地算好的均线/MACD）和近期相关新闻，写一段分析（200字以内），
说清楚：技术面信号说明什么、新闻面整体偏向是什么、两者是否吻合。像写一段给同事看的
研判笔记那样直接说结论和依据，不要"作为一个AI"这类自我介绍，不要"首先...其次...最后"
这种僵硬的分段套话，也别堆砌"值得注意的是""综合来看"这类填充语，有话直说。
不给买卖建议，关键判断用 Markdown 加粗标出。"""


def analyze_index(name: str, technical_summary: str, news_summary: str) -> str:
    """指数版的综合分析——没有财务、没有个股新闻，只有技术面+大盘相关资讯两条线。"""
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _INDEX_ANALYSIS_PROMPT},
            {
                "role": "user",
                "content": f"指数：{name}\n\n技术面信号：\n{technical_summary}\n\n相关新闻：\n{news_summary}",
            },
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


_OVERALL_SUMMARY_PROMPT = """你是财经数据分析助手。下面是同一只标的的几段独立分析结果
（资讯解读、财务摘要、大盘对比、技术面与消息面交叉验证——不是同一个视角，是几个
分开跑的独立判断），请写一段总结性分析（150字以内），把这几条线综合起来给一个
理性、克制的整体判断：几条线的结论是互相印证还是有冲突，整体偏向是什么，最大的
不确定性在哪。不要重复罗列前面每一段说过的内容，要真正综合、提炼出更高层的判断。
不要给"买入/卖出/持有"这类操作指令。语气像分析师给同事的一句话总结，不要"综合以上
分析可以看出"这类套话开场，直接说结论。最后必须单独一行加上：本分析仅供参考，不构成投资建议。"""


def summarize_overall(symbol: str, section_texts: dict) -> str:
    """总结性分析——把前面几个独立模块已经产出的AI文本再综合一次，不重新拉数据，
    只是站在更高层面把几条独立证据链拧成一个判断，给用户一个"看这一段就够"的收尾。
    """
    sections = "\n\n".join(f"【{k}】\n{v}" for k, v in section_texts.items() if v)
    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _OVERALL_SUMMARY_PROMPT},
            {"role": "user", "content": f"标的：{symbol}\n\n{sections}"},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content
