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
三部分。不要输出"建议买入/卖出"这类直接指令性结论，只呈现事实和依据，让用户自己判断。"""


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
