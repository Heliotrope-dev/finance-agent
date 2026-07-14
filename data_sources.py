"""数据层 —— 封装 AkShare 调用，统一加重试/限流间隔/缓存。"""

import json
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd
import requests
import streamlit as st

# 给所有出站 HTTP 请求（包括 akshare 内部）打上 15 秒硬超时，防止页面永久 loading。
_original_session_request = requests.Session.request


def _session_request_with_timeout(self, method, url, **kwargs):
    kwargs.setdefault("timeout", 15)
    return _original_session_request(self, method, url, **kwargs)


requests.Session.request = _session_request_with_timeout

_MIN_INTERVAL_SEC = 3  # 东财接口对高频请求会临时封IP，两次请求之间留够间隔
_last_call_ts = 0.0


def _throttle():
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - elapsed)
    _last_call_ts = time.time()


def _with_retry(fn, retries=2, backoff=5):
    last_err = None
    for attempt in range(retries + 1):
        try:
            _throttle()
            return fn()
        except Exception as e:  # noqa: BLE001 — 数据源异常统一兜底重试
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_err


def _sina_symbol(symbol: str) -> str:
    """AkShare 东财接口用纯数字代码，新浪接口要带交易所前缀。"""
    prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
    return f"{prefix}{symbol}"


def _fetch_history_sina(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """新浪源兜底，列名对齐东财源，让上层（charts.py/app.py）不用关心具体来源。"""
    df = ak.stock_zh_a_daily(symbol=_sina_symbol(symbol), start_date=start_date, end_date=end_date)
    df = df.rename(
        columns={
            "date": "日期",
            "open": "开盘",
            "high": "最高",
            "low": "最低",
            "close": "收盘",
            "volume": "成交量",
        }
    )
    df["日期"] = pd.to_datetime(df["日期"])
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_history(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """日线历史行情。symbol 例如 '600519'。东财限流/超时时自动切新浪源兜底。"""
    try:
        return _with_retry(
            lambda: ak.stock_zh_a_hist(
                symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq"
            )
        )
    except Exception:
        return _with_retry(lambda: _fetch_history_sina(symbol, start_date, end_date))


@st.cache_data(ttl=120, show_spinner=False)
def get_stock_realtime(symbol: str) -> dict:
    """单只股票的最新价，直接复用 get_stock_history 的最后一行，不额外发请求。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
    df = get_stock_history(symbol, start, end)
    if df is None or df.empty:
        return {}
    row = df.iloc[-1]
    return {"代码": symbol, "最新价": row["收盘"]}


@st.cache_data(ttl=600, show_spinner=False)
def get_financial_abstract(symbol: str) -> pd.DataFrame:
    """财务摘要指标。"""
    return _with_retry(lambda: ak.stock_financial_abstract(symbol=symbol))


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_news(symbol: str, limit: int = 10) -> pd.DataFrame:
    """个股新闻 —— 自己实现而非直接调 ak.stock_news_em。

    原因：akshare 1.18.64 的 stock_news_em 在清洗文本时用
    `.str.replace(r"\\u3000", "", regex=True)`，在本环境的 pandas/pyarrow
    字符串后端下会报 "invalid escape sequence" 崩掉。这里复用它的请求逻辑，
    把两处清洗换成普通字符串替换（不用正则），绕开这个上游 bug。
    """

    def _fetch():
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_param = {
            "uid": "",
            "keyword": symbol,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        params = {
            "cb": "jQuery0_0",
            "param": json.dumps(inner_param, ensure_ascii=False),
            "_": str(int(time.time() * 1000)),
        }
        headers = {
            "accept": "*/*",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "referer": f"https://so.eastmoney.com/news/s?keyword={symbol}",
        }
        r = requests.get(url, params=params, headers=headers, timeout=15)
        text = r.text.strip()
        json_str = text[text.index("(") + 1 : text.rindex(")")]
        data = json.loads(json_str)
        items = data.get("result", {}).get("cmsArticleWebOld", [])
        df = pd.DataFrame(items)
        if df.empty:
            return df
        df = df.rename(
            columns={
                "title": "新闻标题",
                "content": "新闻内容",
                "date": "发布时间",
                "mediaName": "文章来源",
                "code": "url_code",
            }
        )
        for col in ("新闻标题", "新闻内容"):
            if col in df.columns:
                df[col] = (
                    df[col]
                    .str.replace("<em>", "", regex=False)
                    .str.replace("</em>", "", regex=False)
                    .str.replace("　", "", regex=False)
                    .str.replace("\r\n", " ", regex=False)
                )
        keep = [c for c in ("新闻标题", "新闻内容", "发布时间", "文章来源") if c in df.columns]
        return df[keep]

    return _with_retry(_fetch)


@st.cache_data(ttl=300, show_spinner=False)
def get_market_news() -> pd.DataFrame:
    """大盘/宏观资讯，补充个股新闻覆盖不到的面。"""
    return _with_retry(ak.stock_news_main_cx)
