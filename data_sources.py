"""数据层 —— 封装 AkShare 调用，统一加重试/限流间隔/缓存。"""

import contextlib
import io
import time
from datetime import datetime, timedelta

import akshare as ak
import baostock as bs
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
    """AkShare 东财接口用纯数字代码，新浪/BaoStock 接口要带交易所前缀。"""
    return "sh" if symbol.startswith(("6", "9")) else "sz"


@st.cache_data(ttl=3600, show_spinner=False)
def search_stock_by_name(query: str) -> list[dict]:
    """按名称（支持模糊匹配）搜个股代码。只返回股票（排除指数/基金），只返回在市的。"""
    query = query.strip()
    if not query:
        return []
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        try:
            rs = bs.query_stock_basic(code_name=query)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    results = []
    for code, name, ipo_date, out_date, type_, status in rows:
        if type_ == "1" and status == "1":  # 1=股票, status 1=在市
            results.append({"code": code.split(".")[1], "name": name})
    return results


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_name(symbol: str) -> str:
    """代码反查公司名，主要给新闻搜索用（新闻搜代码基本搜不到东西）。查不到就退回代码本身。"""
    info = _stock_basic_info(symbol)
    return info[1] if info else symbol


@st.cache_data(ttl=3600, show_spinner=False)
def check_stock_valid(symbol: str) -> tuple[bool, str]:
    """输入的是6位代码时用——检查是不是真实存在、还在交易的股票。

    600001 这种代码格式完全合法，但公司早就退市了（比如邯郸钢铁，2009年退市），
    直接拿去查行情三个数据源当然都查不到，之前的报错说"稍后再试"容易误导人
    以为是临时故障——这里提前判断清楚，返回准确原因。
    """
    info = _stock_basic_info(symbol)
    if not info:
        return False, f"没有找到代码「{symbol}」对应的股票，检查一下是不是输错了。"
    _, name, _ipo, out_date, type_, status = info
    if type_ != "1":
        return False, f"「{symbol}」不是个股（可能是指数/基金/其他），暂不支持分析。"
    if status != "1":
        return False, f"「{name}」（{symbol}）已经退市了（退市日期 {out_date or '未知'}），查不到行情数据。"
    return True, name


def _stock_basic_info(symbol: str) -> tuple | None:
    bs_code = f"{_sina_symbol(symbol)}.{symbol}"
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        try:
            rs = bs.query_stock_basic(code=bs_code)
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    return rows[0] if rows else None


_INTRADAY_FREQS = {"5", "15", "30", "60"}


def _fetch_history_baostock(symbol: str, start_date: str, end_date: str, frequency: str = "d") -> pd.DataFrame:
    """BaoStock 是主数据源：官方维护、不用注册、成功率明显高于爬网页的东财/新浪源。

    frequency: d=日K, w=周K, m=月K, 5/15/30/60=分钟K（BaoStock 原生支持，
    分钟级数据自带 time 字段，用它拼出真正的时间点而不是只有日期）。
    """
    bs_code = f"{_sina_symbol(symbol)}.{symbol}"
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    is_intraday = frequency in _INTRADAY_FREQS
    fields = "date,time,open,high,low,close,volume" if is_intraday else "date,open,high,low,close,volume"

    with contextlib.redirect_stdout(io.StringIO()):  # 屏蔽 baostock 自带的 login/logout 打印
        bs.login()
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, fields, start_date=start, end_date=end,
                frequency=frequency, adjustflag="3",
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()

    cols = (["日期", "时间", "开盘", "最高", "最低", "收盘", "成交量"] if is_intraday
            else ["日期", "开盘", "最高", "最低", "收盘", "成交量"])
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    if is_intraday:
        # time 字段形如 20260714093500000（YYYYMMDDHHMMSSmmm），拼出真正的时间点
        df["日期"] = pd.to_datetime(df["时间"].str[:14], format="%Y%m%d%H%M%S")
        df = df.drop(columns=["时间"])
    else:
        df["日期"] = pd.to_datetime(df["日期"])
    for col in ("开盘", "最高", "最低", "收盘", "成交量"):
        df[col] = pd.to_numeric(df[col])
    return df


def _fetch_history_sina(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """新浪源，第二层兜底。列名对齐主数据源，让上层不用关心具体来源。"""
    df = ak.stock_zh_a_daily(
        symbol=f"{_sina_symbol(symbol)}{symbol}", start_date=start_date, end_date=end_date
    )
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
def get_benchmark_history(start_date: str, end_date: str, index_code: str = "sh.000300") -> pd.DataFrame:
    """基准指数历史收盘价，默认沪深300，用于跟个股走势对比。"""
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
        try:
            rs = bs.query_history_k_data_plus(
                index_code, "date,close", start_date=start, end_date=end, frequency="d"
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock: {rs.error_msg}")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
    df = pd.DataFrame(rows, columns=["日期", "收盘"])
    df["日期"] = pd.to_datetime(df["日期"])
    df["收盘"] = pd.to_numeric(df["收盘"])
    return df


def _fetch_history_hk(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """港股日线，新浪源。stock_hk_daily 不接受日期范围参数，返回全部历史，本地按日期筛。"""
    df = ak.stock_hk_daily(symbol=symbol, adjust="")
    df = df.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
        "close": "收盘", "volume": "成交量",
    })
    df["日期"] = pd.to_datetime(df["日期"])
    start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
    df = df[(df["日期"] >= start) & (df["日期"] <= end)]
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


def _fetch_history_us(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """美股日线，新浪源。同样返回全部历史，本地按日期筛。"""
    df = ak.stock_us_daily(symbol=symbol, adjust="")
    df = df.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
        "close": "收盘", "volume": "成交量",
    })
    df["日期"] = pd.to_datetime(df["日期"])
    start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
    df = df[(df["日期"] >= start) & (df["日期"] <= end)]
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_history(symbol: str, start_date: str, end_date: str, frequency: str = "d", market: str = "A") -> pd.DataFrame:
    """历史行情。symbol：A股例如'600519'，港股例如'00700'，美股例如'AAPL'。

    market: A=沪深A股（默认）, HK=港股, US=美股。
    A股：三层兜底 BaoStock（主，稳定免注册）→ 东财 → 新浪，frequency 支持 d/w/m/5/15/30/60。
    港股/美股：BaoStock不支持这两个市场，只能走新浪源，目前只支持日K
    （frequency 参数对港美股暂时无效，周K/月K/分钟K后续再补）。
    """
    if market == "HK":
        return _with_retry(lambda: _fetch_history_hk(symbol, start_date, end_date))
    if market == "US":
        return _with_retry(lambda: _fetch_history_us(symbol, start_date, end_date))

    if frequency != "d":
        return _fetch_history_baostock(symbol, start_date, end_date, frequency)

    for fetch in (
        lambda: _fetch_history_baostock(symbol, start_date, end_date, "d"),
        lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq"
        ),
        lambda: _fetch_history_sina(symbol, start_date, end_date),
    ):
        try:
            df = _with_retry(fetch, retries=1, backoff=3)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    raise RuntimeError("三个数据源（BaoStock/东财/新浪）全部获取失败，稍后再试。")


@st.cache_data(ttl=30, show_spinner=False)
def get_stock_realtime(symbol: str, market: str = "A") -> dict:
    """真正的实时行情（新浪单股快照接口），不是最近收盘价。

    之前这里是从日线历史数据里取最后一行——那是"最近收盘价"，交易时段内
    跟用户自己在别的地方看到的实时价格对不上。这个接口是新浪的轻量单股查询，
    只查一只股票、不拉全市场，缓存 TTL 也缩短到 30 秒，更贴近"实时"。

    A股/港股/美股三个市场 hq.sinajs.cn 返回的字段顺序完全不一样，各写各的解析。
    """

    def _fetch():
        if market == "HK":
            code = f"rt_hk{symbol}"
        elif market == "US":
            code = f"gb_{symbol.lower()}"
        else:
            code = f"{_sina_symbol(symbol)}{symbol}"

        r = requests.get(
            f"https://hq.sinajs.cn/list={code}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        text = r.content.decode("gbk", errors="ignore")
        raw = text.split('"')[1]
        fields = raw.split(",")

        if market == "HK":
            # 英文名,中文名,今开,昨收,最高,最低,现价,涨跌额,涨跌幅,买一,卖一,成交额,成交量,...,日期,时间
            if len(fields) < 19 or not fields[6]:
                return {}
            return {
                "代码": symbol, "名称": fields[1],
                "最新价": float(fields[6]), "今开": float(fields[2]),
                "昨收": float(fields[3]), "最高": float(fields[4]), "最低": float(fields[5]),
                "更新时间": f"{fields[17]} {fields[18]}",
            }
        if market == "US":
            # 名称,现价,涨跌幅,时间戳,涨跌额,今开,最高,最低,昨收,...
            if len(fields) < 9 or not fields[1]:
                return {}
            return {
                "代码": symbol, "名称": fields[0],
                "最新价": float(fields[1]), "今开": float(fields[5]),
                "昨收": float(fields[8]), "最高": float(fields[6]), "最低": float(fields[7]),
                "更新时间": fields[3],
            }

        if len(fields) < 32 or not fields[3]:
            return {}
        return {
            "代码": symbol,
            "名称": fields[0],
            "最新价": float(fields[3]),
            "今开": float(fields[1]),
            "昨收": float(fields[2]),
            "最高": float(fields[4]),
            "最低": float(fields[5]),
            "更新时间": f"{fields[30]} {fields[31]}",
        }

    return _with_retry(_fetch, retries=1, backoff=2)


@st.cache_data(ttl=600, show_spinner=False)
def get_financial_abstract(symbol: str) -> pd.DataFrame:
    """财务摘要指标。"""
    return _with_retry(lambda: ak.stock_financial_abstract(symbol=symbol))


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_news(keyword: str, limit: int = 10) -> pd.DataFrame:
    """个股相关新闻。

    原本调东财的关键词搜索接口，实测发现它已经被反爬拦截了——不管传什么关键词，
    返回的都是同一份缓存假数据（连 JSONP 回调标识都一模一样）。这类接口层面的
    伪装拦截没法靠改参数绕过，所以换成财新的大盘资讯源（get_market_news），
    本地按公司名关键词过滤出相关条目；如果一条都没提到这家公司，就退化成
    展示最新的大盘资讯，好过什么都不显示。
    """
    df = get_market_news()
    if df is None or df.empty:
        return pd.DataFrame()

    matched = df[df["summary"].str.contains(keyword, na=False)]
    result = matched if not matched.empty else df

    result = result.head(limit).copy()
    result["新闻标题"] = result["summary"].str.slice(0, 24) + "…"
    result = result.rename(columns={"summary": "新闻内容", "tag": "分类"})
    return result[["新闻标题", "新闻内容", "分类"]]


@st.cache_data(ttl=300, show_spinner=False)
def get_market_news() -> pd.DataFrame:
    """大盘/宏观资讯，补充个股新闻覆盖不到的面。"""
    return _with_retry(ak.stock_news_main_cx)
