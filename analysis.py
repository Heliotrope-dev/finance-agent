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
而是把新闻里的说法和真实财务/行情数据做交叉核实，找出：
1. 新闻声称的内容，数据能不能支撑
2. 数据里有没有新闻没提到、但值得注意的信号
3. 明确列出你的判断依据（引用具体数字和新闻来源）

输出格式必须是结构清晰的中文分析，包含"新闻核实""数据信号""不确定/需谨慎对待的点"
三部分。不要输出"建议买入/卖出"这类直接指令性结论，只呈现事实和依据，让用户自己判断。

排版要求：正文用 Markdown 加粗（**文字**）标出关键数字和关键结论性判断
（比如具体的涨跌幅、增速数字，或者"数据不支撑该说法"这类结论句），
不要整段整段地加粗，只标最核心的那几处，方便用户一眼扫到重点。"""

_FINANCIAL_SUMMARY_PROMPT = """你是财经数据分析助手。下面是一家上市公司的原始财务摘要表格
（营收、净利润、毛利率等指标的历史数据），请用大白话写一段简短总结（150字以内），
讲清楚：营收和利润是增长还是下滑、趋势如何、毛利率/净利率处于什么水平、有没有
明显异常的地方。不要给投资建议，只客观转述数据说明的情况。关键数字用 Markdown
加粗标出。"""


def cross_validate(symbol: str, history_summary: str, financial_summary: str, news_summary: str) -> str:
    """把行情+财务+新闻丢给 DeepSeek，产出带依据链的交叉验证分析。"""
    user_prompt = f"""股票代码：{symbol}

【近期行情摘要】
{history_summary}

【财务摘要】
{financial_summary}

【相关新闻】
{news_summary}

请按系统提示的三段式结构做交叉验证分析。"""

    resp = _client().chat.completions.create(
        model=_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    return resp.choices[0].message.content


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
