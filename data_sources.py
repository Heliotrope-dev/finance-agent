"""数据层 —— 封装 AkShare 调用，统一加重试/限流间隔/缓存。"""

import contextlib
import io
import json
import queue as _queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import baostock as bs
import pandas as pd
import requests
import streamlit as st

try:
    import futu as ft
    _FUTU_SDK_AVAILABLE = True
except ImportError:
    _FUTU_SDK_AVAILABLE = False

# 给所有出站 HTTP 请求（包括 akshare 内部）打上 15 秒硬超时，防止页面永久 loading。
_original_session_request = requests.Session.request


def _session_request_with_timeout(self, method, url, **kwargs):
    kwargs.setdefault("timeout", 15)
    return _original_session_request(self, method, url, **kwargs)


requests.Session.request = _session_request_with_timeout

_MIN_INTERVAL_SEC = 3  # 东财接口对高频请求会临时封IP，两次请求之间留够间隔
_last_call_ts = 0.0
_throttle_lock = threading.Lock()  # _throttle()是"读-判断-睡眠-写"，非原子，多线程并发时要加锁

_baostock_lock = threading.Lock()  # BaoStock的login/logout是全局会话，并发调用要加锁

# akshare 内部一部分接口（新浪源的日线/分钟线、部分指数、同花顺板块概要等）
# 用 py_mini_racer（内嵌V8引擎）执行网页反爬JS——实测验证过：两个线程同时
# 各自创建一个MiniRacer实例会直接触发V8内部的致命断言崩溃整个进程
# （"[FATAL:address_pool_manager.cc] Check failed: !pool->IsInitialized()"，
# SIGABRT，不是能try/except捕获的Python异常）。Streamlit给每个用户会话开
# 独立线程，两个用户同时分别打开不同市场的详情页就可能撞上，所以这里跟
# BaoStock一样，全部会触发py_mini_racer的调用点统一加锁串行执行。
_akshare_js_lock = threading.Lock()


def _throttle():
    """全局节流：两次（东财相关）请求之间强制留够_MIN_INTERVAL_SEC秒。

    "读_last_call_ts、判断要不要睡眠、睡眠、写_last_call_ts"这几步不是原子
    操作——多线程并发调用时，两个线程可能同时读到同一个旧的_last_call_ts、
    都判断"还没到间隔"直接放行，等于间隔完全没生效。跟同文件里
    _baostock_lock/_akshare_js_lock/_futu_worker_lock 一样的加锁风格，
    把整个"读-判断-睡眠-写"过程串行化。
    """
    global _last_call_ts
    with _throttle_lock:
        elapsed = time.time() - _last_call_ts
        if elapsed < _MIN_INTERVAL_SEC:
            time.sleep(_MIN_INTERVAL_SEC - elapsed)
        _last_call_ts = time.time()


def _with_retry(fn, retries=2, backoff=5, throttle=True):
    """throttle=True时，两次调用之间强制留至少_MIN_INTERVAL_SEC秒——这是专门
    针对东财接口的保护（东财对高频请求会临时封IP），但这个函数被BaoStock/新浪/
    财新等完全不需要这个保护的数据源也在用，之前不分青红皂白全部限流，导致
    持仓这种一次要连续拉好几只股票实时行情+历史数据的场景，硬生生被这个
    全局3秒间隔拖成"一个一个蹦出来"——实测这是今天好几次"页面好慢"反馈的
    真正原因。现在只有明确传throttle=True（东财相关调用）才会真的限流，
    其它数据源传throttle=False直接跳过等待。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            if throttle:
                _throttle()
            return fn()
        except Exception as e:  # noqa: BLE001 — 数据源异常统一兜底重试
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_err


# 共享熔断器：几个慢/不稳定的兜底数据源（涨停池、港股全市场兜底、指数快照的
# EOD兜底）之前各自手写一份"失败就记一个冷却解除时间戳，冷却期内直接跳过"
# 的逻辑，写法重复了三遍。统一抽成这一套，key 按用途区分
# （如 "limit_pool_up"/"hk_movers_fallback"/"index_snapshot_A"），
# 各处只需要在失败分支调用 _breaker_trip(key)、在入口调用 _breaker_open(key)。
_breaker_state: dict[str, float] = {}


def _breaker_open(key: str) -> bool:
    """熔断是否处于冷却期内（True=还在冷却，应该跳过慢路径直接返回空）。"""
    return time.time() < _breaker_state.get(key, 0.0)


def _breaker_trip(key: str, cooldown: float = 60):
    """记一次失败，进入cooldown秒的冷却期。"""
    _breaker_state[key] = time.time() + cooldown


def _sina_symbol(symbol: str) -> str:
    """AkShare 东财接口用纯数字代码，新浪/BaoStock 接口要带交易所前缀。

    这条判断规则原来只覆盖个股（6/9开头沪市，其它深市），没考虑基金/ETF——
    上交所的场内基金代码是5开头（510300沪深300ETF、588000科创50ETF这些），
    原规则把5开头的一律归到"其它→深市"，实际上是沪市的，会用错交易所前缀
    去查行情，直接查不到。加上"5"之后：沪市＝6/9(个股)+5(基金)，其余归深市，
    不影响任何已有的个股代码判断（没有深市个股代码是5开头的）。"""
    return "sh" if symbol.startswith(("5", "6", "9")) else "sz"


@st.cache_data(ttl=3600, show_spinner=False)
def search_stock_by_name(query: str) -> list[dict]:
    """按名称（支持模糊匹配）搜个股代码。只返回股票（排除指数/基金），只返回在市的。"""
    query = query.strip()
    if not query:
        return []
    with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):
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

    type_="5"（BaoStock对ETF/场内基金的分类）跟"1"（个股）一样放行——这个
    函数原本只服务"添加持仓"这一个调用点（app.py _resolve_confirmed_symbol），
    不是给"个股深度分析"用的，用户明确要求ETF/基金也要能添加成持仓，没理由
    在这里挡住。BaoStock的基础信息表本身对场内基金覆盖不全（比如510300这类
    上交所基金查不到基础信息，但get_stock_realtime能查到真实行情）——这种
    "BaoStock不认识"的情况，调用方(_resolve_confirmed_symbol)会再用真实行情
    查询兜底判断，不能把"没有找到代码"直接当成"这个代码不存在"，这里返回的
    只是"BaoStock这张表里没有"，不是终审结论。
    """
    info = _stock_basic_info(symbol)
    if not info:
        return False, f"没有找到代码「{symbol}」对应的股票，检查一下是不是输错了。"
    _, name, _ipo, out_date, type_, status = info
    if type_ not in ("1", "5"):
        return False, f"「{symbol}」不是个股/基金（可能是指数或其他类型），暂不支持添加。"
    if status != "1":
        return False, f"「{name}」（{symbol}）已经退市了（退市日期 {out_date or '未知'}），查不到行情数据。"
    return True, name


def _stock_basic_info(symbol: str) -> tuple | None:
    bs_code = f"{_sina_symbol(symbol)}.{symbol}"
    with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):
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

    with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):  # 屏蔽 baostock 自带的 login/logout 打印
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
    with _akshare_js_lock:
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


def _benchmark_history_a(start_date: str, end_date: str, index_code: str) -> pd.DataFrame:
    start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):
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


@st.cache_data(ttl=300, show_spinner=False)
def get_benchmark_history(start_date: str, end_date: str, market: str = "A") -> pd.DataFrame:
    """基准指数历史收盘价：A股用沪深300，港股用恒生指数，美股用标普500。

    三个分支原来都是裸调用（HK/US是没套_with_retry的新浪akshare接口，A股是
    baostock，出错会raise RuntimeError）——调用方（app.py"对比大盘"模块）没有
    try/except，接口一抖动就是整个详情页崩掉报错，而不是"对比大盘暂时不可用"
    这种降级。这里统一包一层try/except，取数失败就返回空DataFrame，跟
    app.py那边"if benchmark is not None and not benchmark.empty"的既有判断
    正好对上，不需要再改调用方。
    """
    try:
        if market == "HK":
            with _akshare_js_lock:
                df = ak.stock_hk_index_daily_sina(symbol="HSI")
            df = df.rename(columns={"date": "日期", "close": "收盘"})
            df["日期"] = pd.to_datetime(df["日期"])
            start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
            return df[(df["日期"] >= start) & (df["日期"] <= end)][["日期", "收盘"]]
        if market == "US":
            with _akshare_js_lock:
                df = ak.index_us_stock_sina(symbol=".INX")
            df = df.rename(columns={"date": "日期", "close": "收盘"})
            df["日期"] = pd.to_datetime(df["日期"])
            start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
            return df[(df["日期"] >= start) & (df["日期"] <= end)][["日期", "收盘"]]
        return _benchmark_history_a(start_date, end_date, "sh.000300")
    except Exception:
        return pd.DataFrame()


_MULTI_INDICES = {
    "A": [("上证指数", "sh.000001"), ("深证成指", "sz.399001"), ("创业板指", "sz.399006")],
    "HK": [("恒生指数", "HSI"), ("恒生科技", "HSTECH"), ("国企指数", "HSCEI")],
    "US": [("标普500", ".INX"), ("纳斯达克100", ".NDX"), ("道琼斯", ".DJI")],
}


def _one_index_snapshot(market: str, name: str, code: str) -> dict | None:
    """单个指数的快照，给 get_multi_index_snapshot 并发调用用（3个指数不再串行等）。

    EOD兜底分支（BaoStock/新浪指数日线）没有超时/重试包装，外层3秒缓存
    (get_multi_index_snapshot, ttl=3) 会导致这条慢路径被反复触发——跟
    get_limit_pool/_get_hk_movers_by_change是同一个问题，这里用同一套
    共享熔断（_breaker_open/_breaker_trip），按 (market) 维度冷却，
    一次失败后60秒内直接跳过兜底、返回None，不再反复卡住。
    """
    breaker_key = f"index_snapshot_{market}"
    try:
        if market == "A":
            tc_snap = _a_index_snapshot_tencent(code)
            if tc_snap:
                return {"名称": name, **tc_snap}
            if _breaker_open(breaker_key):
                return None
            # 腾讯实时快照失败时的兜底——BaoStock日线是EOD数据，交易时段内会滞后一天。
            # BaoStock 的 login/logout 是全局会话，不是线程安全的，不只这里3个指数
            # 并发跑会撞车——Streamlit给每个用户会话开独立线程，任何两个不同用户
            # 同时触发本文件里任意两处BaoStock调用都可能互相踢掉对方的登录状态，
            # 所以全文件所有 bs.login()/bs.logout() 都统一加了 _baostock_lock。
            try:
                start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
                end = datetime.now().strftime("%Y-%m-%d")
                with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):
                    bs.login()
                    try:
                        rs = bs.query_history_k_data_plus(code, "date,close", start_date=start, end_date=end, frequency="d")
                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                    finally:
                        bs.logout()
            except Exception:
                _breaker_trip(breaker_key)
                raise
            if len(rows) < 2:
                _breaker_trip(breaker_key)
                return None
            last, prev = float(rows[-1][1]), float(rows[-2][1])
        elif market == "HK":
            futu_snap = _hk_index_snapshot_futu(name)
            if futu_snap:
                return {"名称": name, **futu_snap}
            if _breaker_open(breaker_key):
                return None
            # Futu不可用时的兜底——新浪的指数日线接口是EOD数据，交易时段内会滞后一天。
            try:
                with _akshare_js_lock:
                    df = ak.stock_hk_index_daily_sina(symbol=code)
            except Exception:
                _breaker_trip(breaker_key)
                raise
            if len(df) < 2:
                _breaker_trip(breaker_key)
                return None
            last, prev = float(df.iloc[-1]["close"]), float(df.iloc[-2]["close"])
        else:
            if _breaker_open(breaker_key):
                return None
            try:
                with _akshare_js_lock:
                    df = ak.index_us_stock_sina(symbol=code)
            except Exception:
                _breaker_trip(breaker_key)
                raise
            if len(df) < 2:
                _breaker_trip(breaker_key)
                return None
            prev = float(df.iloc[-2]["close"])
            futu_snap = _us_index_snapshot_futu(name, prev)
            if futu_snap:
                return {"名称": name, **futu_snap}
            # Futu不支持美股原生指数代码，兜底走新浪日线（EOD，交易时段内滞后一天）。
            last = float(df.iloc[-1]["close"])
        change = last - prev
        pct = change / prev * 100 if prev else 0
        return {"名称": name, "最新": last, "涨跌": change, "涨跌幅": pct}
    except Exception:
        return None


@st.cache_data(ttl=3, show_spinner=False)
def get_multi_index_snapshot(market: str) -> list[dict]:
    """给行情页顶部的指数卡片用：每个市场固定几个核心指数，各自最新值+涨跌。

    这里特意不用线程池并发拉——实测过一次，AkShare 内部某些接口用 py_mini_racer
    （V8引擎，用来跑一段JS解密响应）做首次初始化不是线程安全的，多个线程同时
    第一次触发会直接把整个 Streamlit 进程带崩（FATAL级别，不是能catch的异常）。
    串行虽然慢一点，但这是能稳定跑的版本。

    TTL 跟指数详情页实时价格区块（_render_index_price_header，@st.fragment
    每 3 秒自动刷新）对齐——原来是25秒，比刷新周期长得多，fragment大部分
    刷新其实都在重画同一份缓存值，数字并不会真的跳动。
    """
    indices = _MULTI_INDICES.get(market, [])
    return [r for r in (_one_index_snapshot(market, name, code) for name, code in indices) if r]


@st.cache_data(ttl=60, show_spinner=False)
def get_multi_index_snapshot_slow(market: str) -> list[dict]:
    """跟get_multi_index_snapshot逻辑完全一样，只是缓存TTL换成60秒——给
    AI咨询窗（assistant.py）这类"偶尔查一次、不需要3秒级实时跳动"的调用方
    用，不要直接调上面那个3秒缓存的版本。2026-08-28复核时实测发现：AI
    助手打开时对A/HK/US三个市场各调一次get_multi_index_snapshot，三个市场
    加起来近9个指数，3秒缓存意味着几乎每次打开都要重新串行发一遍网络
    请求（这个函数内部本来就是故意串行的，见上面函数的docstring），实测
    60秒都没跑完。3秒缓存是专门为"行情"页那个每3秒自动刷新的fragment
    设计的，AI助手复用它纯属选错了函数，不是"需要更快的数据"，加一个更
    长缓存的独立版本就够。"""
    return get_multi_index_snapshot(market)


@st.cache_data(ttl=300, show_spinner=False)
def get_index_history(code: str, market: str, period: str = "日K") -> pd.DataFrame:
    """指数K线。三个市场的指数接口都只给日线，周K/月K用pandas重采样凑，
    没有分时数据（指数没有Futu那种实时分时源），分时选项退化成展示日K。
    """
    if market == "A":
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=1825)).strftime("%Y-%m-%d")
        with _baostock_lock, contextlib.redirect_stdout(io.StringIO()):
            bs.login()
            try:
                rs = bs.query_history_k_data_plus(
                    code, "date,open,high,low,close,volume", start_date=start, end_date=end, frequency="d",
                )
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
            finally:
                bs.logout()
        df = pd.DataFrame(rows, columns=["日期", "开盘", "最高", "最低", "收盘", "成交量"])
    elif market == "HK":
        with _akshare_js_lock:
            raw = ak.stock_hk_index_daily_sina(symbol=code)
        df = raw.rename(columns={"date": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量"})
    else:
        with _akshare_js_lock:
            raw = ak.index_us_stock_sina(symbol=code)
        df = raw.rename(columns={"date": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量"})

    df["日期"] = pd.to_datetime(df["日期"])
    for col in ("开盘", "最高", "最低", "收盘", "成交量"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["开盘", "收盘"]).sort_values("日期")

    today = pd.Timestamp(datetime.now().date())
    if not df.empty and df["日期"].max() < today:
        try:
            name = next((n for n, c in _MULTI_INDICES.get(market, []) if c == code), None)
            snap = next((s for s in get_multi_index_snapshot(market) if s["名称"] == name), None) if name else None
        except Exception:
            snap = None
        if snap and snap.get("最新"):
            today_row = {
                "日期": today, "开盘": snap["最新"], "最高": snap["最新"],
                "最低": snap["最新"], "收盘": snap["最新"], "成交量": 0,
            }
            df = pd.concat([df, pd.DataFrame([today_row])], ignore_index=True)

    if period == "周K":
        df = df.set_index("日期").resample("W").agg(
            {"开盘": "first", "最高": "max", "最低": "min", "收盘": "last", "成交量": "sum"}
        ).dropna().reset_index()
    elif period == "月K":
        df = df.set_index("日期").resample("ME").agg(
            {"开盘": "first", "最高": "max", "最低": "min", "收盘": "last", "成交量": "sum"}
        ).dropna().reset_index()
    else:
        df = df.tail(90)
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


@st.cache_data(ttl=300, show_spinner=False)
def get_market_breadth() -> dict:
    """A股大盘涨跌家数统计（上涨/下跌/涨停/跌停/活跃度）。只有A股有这个概念。"""
    df = _with_retry(ak.stock_market_activity_legu, throttle=False)  # 乐咕乐股网，不是东财
    return dict(zip(df["item"], df["value"]))


@st.cache_data(ttl=300, show_spinner=False)
def get_southbound_flow() -> dict | None:
    """南向资金（沪/深港股通合计净买额），只有港股有这个概念——内地资金通过港股通
    买卖港股的净额，是港股市场常看的一个风向标。数据来自东财，跟同花顺展示的
    同一份底层数据，口径可能有细微差异（分钟级更新时间点不同）。
    """
    df = _with_retry(ak.stock_hsgt_fund_flow_summary_em)
    if df is None or df.empty or "资金方向" not in df.columns:
        return None
    south = df[df["资金方向"] == "南向"]
    if south.empty:
        return None
    net_buy = float(south["成交净买额"].sum())
    return {"净买额": net_buy, "交易日": south["交易日"].iloc[0] if "交易日" in south.columns else ""}


@st.cache_data(ttl=60, show_spinner=False)
def get_limit_pool(kind: str = "up", limit: int = 10) -> pd.DataFrame:
    """涨停股池(kind='up')/跌停股池(kind='down')，按涨跌幅排序取前 limit 条。只有A股有这个概念。

    TTL之前跟着"涨跌闪烁"需求缩到过3秒，但那个功能所在的_render_a_share_
    overview后来因为导致页面残留问题被撤回了（改回手动刷新），3秒的缓存
    没了对应的自动刷新场景，只留下风险：实测东财这个push2ex接口本身会
    偶尔卡住触发15秒的read timeout，_with_retry的重试+退避加起来最坏能
    到30多秒——3秒缓存下用户随便一次点击触发的rerun都可能命中这个慢
    路径，这正是"网页又卡了"的真正原因。改回60秒（配合下面的熔断逻辑，
    足够安全），只有真正手动切换市场/展开才会偶尔等一下，不会每次交互
    都可能撞上这个慢接口。熔断用共享的 _breaker_open/_breaker_trip
    （见文件靠前的定义），key 是 "limit_pool_{kind}"。
    """
    breaker_key = f"limit_pool_{kind}"
    if _breaker_open(breaker_key):
        return pd.DataFrame()
    date_str = datetime.now().strftime("%Y%m%d")
    fn = ak.stock_zt_pool_em if kind == "up" else ak.stock_zt_pool_dtgc_em
    try:
        df = _with_retry(lambda: fn(date=date_str))
    except Exception:
        _breaker_trip(breaker_key)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.sort_values("涨跌幅", ascending=(kind != "up")).head(limit)
    keep = [c for c in ("代码", "名称", "涨跌幅", "最新价", "换手率") if c in df.columns]
    return df[keep].reset_index(drop=True)


_HK_FAMOUS_CODES = [
    "00700", "09988", "03690", "01810", "09999", "00941", "00939", "01398",
    "02318", "00005", "01299", "00388", "03968", "09618", "01024", "02020",
    "00027", "01928", "02628", "00016", "00883", "00003", "00688", "01109",
    "02331", "06618", "09888", "03888", "01211",
    # 2026-08-28：观察池港股目标从20支扩到40支——热度榜（stock_hk_hot_rank_em）
    # 挂了的时候，兜底路径（_get_hk_movers_by_change）只从这份名单里选，原来
    # 29支不够覆盖40支的目标，补了11支同样是知名度高、流动性好的港股（含几支
    # 中概股在港股这边的代码，跟_HK_NAME_MAP已有的条目对应）。
    "09961", "09626", "09866", "02015", "09868", "01088", "02688",
    "00011", "00006", "00012", "00017",
]

# 名称/别名 -> 代码，只覆盖知名股清单，给搜索框做本地模糊匹配用（不是全市场公司名库）。
_HK_NAME_MAP = {
    "腾讯": "00700", "腾讯控股": "00700", "tencent": "00700",
    "阿里巴巴": "09988", "阿里": "09988", "alibaba": "09988",
    "美团": "03690", "meituan": "03690",
    "小米": "01810", "小米集团": "01810", "xiaomi": "01810",
    "网易": "09999", "netease": "09999",
    "中国移动": "00941", "china mobile": "00941",
    "建设银行": "00939", "建行": "00939", "ccb": "00939",
    "工商银行": "01398", "工行": "01398", "icbc": "01398",
    "中国平安": "02318", "平安": "02318", "ping an": "02318",
    "汇丰": "00005", "汇丰控股": "00005", "hsbc": "00005",
    "友邦保险": "01299", "友邦": "01299", "aia": "01299",
    "香港交易所": "00388", "港交所": "00388", "hkex": "00388",
    "招商银行": "03968", "招行": "03968", "cmb": "03968",
    "京东": "09618", "京东集团": "09618", "jd": "09618",
    "快手": "01024", "kuaishou": "01024",
    "安踏": "02020", "安踏体育": "02020", "anta": "02020",
    "银河娱乐": "00027", "galaxy entertainment": "00027",
    "金沙中国": "01928", "sands china": "01928",
    "中国人寿": "02628", "china life": "02628",
    "新鸿基地产": "00016", "新鸿基": "00016", "shkp": "00016",
    "中国海洋石油": "00883", "中海油": "00883", "cnooc": "00883",
    "中华煤气": "00003", "香港中华煤气": "00003",
    "中国海外发展": "00688", "中海外": "00688",
    "华润置地": "01109", "china resources land": "01109",
    "李宁": "02331", "li ning": "02331",
    "京东健康": "06618", "jd health": "06618",
    "百度": "09888", "百度集团": "09888", "baidu": "09888",
    "金山软件": "03888", "kingsoft": "03888",
    "比亚迪": "01211", "byd": "01211",
    # 同时在美股上市的中概股，港股这边的代码——跟_US_NAME_MAP里对应条目
    # 配对使用，缺了任何一边"多市场都查得到就问用户选哪个"的歧义检测就查不全。
    "携程": "09961", "携程集团": "09961", "trip.com": "09961",
    "哔哩哔哩": "09626", "b站": "09626", "bilibili": "09626",
    "蔚来": "09866", "蔚来汽车": "09866", "nio": "09866",
    "理想汽车": "02015", "理想": "02015", "li auto": "02015",
    "小鹏汽车": "09868", "小鹏": "09868", "xpeng": "09868",
    # 恒生系列指数没有个股代码，跟大宗商品同一个处理原则——用规模/成交额
    # 最大的那支ETF代表（实测成交额对比过，03033比同类03032高约50倍）。
    "恒生科技": "03033", "恒生科技指数": "03033", "hstech": "03033",
    "恒生指数": "02800", "恒指": "02800", "hsi": "02800",
    "恒生国企": "02828", "国企指数": "02828", "恒生中国企业": "02828",
}


@st.cache_data(ttl=600, show_spinner=False)
def _get_hk_hot_rank_raw():
    """东财人气榜-港股市场原始数据，实测这个接口不太稳定（有时候整个响应体
    是空的，akshare内部json.loads直接炸"Expecting value"），单独抽出来方便
    两个调用方（get_hk_famous_movers、get_index_top_movers）共用同一套
    "热度榜失败就退回涨跌幅榜"的兜底逻辑，不用两边各写一次。
    """
    try:
        df = _with_retry(ak.stock_hk_hot_rank_em, retries=2, backoff=3)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    return df.rename(columns={"股票名称": "名称"})


def _get_hk_movers_by_change(limit: int) -> pd.DataFrame:
    """知名港股名单按涨跌幅排序，给"行情"页港股核心股用。

    优先走Futu批量快照，只查_HK_FAMOUS_CODES这几十只知名股（秒级返回）。
    之前这里是用ak.stock_hk_spot()先拉整个港股市场（几千只股票）的快照，
    再从里面筛出_HK_FAMOUS_CODES这几十条——用户反馈"港股行情加载异常慢"，
    实测ak.stock_hk_spot()单次调用本身就要30-40秒（内部分页拉全市场），
    而这个函数最终只用得上其中29条数据，等于为了29只股票拉了全市场几千只
    股票的快照，完全没必要。Futu本来就已经连着、给指数快照用（那边0.5秒内
    返回），这里改成直接查这份知名股清单，不用再绕全市场快照这条慢路径。
    只有Futu没连上时才退回旧的akshare全市场快照兜底（慢，但至少能用）。

    熔断这条兜底路径的原因：这个函数现在配合"港股核心股"卡片的涨跌闪烁
    效果，缓存TTL缩短了（get_hk_famous_movers）。如果不加熔断，一旦Futu
    真的掉线，每次缓存过期都会重新触发这个近30-40秒的慢兜底，而这个函数
    自己跑一次的时间比缓存TTL还长，等于缓存刚写完、下一次读取又发现"过期"
    了，陷入几乎不间断连续触发慢路径的死循环。加上60秒冷却（用共享的
    _breaker_open/_breaker_trip，key="hk_movers_fallback"）：兜底刚失败过
    就在冷却期内直接返回空，不再反复尝试，让页面至少能正常展示"暂时获取
    不到"而不是一直卡着。
    """
    codes = [f"HK.{c}" for c in _HK_FAMOUS_CODES]
    ret, snap = _futu_call(lambda ctx: ctx.get_market_snapshot(codes), default=(None, None))
    if ret == ft.RET_OK and snap is not None and not snap.empty:
        snap = snap[snap["prev_close_price"] > 0].copy()
        if not snap.empty:
            snap["涨跌幅"] = (snap["last_price"] - snap["prev_close_price"]) / snap["prev_close_price"] * 100
            snap["涨跌额"] = snap["last_price"] - snap["prev_close_price"]
            snap["代码"] = snap["code"].str.replace("HK.", "", regex=False)
            snap = snap.rename(columns={"name": "名称", "last_price": "最新价"})
            snap = snap.sort_values("涨跌幅", ascending=False).head(limit)
            return snap[["代码", "名称", "最新价", "涨跌幅", "涨跌额"]].reset_index(drop=True)

    breaker_key = "hk_movers_fallback"
    if _breaker_open(breaker_key):
        return pd.DataFrame()

    # Futu没连上时的兜底：老的全市场快照方案，慢但能用
    try:
        with _akshare_js_lock:
            df = _with_retry(ak.stock_hk_spot, retries=1, throttle=False)  # 新浪，不是东财
    except Exception:
        _breaker_trip(breaker_key)
        return pd.DataFrame()
    if df is None or df.empty or "涨跌幅" not in df.columns:
        _breaker_trip(breaker_key)
        return pd.DataFrame()
    df = df[df["代码"].isin(_HK_FAMOUS_CODES)]
    df = df.sort_values("涨跌幅", ascending=False).head(limit)
    keep = [c for c in ("代码", "中文名称", "最新价", "涨跌幅", "涨跌额") if c in df.columns]
    return df[keep].rename(columns={"中文名称": "名称"}).reset_index(drop=True)


# 恒生科技指数(HSTECH)真实成分股，截至2026-06-08生效（用WebFetch核实过、
# 跟另一独立信息源交叉验证过——比亚迪/腾讯音乐/地平线机器人是这一次调整
# 新纳入的，阅文集团/东方甄选/众安在线被剔除了，两边说法一致）。之前这里
# 用"港股全市场热门股按涨跌幅排序"代替真实成分股，用户反馈里面混进了
# 优然牧业（乳业）、布鲁可（玩具）这些跟科技毫不相关的公司；改成不展示后
# 用户又反馈"之前虽然错但至少有内容，现在啥都没有"——两头权衡，用这份
# 手动核实过的真实名单，比"编一份数据"或者"完全不展示"都更合适。
# **需要人工维护**：恒生指数公司按季度调整成分股，这份名单会过时，如果
# 官方有调整、这里没跟着更新，就会漏掉新纳入的或者错误保留已剔除的——
# 跟_HK_FAMOUS_CODES这类手动维护名单是同一类已知局限，不是自动同步的。
_HSTECH_CONSTITUENTS = [
    "09999", "00700", "01810", "01211", "09988", "03690", "00981", "09618",
    "09888", "00992", "01024", "01347", "09961", "09868", "02015", "09660",
    "00300", "00020", "02382", "06690", "09626", "06618", "09863", "09866",
    "02513", "00241", "00285", "00780", "00100", "01698",
]


def get_hstech_constituents(limit: int = 30) -> pd.DataFrame:
    """恒生科技指数的真实成分股（见_HSTECH_CONSTITUENTS上面的说明），只走
    Futu批量快照——这份名单本来就是手动维护的真实成分股，不是"全市场筛出来
    的近似"，没有必要也不应该再退回akshare全市场快照那条路（那条路径拉到的
    是全市场排名，用来"筛"这30只反而会因为快照对不上时点而产生误差，直接
    Futu查这30只自己的实时快照最准）。Futu没连上时返回空，调用方按"暂不
    可用"处理，不硬凑数据。

    不复用共享的 _get_futu_ctx()——那个 ctx 是在后台线程里连接、传回主线程
    长期持有的，这里如果直接用它同步查询30只股票的快照，就是"连接和调用不是
    同一个线程"，实测会直接卡死超过90秒不返回（跟 get_futu_news /
    get_stock_realtime_futu 那边记录的教训一样）。改成跟 get_futu_news 一样：
    连接+查询+关闭全部放在同一个子线程里做完，超时兜底返回空。
    """
    if not _FUTU_SDK_AVAILABLE:
        return pd.DataFrame()

    codes = [f"HK.{c}" for c in _HSTECH_CONSTITUENTS]

    def _fetch():
        ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            return ctx.get_market_snapshot(codes)
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    try:
        result = _run_with_timeout(_fetch, timeout=8, default=None)
    except Exception:
        result = None
    if result is None:
        return pd.DataFrame()
    ret, snap = result
    if ret != ft.RET_OK or snap is None or snap.empty:
        return pd.DataFrame()
    snap = snap[snap["prev_close_price"] > 0].copy()
    if snap.empty:
        return pd.DataFrame()
    snap["涨跌幅"] = (snap["last_price"] - snap["prev_close_price"]) / snap["prev_close_price"] * 100
    snap["涨跌额"] = snap["last_price"] - snap["prev_close_price"]
    snap["代码"] = snap["code"].str.replace("HK.", "", regex=False)
    snap = snap.rename(columns={"name": "名称", "last_price": "最新价"})
    snap = snap.sort_values("涨跌幅", ascending=False).head(limit)
    return snap[["代码", "名称", "最新价", "涨跌幅", "涨跌额"]].reset_index(drop=True)


@st.cache_data(ttl=3, show_spinner=False)
def get_hk_famous_movers(limit: int = 15) -> pd.DataFrame:
    """港股核心股列表——直接走涨跌幅榜（新浪快照+知名股名单），不走东财人气榜。
    东财人气榜数据是更真实的"热度"，但接口本身不稳定，_with_retry重试+全局限流
    加起来经常要好几秒才失败一次，"行情"页每次打开港股都要扛这个延迟，用户明确
    反馈"加载好慢"——两边取舍下来，稳定快速更重要，人气榜这条路径不再用在这里
    （get_index_top_movers的港股成分股仍然用_get_hk_hot_rank_raw，那边场景不同，
    是点开指数详情页才触发，不是每次进港股行情都要等）。

    这里必须加缓存——实测ak.stock_hk_spot()本身单次调用就要接近30秒（内部是
    分页/逐条拉全市场快照），去掉热度榜那条路径之后它就是唯一数据源了，不缓存
    的话每次进港股行情都要扛这近30秒，比之前更慢，跟这次改动的初衷完全相反。

    TTL跟涨跌闪烁其它地方一样对齐到3秒——之前担心_get_hk_movers_by_change
    在Futu掉线时会退回近30秒的akshare全市场慢兜底，3秒缓存下会陷入几乎
    不间断触发慢路径的死循环；现在那条兜底路径本身加了60秒熔断（见
    _get_hk_movers_by_change），触发一次慢查询后不管成败都会有60秒不再
    重试，缓存TTL可以放心跟其它地方一样缩到3秒，用户能看到实时闪烁效果。
    """
    return _get_hk_movers_by_change(limit)


_US_FAMOUS_CODES = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX",
    "AMD", "INTC", "AVGO", "ORCL", "CRM", "ADBE", "PYPL", "UBER",
    "DIS", "KO", "PEP", "NKE",
    "JPM", "V", "MA", "HD", "WMT", "JNJ", "PG", "XOM", "CVX", "BA",
    "GS", "MS", "IBM", "QCOM", "TXN", "COST", "SBUX", "MCD", "LLY",
    "UNH", "PFE", "T", "VZ", "CSCO", "GE", "CAT", "PLTR", "SNOW", "SHOP",
    # 2026-08-28：观察池美股目标从20支扩到60支（get_index_top_movers("US")
    # 兜底就是从这份名单按当天涨跌幅取前N），原来48支不够覆盖60支的目标，
    # 补了17支同样是市值大、流动性好、跨行业分散的知名股，不是随便凑数。
    "LIN", "ABBV", "ABT", "ACN", "AXP", "BAC", "BKNG", "C", "CMCSA",
    "COP", "DE", "GILD", "HON", "INTU", "ISRG", "LRCX", "MU",
]

_US_NAME_MAP = {
    "苹果": "AAPL", "apple": "AAPL",
    "微软": "MSFT", "microsoft": "MSFT",
    "谷歌": "GOOGL", "google": "GOOGL", "alphabet": "GOOGL",
    "亚马逊": "AMZN", "amazon": "AMZN",
    "特斯拉": "TSLA", "tesla": "TSLA",
    "英伟达": "NVDA", "nvidia": "NVDA",
    "meta": "META", "facebook": "META", "脸书": "META",
    "奈飞": "NFLX", "netflix": "NFLX",
    "amd": "AMD",
    "英特尔": "INTC", "intel": "INTC",
    "博通": "AVGO", "broadcom": "AVGO",
    "甲骨文": "ORCL", "oracle": "ORCL",
    "salesforce": "CRM", "赛富时": "CRM",
    "adobe": "ADBE", "奥多比": "ADBE",
    "paypal": "PYPL",
    "优步": "UBER", "uber": "UBER",
    "迪士尼": "DIS", "disney": "DIS",
    "可口可乐": "KO", "coca cola": "KO", "coca-cola": "KO",
    "百事": "PEP", "百事可乐": "PEP", "pepsi": "PEP",
    "耐克": "NKE", "nike": "NKE",
    # 下面这些是同时在港股+美股两地上市的中概股——跟_HK_NAME_MAP里用一样的
    # 中文key，是给"添加持仓"那边做"多市场都查得到就问用户选哪个"的
    # 判断用的，不能漏掉任何一边，不然那个歧义检测就形同虚设。
    "阿里巴巴": "BABA", "阿里": "BABA", "alibaba": "BABA",
    "京东": "JD", "京东集团": "JD",
    "百度": "BIDU", "百度集团": "BIDU", "baidu": "BIDU",
    "网易": "NTES", "netease": "NTES",
    "携程": "TCOM", "携程集团": "TCOM", "trip.com": "TCOM",
    "哔哩哔哩": "BILI", "b站": "BILI", "bilibili": "BILI",
    "蔚来": "NIO", "蔚来汽车": "NIO", "nio": "NIO",
    "理想汽车": "LI", "理想": "LI", "li auto": "LI",
    "小鹏汽车": "XPEV", "小鹏": "XPEV", "xpeng": "XPEV",
    "拼多多": "PDD", "pdd": "PDD",
    "唯品会": "VIPS", "vipshop": "VIPS",
    "腾讯音乐": "TME", "tencent music": "TME",
    # 大宗商品没有个股代码，用户明确要求"一律以最典型的指数或个股出现"——
    # 这里挑各品类流动性最好、最被广泛当作价格代理的ETF：黄金GLD(SPDR
    # Gold Shares，规模最大的黄金ETF)、白银SLV、原油USO(跟踪WTI原油价格，
    # 零售投资者最常用的原油代理，不是期货本身)、天然气UNG、铜CPER。
    "黄金": "GLD", "gold": "GLD",
    "白银": "SLV", "silver": "SLV",
    "原油": "USO", "石油": "USO", "crude oil": "USO", "oil": "USO",
    "天然气": "UNG", "natural gas": "UNG",
    "铜": "CPER", "copper": "CPER",
}

# A股场内基金/ETF名称->代码，BaoStock的按名称模糊搜索(search_stock_by_name)
# 只覆盖个股不含基金，中文名搜不到——手动维护这份最主流宽基/行业ETF的别名表
# 补上这块。用户反馈"ETF基金搜索搜不到"，排查发现两个独立问题都要修：一是
# 这里(名称搜索完全没覆盖基金)，二是check_stock_valid原来直接拒绝非个股
# type、以及_sina_symbol的沪深交易所前缀判断没考虑基金代码(5开头是沪市基金，
# 原来跟"其它"一起被归到深市，两个bug已在check_stock_valid/_sina_symbol修复)。
_A_FUND_NAME_MAP = {
    "沪深300etf": "510300", "沪深300": "510300",
    "创业板etf": "159915", "创业板": "159915",
    "科创50etf": "588000", "科创50": "588000",
    "黄金etf": "518880",
    "纳指etf": "513100", "纳斯达克100etf": "513100",
    "中证500etf": "510500", "中证500": "510500",
}


def search_quote_futu(keyword: str) -> list[dict]:
    """Futu 的全市场模糊搜索（get_search_quote），支持中英文/拼音，覆盖全市场股票，
    不是手动维护的名单——只在本机连得上 OpenD 时可用，连不上返回空列表。
    """
    ret, data = _futu_call(lambda ctx: ctx.get_search_quote(keyword, 10), timeout=6, default=(None, None))
    if ret != ft.RET_OK or data is None or data.empty:
        return []
    results = []
    for _, row in data.iterrows():
        if row.get("sec_type") != "STOCK" or row.get("market") not in ("HK", "US"):
            continue
        raw_code = str(row["code"])
        code = raw_code.split(".", 1)[1] if "." in raw_code else raw_code
        results.append({"market": row["market"], "code": code, "name": row["name"]})
    return results


def resolve_symbol_by_name(query: str, market: str) -> str | None:
    """名称/别名（中英文）匹配到代码。先查手动维护的知名股名单（快、不依赖本地环境），
    查不到再退一步试 Futu 的全市场模糊搜索（准，但只有本机连着 OpenD 才有）。

    两边都查不到不代表股票不存在，可能是没上市（比如SpaceX，私营公司，
    压根没有股票代码），或者 Futu 不可用时又刚好不在知名股名单里。
    """
    name_map = {"HK": _HK_NAME_MAP, "US": _US_NAME_MAP}.get(market)
    hit = name_map.get(query.strip().lower()) if name_map else None
    if hit:
        return hit
    for r in search_quote_futu(query.strip()):
        if r["market"] == market:
            return r["code"]
    return None


def detect_symbol_candidates(query: str) -> list[dict]:
    """"添加持仓"用——不用先问用户选哪个市场，自动判断这个名字在哪些市场
    能查到。像"苹果"这种只有一个市场命中，直接返回一条结果，调用方可以
    不问market直接添加；像"阿里巴巴"这种港股美股都有手动维护的别名条目，
    会返回两条，调用方就得让用户选。

    只走快速安全的路径（纯代码格式的正则判断 + A股名称库 + 港股/美股手动
    维护的别名map），不碰Futu的全市场模糊搜索兜底——那条路径在没有本地
    OpenD连接、或者跨线程调用时实测会直接卡死不返回（get_stock_realtime_futu
    那边记录过这个坑），放在这种"先探测再决定"的轻量场景里风险太大。
    覆盖不到的冷门名字，调用方自己再退回原来那套需要手动选市场的输入方式。
    """
    q = query.strip()
    if not q:
        return []

    if re.match(r"^\d{6}$", q):
        # 6位数字：可能是交易所个股/ETF代码，也可能是OTC联接基金代码
        # （比如012805），两者代码格式完全一样区分不了，交给get_stock_realtime
        # 自己按"先试交易所行情、查不到再试基金净值"的顺序兜底，这里不用
        # 提前判断是哪一种。
        return [{"symbol": q, "market": "A", "market_label": "A股"}]
    if re.match(r"^\d{4,5}$", q):
        return [{"symbol": q.zfill(5), "market": "HK", "market_label": "港股"}]
    if q.upper() in _SGE_SYMBOL_MAP:
        return [{"symbol": q.upper(), "market": "A", "market_label": "A股"}]

    results = []
    q_lower = q.lower()
    try:
        a_matches = search_stock_by_name(q)
    except Exception:
        a_matches = []
    if a_matches:
        results.append({"symbol": a_matches[0]["code"], "market": "A", "market_label": "A股"})
    else:
        # search_stock_by_name(BaoStock按名称模糊搜索)只覆盖个股，不含ETF/基金，
        # 名字查不到个股时先试手动维护的A股场内ETF别名表，再试OTC联接基金
        # 全量名录子串匹配（覆盖面更广但没有人工筛选过，放在最后兜底）。
        a_fund_code = _A_FUND_NAME_MAP.get(q_lower)
        if a_fund_code:
            results.append({"symbol": a_fund_code, "market": "A", "market_label": "A股"})
        else:
            otc_hit = search_otc_fund_by_name(q)
            if otc_hit:
                results.append({"symbol": otc_hit["symbol"], "market": "A", "market_label": "A股"})

    hk_code = _HK_NAME_MAP.get(q_lower)
    if hk_code:
        results.append({"symbol": hk_code, "market": "HK", "market_label": "港股"})

    us_code = _US_NAME_MAP.get(q_lower)
    if us_code:
        results.append({"symbol": us_code, "market": "US", "market_label": "美股"})

    # 纯字母代码——但字母代码也可能刚好撞上上面手动维护的英文别名
    # （比如"nio"既是代码又是别名），别名表命中优先，查不到才当纯代码用。
    if not results and re.match(r"^[A-Za-z.]{1,6}$", q):
        results.append({"symbol": q.upper(), "market": "US", "market_label": "美股"})

    return results


@st.cache_data(ttl=3, show_spinner=False)
def get_us_famous_movers(limit: int = 15) -> pd.DataFrame:
    """美股核心股列表，按固定的核心股顺序展示，不按涨跌幅排。

    找过港股那样的真实热度榜（东财人气榜、百度热搜股票）——百度热搜
    stock_hot_search_baidu(symbol="美股") 确实有热度数据（"综合热度"字段），
    但它给的是公司中文名不是股票代码，很多名字（比如"Sandisk Corporation"
    这种英文法人名）没法可靠地映射回代码，硬猜容易猜错，不如老实展示固定
    核心股名单，不排序也不装作是热度榜。

    stock_us_famous_spot_em（东财）今晚忽好忽坏，重试也救不回来。改用腾讯
    行情接口，而且这个接口支持一次请求批量查多只股票（逗号分隔），不用像
    单股实时行情那样一只只查——一次请求近50只，几乎瞬间。这里不走
    get_stock_realtime/_with_retry 那条路，就是因为不想被那个为东财设计的
    全局限流拖慢。（原来走新浪，把跳动周期缩到3秒后新浪被限流连不上了，
    索性全部换成腾讯，见get_stock_realtime的说明。）
    """
    codes = ",".join(f"us{c}" for c in _US_FAMOUS_CODES)
    r = requests.get(f"https://qt.gtimg.cn/q={codes}", timeout=10)
    text = r.content.decode("gbk", errors="ignore")

    rows = []
    for code, line in zip(_US_FAMOUS_CODES, text.strip().split("\n")):
        fields = _tencent_quote_fields(line)
        if fields is None:
            continue
        rows.append({
            "代码": code, "名称": fields[1], "最新价": float(fields[3]),
            "涨跌幅": float(fields[32]), "涨跌额": float(fields[31]),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).head(limit).reset_index(drop=True)


@st.cache_data(ttl=180, show_spinner=False)
def get_hot_sectors(market: str, limit: int = 30) -> pd.DataFrame:
    """"热门板块"用——按热度排序的行业板块列表，columns固定为
    [板块, 涨跌幅, 热度]。三个市场都没有找到真正意义上的"板块人气榜"
    （不像个股有东财人气榜那种真实热度数据），"热度"这里用成交额/成交量
    做代理指标——板块本身是一堆股票的聚合，钱往哪个板块涌得多，本来就
    比"点了多少次"更能说明这个板块今天是不是真的热，是个合理的替代指标，
    页面上会如实标注这不是官方热度指数。

    A股：同花顺的行业板块汇总接口（stock_board_industry_summary_ths），
    不依赖东财——今晚测试的时候东财的板块接口（stock_board_industry_name_em）
    连续多次连接失败，同花顺这条线稳定。
    港股/美股：Futu的板块快照——get_plate_list拿到这个市场全部行业板块，
    再用get_market_snapshot批量查这些板块自己的价格快照（Futu把板块当成
    一个可以查快照的"标的"，last_price/prev_close_price算出板块涨跌幅，
    turnover就是板块成交额）。
    """
    if market == "A":
        # 这里故意不 try/except 吞掉异常——get_hot_sectors 被 @st.cache_data(ttl=180)
        # 缓存，之前吞掉异常返回空DataFrame会被当成一次"正常"结果缓存180秒：
        # 哪怕只是一次瞬时网络抖动，"暂时获取不到"这个空结果会被冻结3分钟，
        # 期间所有用户都看到同样的失败提示，即便数据源早就恢复了。让异常
        # 正常抛出，st.cache_data 不会缓存抛异常的调用，下次访问会重新请求；
        # 调用方 _render_hot_sectors 已经有 try/except 兜底展示。
        with _akshare_js_lock:
            df = _with_retry(ak.stock_board_industry_summary_ths, throttle=False)
        if df is None or df.empty or "板块" not in df.columns:
            return pd.DataFrame()
        df = df.rename(columns={"总成交额": "热度"})
        df = df.sort_values("热度", ascending=False).head(limit)
        return df[["板块", "涨跌幅", "热度"]].reset_index(drop=True)

    # HK / US：走 Futu 板块快照
    ret, plates = _futu_call(lambda ctx: ctx.get_plate_list(market, ft.Plate.INDUSTRY), default=(None, None))
    if ret != ft.RET_OK or plates is None or plates.empty:
        return pd.DataFrame()
    codes = plates["code"].tolist()
    ret2, snap = _futu_call(lambda ctx: ctx.get_market_snapshot(codes), default=(None, None))
    if ret2 != ft.RET_OK or snap is None or snap.empty:
        return pd.DataFrame()

    snap = snap[snap["prev_close_price"] > 0].copy()
    snap["涨跌幅"] = (snap["last_price"] - snap["prev_close_price"]) / snap["prev_close_price"] * 100
    snap = snap.rename(columns={"name": "板块", "turnover": "热度"})
    snap = snap.sort_values("热度", ascending=False).head(limit)
    return snap[["板块", "涨跌幅", "热度"]].reset_index(drop=True)


@st.cache_data(ttl=180, show_spinner=False)
def get_sector_constituents(market: str, sector_name: str, limit: int = 30) -> pd.DataFrame:
    """"热门板块"点进去的成分股列表——返回列固定为[代码,名称,最新价,涨跌幅]，
    跟_render_stock_movers_cards期望的格式一致，成分股本身直接复用已有的
    个股详情页（走势+AI分析），不用给"板块"这个概念单独再造一套。

    A股：get_hot_sectors用的是同花顺板块名(stock_board_industry_summary_ths)，
    但同花顺没有对应的"成分股"接口(akshare里只有_em版本)，这里改用东财的
    stock_board_industry_cons_em——实测过东财的板块类接口这几天连续失败过
    (见get_hot_sectors的说明)，且东财自己的板块命名和同花顺不是同一套分类，
    传同花顺的板块名过去有一定概率查不到(接口内部按名称精确匹配东财自己的
    板块列表，查不到会抛KeyError/IndexError，不是网络异常)。两种失败都用
    try/except统一按“获取不到”处理，加熔断避免频繁重试拖慢页面，不假装
    这条路径和港股/美股一样可靠。

    港股/美股：Futu的板块体系是自洽的一整套(get_plate_list查到的板块名
    就是get_hot_sectors展示给用户看的那个名字)，plate_name精确匹配后
    用get_plate_stock拿成分股代码/名称，再用get_market_snapshot批量查价格，
    三步都在Futu内部，不存在跨数据源的分类口径不一致问题，可靠性明显更高。
    """
    if market == "A":
        breaker_key = "sector_cons_A"
        if _breaker_open(breaker_key):
            return pd.DataFrame()
        try:
            with _akshare_js_lock:
                df = _with_retry(
                    lambda: ak.stock_board_industry_cons_em(symbol=sector_name), retries=1, backoff=2, throttle=True,
                )
        except Exception:
            _breaker_trip(breaker_key)
            return pd.DataFrame()
        if df is None or df.empty or "代码" not in df.columns:
            _breaker_trip(breaker_key)
            return pd.DataFrame()
        if "成交额" in df.columns:
            df = df.sort_values("成交额", ascending=False)
        df = df.head(limit)
        keep = [c for c in ("代码", "名称", "最新价", "涨跌幅") if c in df.columns]
        return df[keep].reset_index(drop=True)

    # HK/US：板块列表 -> 成分股代码 -> 价格快照，三步都走Futu
    ret, plates = _futu_call(lambda ctx: ctx.get_plate_list(market, ft.Plate.INDUSTRY), default=(None, None))
    if ret != ft.RET_OK or plates is None or plates.empty:
        return pd.DataFrame()
    match = plates[plates["plate_name"] == sector_name]
    if match.empty:
        return pd.DataFrame()
    plate_code = match["code"].iloc[0]

    ret2, cons = _futu_call(lambda ctx: ctx.get_plate_stock(plate_code), default=(None, None))
    if ret2 != ft.RET_OK or cons is None or cons.empty:
        return pd.DataFrame()
    codes = cons["code"].tolist()

    ret3, snap = _futu_call(lambda ctx: ctx.get_market_snapshot(codes), default=(None, None))
    if ret3 != ft.RET_OK or snap is None or snap.empty:
        return pd.DataFrame()
    snap = snap[snap["prev_close_price"] > 0].copy()
    if snap.empty:
        return pd.DataFrame()
    snap["涨跌幅"] = (snap["last_price"] - snap["prev_close_price"]) / snap["prev_close_price"] * 100
    snap["代码"] = snap["code"].str.replace(f"{market}.", "", regex=False)
    name_map = dict(zip(cons["code"], cons["stock_name"]))
    snap["名称"] = snap["code"].map(name_map)
    snap = snap.rename(columns={"last_price": "最新价"})
    snap = snap.sort_values("涨跌幅", ascending=False).head(limit)
    return snap[["代码", "名称", "最新价", "涨跌幅"]].reset_index(drop=True)


# A股三大宽基指数各自覆盖的交易所/板块代码前缀。用来在"涨停股池"（覆盖
# 全市场）结果里筛掉根本不属于这个指数所在交易所/板块的股票——之前没做
# 这层过滤，"创业板指"的成分股板块会混进60/68开头（上交所主板/科创板）
# 的股票，这些公司压根没在创业板上市，比"不是官方成分股名单"这个已知的
# 近似误差更严重，是直接展示了错误归属的数据。
_A_INDEX_CODE_PREFIX = {
    "上证指数": ("60", "68"),  # 上交所主板 + 科创板
    "深证成指": ("00", "30"),  # 深交所主板/中小板 + 创业板
    "创业板指": ("30",),       # 创业板专属（300xxx/301xxx）
}


@st.cache_data(ttl=120, show_spinner=False)
def get_index_top_movers(market: str, limit: int = 30, index_name: str = "") -> pd.DataFrame:
    """指数详情页"成分股"板块用——不是严格意义上的官方成分股名单（A股几个
    宽基指数动辄几百上千只成分股，没法也没必要全拉一遍实时行情；港股/美股
    压根没找到带股票代码的官方成分股免费源），而是"这个市场里涨幅最大的一批
    股票"，按用户的说法："涨的最多的十个/三十个就好，不用都显示"——够用，
    不用追求跟官方成分股名单逐一对应。

    A股：复用涨停股池（stock_zt_pool_em）而不是拉全市场快照——之前这里用
    stock_zh_a_spot_em 拉全市场几千只股票的快照，本地排序取前limit名，
    用户反馈"成分股板块卡住了"，实测这个接口单次调用要接近2分钟（内部分页
    拉全市场，跟之前港股那个"热门板块"慢的问题是同一类根因）。A股涨幅有
    10%/20%封顶，当天涨幅最大的股票几乎必然是涨停股，语义上"涨停股池"
    约等于"涨幅最大的一批股票"，直接复用这个已经很快（十几秒，且跟"行情"
    页共用同一份缓存，用户逛过一次"行情"页的话这里经常是秒开）的数据源，
    不用再单独扛一次全市场扫描。传入index_name时按_A_INDEX_CODE_PREFIX
    过滤掉不属于这个指数所在交易所/板块的股票（比如"创业板指"不会再混进
    上交所主板的股票）——用户反馈过恒生科技那边混进不相关公司的问题，
    排查时顺带发现A股这边也有同一类问题，一并修了。
    港股：复用已经在用的东财人气榜（stock_hk_hot_rank_em，100只热门港股），
    改成按涨跌幅排序而不是按人气排序。
    美股：复用_US_FAMOUS_CODES这份手动维护的核心股名单（新浪批量行情），
    按涨跌幅排序——没有找到带代码的美股热度/成分股免费源，只能退而求其次
    用这份覆盖主要板块龙头的名单。
    """
    if market == "A":
        prefixes = _A_INDEX_CODE_PREFIX.get(index_name)
        # 有前缀过滤时多拉一些候选（过滤后可能不够limit条），没有的话按原样取limit
        df = get_limit_pool("up", limit * 5 if prefixes else limit)
        if df is None or df.empty:
            return pd.DataFrame()
        if prefixes:
            df = df[df["代码"].astype(str).str.startswith(prefixes)]
        df = df.head(limit)
        if df.empty:
            return pd.DataFrame()
        keep = [c for c in ("代码", "名称", "最新价", "涨跌幅") if c in df.columns]
        return df[keep].reset_index(drop=True)

    if market == "HK":
        df = _get_hk_hot_rank_raw()
        if df is not None:
            df = df.sort_values("涨跌幅", ascending=False).head(limit)
            keep = [c for c in ("代码", "名称", "最新价", "涨跌幅") if c in df.columns]
            return df[keep].reset_index(drop=True)
        # 热度榜也挂了，退回涨跌幅榜兜底（跟get_hk_famous_movers共用同一份
        # 兜底数据源，只是这里limit可以到30）。
        return _get_hk_movers_by_change(limit)

    # US
    try:
        codes = ",".join(f"us{c}" for c in _US_FAMOUS_CODES)
        r = requests.get(f"https://qt.gtimg.cn/q={codes}", timeout=10)
    except Exception:
        return pd.DataFrame()
    text = r.content.decode("gbk", errors="ignore")
    rows = []
    for code, line in zip(_US_FAMOUS_CODES, text.strip().split("\n")):
        fields = _tencent_quote_fields(line)
        if fields is None:
            continue
        rows.append({"代码": code, "名称": fields[1], "最新价": float(fields[3]), "涨跌幅": float(fields[32])})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("涨跌幅", ascending=False).head(limit).reset_index(drop=True)


def _fetch_history_hk(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """港股日线，新浪源。stock_hk_daily 不接受日期范围参数，返回全部历史，本地按日期筛。"""
    try:
        with _akshare_js_lock:
            df = ak.stock_hk_daily(symbol=symbol, adjust="")
    except Exception:
        # 代码不存在/格式不对时，akshare 内部解析新浪返回的空数据会直接抛
        # KeyError/IndexError 这类看不懂的底层异常，统一转成明确提示。
        raise ValueError(f"「{symbol}」不是有效的港股代码（应为5位数字，如 00700）。")
    if df is None or df.empty or "date" not in df.columns:
        raise ValueError(f"「{symbol}」不是有效的港股代码（应为5位数字，如 00700）。")
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
    try:
        with _akshare_js_lock:
            df = ak.stock_us_daily(symbol=symbol, adjust="")
    except Exception:
        raise ValueError(f"「{symbol}」不是有效的美股代码（应为英文股票代码，如 AAPL）。")
    if df is None or df.empty or "date" not in df.columns:
        raise ValueError(f"「{symbol}」不是有效的美股代码（应为英文股票代码，如 AAPL）。")
    df = df.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
        "close": "收盘", "volume": "成交量",
    })
    df["日期"] = pd.to_datetime(df["日期"])
    start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
    df = df[(df["日期"] >= start) & (df["日期"] <= end)]
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


def _append_today_bar(df: pd.DataFrame, symbol: str, market: str) -> pd.DataFrame:
    """日线数据源（BaoStock/新浪历史接口）都是收盘结算后才入库的，交易时段内查不到"今天"。

    图表看着就像整整滞后一天。用已经验证过又快又稳的实时报价（get_stock_realtime，
    港美股优先走Futu）拼一根"今天"的临时K线上去——高低开用实时快照里的数据，
    成交量拿不到就填0，好过图表在交易时间里一直停在昨天收盘。
    """
    if df is None or df.empty:
        return df
    today = pd.Timestamp(datetime.now().date())
    if df["日期"].max() >= today:
        return df
    try:
        spot = get_stock_realtime(symbol, market=market)
    except Exception:
        return df
    if not spot or not spot.get("最新价"):
        return df
    today_row = {
        "日期": today,
        "开盘": spot.get("今开") or spot["最新价"],
        "最高": spot.get("最高") or spot["最新价"],
        "最低": spot.get("最低") or spot["最新价"],
        "收盘": spot["最新价"],
        "成交量": 0,
    }
    return pd.concat([df, pd.DataFrame([today_row])], ignore_index=True)


@st.cache_data(ttl=1800, show_spinner=False)
def get_stock_history(symbol: str, start_date: str, end_date: str, frequency: str = "d", market: str = "A") -> pd.DataFrame:
    # ttl从300秒调到1800秒——实测过：持仓迷你走势图和详情页K线共用这个函数，
    # 5分钟缓存一过期，港股/美股这条走新浪源，单只股票冷取一次实测最慢能到
    # 6秒多(AAPL)，7支持仓并发时被最慢的那个拖累，整批加载能到6-7秒，
    # 这正是"持仓加载慢"复现出来的真实瓶颈。这里存的是"日线颗粒度"的历史
    # 收盘价，真正当天还在变化的"今天"这根K线是靠get_stock_realtime另外
    # 实时拼接进去的(_append_today_bar)，不依赖这份缓存的新鲜度——所以底层
    # 历史数据缓存30分钟不新鲜完全没问题，换来的是把这个慢路径的触发频率
    # 从"每5分钟一次"降到"每30分钟一次"，6倍地减少用户撞见这个坑的概率。
    """历史行情。symbol：A股例如'600519'，港股例如'00700'，美股例如'AAPL'。

    market: A=沪深A股（默认）, HK=港股, US=美股。
    A股：三层兜底 BaoStock（主，稳定免注册）→ 东财 → 新浪，frequency 支持 d/w/m/5/15/30/60。
    港股/美股：新浪源 + 拼今日实时价兜底（见 _append_today_bar）。
    这里特意不用 Futu 的 request_history_kline 当主数据源——实测这个接口延迟不稳定，
    偶尔会卡住十几秒到几分钟不返回，放在默认页面加载路径上风险太大。它仍然接在
    get_stock_kline_futu 里给用户主动切换周期（周K/月K/分时K）时按需尝试，那条路径
    自己有超时保护，卡住也只影响那一次点击，不会拖累首屏。
    """
    if market == "HK":
        df = _with_retry(lambda: _fetch_history_hk(symbol, start_date, end_date), throttle=False)  # 新浪
        return _append_today_bar(df, symbol, market) if frequency == "d" else df
    if market == "US":
        df = _with_retry(lambda: _fetch_history_us(symbol, start_date, end_date), throttle=False)  # 新浪
        return _append_today_bar(df, symbol, market) if frequency == "d" else df

    if frequency != "d":
        return _fetch_history_baostock(symbol, start_date, end_date, frequency)

    for fetch, _needs_throttle in (
        (lambda: _fetch_history_baostock(symbol, start_date, end_date, "d"), False),  # BaoStock，不是东财
        (lambda: ak.stock_zh_a_hist(
            symbol=symbol, period="daily", start_date=start_date, end_date=end_date, adjust="qfq"
        ), True),  # 东财，需要限流保护
        (lambda: _fetch_history_sina(symbol, start_date, end_date), False),  # 新浪，不是东财
    ):
        try:
            df = _with_retry(fetch, retries=1, backoff=3, throttle=_needs_throttle)
            if df is not None and not df.empty:
                return _append_today_bar(df, symbol, market)
        except Exception:
            continue
    raise RuntimeError("三个数据源（BaoStock/东财/新浪）全部获取失败，稍后再试。")


def _run_with_timeout(fn, timeout=8, default=None):
    """通用的"拿子线程跑、主线程最多等timeout秒"包装，跟Futu无关的地方
    （比如别处一次性小任务）可以直接用。卡住的线程会泄漏，但用在低频路径上
    好过卡住整页不返回。Futu相关的调用不要用这个直接包一层ctx.xxx(...)——
    见下面_futu_call的说明，那样会导致连接线程和调用线程不一致。
    """
    q = _queue.Queue(maxsize=1)

    def _worker():
        try:
            q.put(fn())
        except Exception:
            q.put(default)

    threading.Thread(target=_worker, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except _queue.Empty:
        return default


_futu_ctx = None  # 只应该被"当前生效"的那一代worker线程创建/读写，其它地方不要直接碰
_futu_queue = None
_futu_worker_lock = threading.Lock()
_futu_worker_started = False
_futu_last_connect_attempt = 0.0
_futu_fail_count = 0  # 连续失败计数——健康检查用，见 _futu_worker_loop 里的说明
_FUTU_MAX_CONSEC_FAILS = 3  # 连续失败达到这个数就判定连接已失效，主动断开重连
_futu_reconnect_fail_streak = 0  # 连续"重连尝试本身"失败的次数（不是连接建立后的调用失败），见下面退避说明
_FUTU_RECONNECT_BASE_COOLDOWN = 30  # 重连间隔起点（秒）
_FUTU_RECONNECT_MAX_COOLDOWN = 300  # 重连间隔封顶（秒）——指数退避不能无限拉长

# ── 看门狗：兜底"worker线程真的卡死在一次SDK调用里出不来"这种极端情况 ──────────
# _futu_call 给"调用方等多久"设了超时，但那只保护调用方，worker线程内部执行
# fn(_futu_ctx)/candidate.get_global_state() 这些同步阻塞调用本身完全没有
# 超时——如果某一次SDK调用真的悬挂不返回（不是慢，是挂死，_futu_worker_loop
# 的说明里记录过真实先例"90秒以上没有任何响应"，不是不可能），worker线程会
# 卡在那一行永远出不来：不会崩溃、不会报错，就是从此再也不从_futu_queue.get()
# 取下一个任务，Futu功能从那一刻起永久性静默失效，直到进程重启才能恢复
# ——健康检查/自动重连逻辑本身也再没机会跑，因为都在同一个卡住的循环里。
#
# 没有真实Futu环境验证过这一整套看门狗逻辑（包括下面的阈值选取），是纯防御性
# 的兜底，设计原则是"宁可极端情况下慢一点才自愈，也不要在正常场景下误伤"：
_FUTU_WATCHDOG_STALL_SECONDS = 180  # 判定"worker真的卡死"的阈值，特意设得比README
                                     # 记录过的"几十秒到几分钟"最坏首次连接延迟还要更高，
                                     # 尽量避免把正常的慢请求误判成卡死
_futu_heartbeat_at = 0.0  # 当前生效worker"正卡在某次阻塞SDK调用里"的起始时间戳，
                           # 0表示"当前没有正在执行的阻塞调用"，看门狗只在非0时才检查是否超时
_futu_worker_epoch = 0  # 当前生效worker的代号。看门狗一旦判定卡死，会+1并起一个新worker——
                          # 旧worker线程即使将来真的从阻塞调用里返回，也会发现自己的epoch已经
                          # 过期，不再碰任何共享全局状态（不会跟新worker抢_futu_ctx），直接退出
_futu_watchdog_started = False


def _futu_worker_loop(my_epoch: int, my_queue):
    """Futu OpenD 连接的唯一owner线程——所有连接和方法调用永远发生在这一个
    线程里执行。这是让 Futu SDK 稳定工作的硬要求：实测 ctx 在一个线程里
    创建、换另一个线程调用它的方法，会导致 SDK 内部状态错乱直接卡死不返回
    （不是慢，是几十秒到几分钟都拿不到结果，"恒生科技成分股"这个功能踩过，
    排查后发现旧版 _get_futu_ctx() 是在自己开的子线程里连接、把 ctx 传回
    调用方线程使用——这本身就已经犯了这个错，此前各个功能点各自又用
    _run_with_timeout 再包一层去调用 ctx 的方法，等于线程还换了不止一次）。
    现在改成一个进程只有这一个线程碰 ctx：所有请求排队交给它做，调用方
    通过独立的 Queue 拿超时保护的结果，超时不影响这个线程继续处理后面的请求。

    健康检查：之前只有 _futu_ctx is None 时才会尝试重连——一旦连接建立后
    在某个时刻失效（OpenD重启/网络抖动），fn(_futu_ctx) 会持续抛异常，但
    异常被下面的 try/except 吞掉后 _futu_ctx 从不会被置回 None，坏连接就
    会被无限复用、永远不会触发重连。现在加一个连续失败计数：ctx 存在的
    情况下，fn(_futu_ctx) 连续失败达到 _FUTU_MAX_CONSEC_FAILS 次，就主动
    close 掉这个 ctx 并置为 None，让下一次请求走重连分支；只要有一次成功
    就把计数器清零。

    排查页面卡顿时额外发现的问题（在没有真实OpenD的环境里实测到的）：
    futu-api 这个SDK自己的 OpenQuoteContext 一旦被构造出来，内部会起一个
    自己的后台线程去做连接维护——即使这次 get_global_state() 判定失败、
    我们这边把 candidate.close() 掉，SDK内部那个线程实测并不会跟着停，
    会以自己的节奏（实测约6秒一次）持续尝试连接，一直到进程退出。也就是说
    "固定30秒重连一次"这个节流只控制了"我们这边多久创建一次新的
    OpenQuoteContext"，每创建一次失败的candidate就会留下一个SDK自己的
    僵尸重连线程——OpenD长时间断连的情况下，这些僵尸线程会越攒越多，
    每个都在后台悄悄发真实的socket连接尝试，长期运行的服务器进程上这是
    一个会越滚越大的开销来源。这里没法从我们的代码里去"关掉"SDK内部那个
    线程（那是第三方库内部实现，关闭candidate并不能保证连带关掉它），
    唯一能做、也确实有把握做对的缓解手段：连续重连失败时拉长下一次重连
    尝试的间隔（指数退避，30秒翻倍封顶到5分钟）——OpenD长时间没恢复时，
    降低"我们这边又造出一个新candidate、又留下一个新僵尸线程"的频率，
    但不能消除已经留下的僵尸线程，只能减缓新增速度。

    my_epoch/my_queue：这个worker线程自己这一代的身份凭证和专属队列，不是
    每次都从全局变量_futu_queue现读——配合上面的看门狗：一旦看门狗判定这个
    worker卡死、把_futu_worker_epoch/_futu_queue换成新的一代，这个（旧的、
    可能仍在某次阻塞SDK调用里出不来的）线程即使将来真的从阻塞调用里返回，
    也会在每一处touch共享全局状态之前先检查"my_epoch是不是还等于当前生效的
    _futu_worker_epoch"，发现自己已经被弃用就直接放弃这次任务，不写任何全局
    状态、不去关正在被新worker使用的_futu_ctx——避免"两个线程同时碰同一个
    ctx"这个从架构上本来就要杜绝的错误借着这条恢复路径重新出现。Python线程
    没办法被强制杀死，卡死的旧线程本身依然会在后台一直存在直到它自己的阻塞
    调用真的返回（如果永远不返回，这个daemon线程会陪着进程活到退出，不阻塞
    进程本身退出，是可以接受的资源代价）。
    """
    global _futu_ctx, _futu_last_connect_attempt, _futu_fail_count, _futu_reconnect_fail_streak
    global _futu_heartbeat_at
    while True:
        fn, out_q = my_queue.get()
        if my_epoch != _futu_worker_epoch:
            # 已经被看门狗弃用重建过了，不是现役worker——正常不该走到这里，
            # 只有"旧线程从卡死的调用里最终恢复过来、还残留着排队任务"这种
            # 边缘情况才会触发，直接放弃，不碰共享状态。
            out_q.put(None)
            continue
        if _futu_ctx is None:
            now = time.time()
            _reconnect_cooldown = min(
                _FUTU_RECONNECT_BASE_COOLDOWN * (2 ** _futu_reconnect_fail_streak), _FUTU_RECONNECT_MAX_COOLDOWN
            )
            if now - _futu_last_connect_attempt >= _reconnect_cooldown:
                _futu_last_connect_attempt = now
                try:
                    _futu_heartbeat_at = time.time()
                    candidate = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
                    ret, _ = candidate.get_global_state()
                    if my_epoch != _futu_worker_epoch:
                        # 连接过程中被看门狗弃用了——这个candidate跟新worker
                        # 无关，自己关掉，不要让它也变成一个泄漏的连接。
                        try:
                            candidate.close()
                        except Exception:
                            pass
                        out_q.put(None)
                        continue
                    if ret == ft.RET_OK:
                        _futu_ctx = candidate
                        _futu_fail_count = 0
                        _futu_reconnect_fail_streak = 0
                    else:
                        candidate.close()
                        _futu_reconnect_fail_streak += 1
                except Exception:
                    if my_epoch == _futu_worker_epoch:
                        _futu_reconnect_fail_streak += 1
                finally:
                    if my_epoch == _futu_worker_epoch:
                        _futu_heartbeat_at = 0.0
        if my_epoch != _futu_worker_epoch:
            out_q.put(None)
            continue
        if _futu_ctx is not None:
            try:
                _futu_heartbeat_at = time.time()
                result = fn(_futu_ctx)
                if my_epoch == _futu_worker_epoch:
                    _futu_fail_count = 0
            except Exception:
                result = None
                if my_epoch == _futu_worker_epoch:
                    _futu_fail_count += 1
                    if _futu_fail_count >= _FUTU_MAX_CONSEC_FAILS:
                        try:
                            _futu_ctx.close()
                        except Exception:
                            pass
                        _futu_ctx = None
                        _futu_fail_count = 0
            finally:
                if my_epoch == _futu_worker_epoch:
                    _futu_heartbeat_at = 0.0
        else:
            result = None
        out_q.put(result)


def _futu_watchdog_loop():
    """看门狗——独立线程，只读一个心跳时间戳，不碰 _futu_ctx、不调用任何SDK
    方法。每隔一小段时间醒来检查一次：如果当前生效worker"正卡在某次阻塞调用
    里"的时间已经超过 _FUTU_WATCHDOG_STALL_SECONDS，判定这一代worker已经卡死
    救不回来了——换一个全新的queue、把_futu_ctx置空、epoch+1，起一个全新的
    worker线程接手。旧worker线程和它可能还攥着的ctx原地放着自生自灭（daemon
    线程，不阻塞进程退出），新提交的_futu_call请求全部会排到新worker这边，
    不会再排到一个永远不会被处理的旧队列里。

    没有真实Futu环境验证过这套逻辑——包括阈值选取是否合适、真发生卡死时是否
    确实能让功能恢复正常。上线后需要留意健康面板/日志确认没有误伤正常慢请求，
    也确认真卡死时这条恢复路径确实生效。
    """
    global _futu_queue, _futu_ctx, _futu_worker_epoch, _futu_heartbeat_at
    global _futu_last_connect_attempt, _futu_fail_count, _futu_reconnect_fail_streak
    while True:
        time.sleep(10)
        with _futu_worker_lock:
            hb = _futu_heartbeat_at
            if not hb or time.time() - hb <= _FUTU_WATCHDOG_STALL_SECONDS:
                continue
            _futu_queue = _queue.Queue()
            _futu_ctx = None
            _futu_fail_count = 0
            _futu_heartbeat_at = 0.0
            # 不清零 _futu_reconnect_fail_streak/_futu_last_connect_attempt：
            # 判定卡死本身就说明这次连接不健康，退避节奏应该延续，不要因为
            # 换了一代worker就重新给一次"立刻重连"的优待。
            _futu_worker_epoch += 1
            threading.Thread(
                target=_futu_worker_loop, args=(_futu_worker_epoch, _futu_queue), daemon=True,
            ).start()


def _ensure_futu_worker():
    global _futu_queue, _futu_worker_started, _futu_watchdog_started
    if _futu_worker_started:
        return
    with _futu_worker_lock:
        if _futu_worker_started:
            return
        _futu_queue = _queue.Queue()
        threading.Thread(target=_futu_worker_loop, args=(_futu_worker_epoch, _futu_queue), daemon=True).start()
        threading.Thread(target=_futu_watchdog_loop, daemon=True).start()
        _futu_watchdog_started = True
        _futu_worker_started = True


def _futu_call(fn, timeout: float = 8, default=None):
    """所有 Futu 查询的统一入口，取代旧的 "ctx = _get_futu_ctx(); ctx.xxx(...)"
    写法。fn 接收 ctx 并返回结果；ctx 还没连上/连接失败/排队超时/fn 抛异常，
    统一按 default 处理（fn 正常返回 None 的情况这里认为不存在——Futu SDK的
    调用永远是 (ret, data) 元组，跟 default 的失败语义不冲突）。

    注意：get_hstech_constituents/get_futu_news 这两处没有走这个统一入口，
    是刻意的例外——它们各自在自己的临时子线程里"连接+查询+关闭"一次性做完，
    不碰这里的常驻worker/共享_futu_ctx，同样满足"连接和调用同一线程"这条硬
    规则，只是不复用常驻连接。这么做是因为这两个查询本身不追求复用长连接，
    独立短连接反而更简单可控；缺点是两套Futu访问方式并存，以后新增Futu调用点
    时要留意选对模式，不要想当然地复用常驻ctx。详见这两个函数各自的说明。
    """
    if not _FUTU_SDK_AVAILABLE:
        return default
    _ensure_futu_worker()
    out_q = _queue.Queue(maxsize=1)
    _futu_queue.put((fn, out_q))
    try:
        result = out_q.get(timeout=timeout)
    except _queue.Empty:
        result = None
    return default if result is None else result


_BREAKER_DISPLAY_NAMES = {
    "index_snapshot_A": "A股指数快照兜底",
    "index_snapshot_HK": "港股指数快照兜底",
    "index_snapshot_US": "美股指数快照兜底",
    "limit_pool_up": "涨停池",
    "limit_pool_down": "跌停池",
    "hk_movers_fallback": "港股行情兜底",
}


def get_data_source_health() -> dict:
    """给侧边栏"数据源健康状态"小面板用的轻量快照——只读现有的连接/熔断状态，
    不主动发起任何新的探测请求（看一眼状态面板不应该反而多打一次网络请求）。

    - Futu：读 _futu_worker_loop 本来就在维护的几个全局变量（_futu_ctx 是否
      为 None、连续失败次数、上次尝试连接的时间戳），这是唯一权威的连接状态，
      不重新连一次去"验证"。
    - 兜底数据源熔断情况：读共享熔断器 _breaker_state。这个字典只在真正触发过
      兜底失败时才会写入（走通正常路径根本不会碰它），能查到记录只能读作
      "曾经失败过、可能还在冷却"，查不到不代表"已确认成功过"——这里如实
      按这个口径展示，不虚构一个"成功时间"出来。
    """
    futu_status = {
        "已安装SDK": _FUTU_SDK_AVAILABLE,
        "已连接": _futu_ctx is not None,
        "连续失败次数": _futu_fail_count,
        "上次尝试连接": _futu_last_connect_attempt or None,
        # 重连退避：连续重连失败次数越多，下次重连间隔越长（30秒翻倍封顶
        # 5分钟），见_futu_worker_loop里的说明——这里如实展示当前的等待间隔，
        # 而不是让用户以为"没连上"就该立刻重试。
        "下次重连间隔秒": (
            min(_FUTU_RECONNECT_BASE_COOLDOWN * (2 ** _futu_reconnect_fail_streak), _FUTU_RECONNECT_MAX_COOLDOWN)
            if _futu_ctx is None else None
        ),
    }

    now = time.time()
    breakers = []
    for key, trip_until in sorted(_breaker_state.items()):
        cooling = now < trip_until
        breakers.append({
            "key": key,
            "名称": _BREAKER_DISPLAY_NAMES.get(key, key),
            "冷却中": cooling,
            "最近一次失败": trip_until - 60,  # 目前所有调用点的cooldown都是默认60秒
        })
    return {"futu": futu_status, "熔断记录": breakers}


def get_stock_realtime_futu(symbol: str, market: str) -> dict:
    """走本地 Futu OpenD 网关拿真实时快照，只支持港股/美股（A股无权限）。

    market 检查放在最前面——A股走这函数是必然返回空的，没必要为此白连一次 Futu。
    """
    if market not in ("HK", "US"):
        return {}
    code = f"HK.{symbol}" if market == "HK" else f"US.{symbol}"
    ret, data = _futu_call(lambda ctx: ctx.get_market_snapshot([code]), default=(None, None))
    if ret != ft.RET_OK or data is None or data.empty:
        return {}
    row = data.iloc[0]
    prev_close = float(row["prev_close_price"])
    last = float(row["last_price"])
    if not prev_close:
        return {}
    return {
        "代码": symbol,
        "名称": str(row["name"]),
        "最新价": last,
        "今开": float(row["open_price"]),
        "昨收": prev_close,
        "最高": float(row["high_price"]),
        "最低": float(row["low_price"]),
        "涨跌额": last - prev_close,
        "涨跌幅": (last - prev_close) / prev_close * 100,
        "成交额": float(row["turnover"]) if pd.notna(row.get("turnover")) else None,
        "更新时间": str(row["update_time"]),
        "数据源": "Futu实时",
    }


@st.cache_data(ttl=300, show_spinner=False)
def get_futu_news(keyword: str, max_count: int = 8) -> pd.DataFrame:
    """走 Futu OpenD 的资讯搜索（get_search_news）——这是目前找到的最好的新闻源：
    真按关键词匹配（不是财新那种整段大盘资讯里瞎找子串），A股/港股/美股通吃
    （财新的公司新闻只覆盖到个别大公司，港股/美股基本没东西），链接指向
    news.futunn.com（富途自己的资讯站，公开可读，不需要富途账号订阅）。
    只有本机/服务器跑了 OpenD 才能用，连不上就静默返回空，上层自然会退回
    财新兜底。用 NewsSubType.NEWS 过滤掉窝轮牛熊证估值公告这类噪音。

    这里不复用全局共享的 _get_futu_ctx()——那个 ctx 是在后台线程里连接、
    传回主线程长期持有的，别的地方都是同步直调，一旦再套一层 _run_with_timeout
    去调用它的方法，就变成"连接和调用不是同一个线程"，实测会直接卡死（跟
    get_stock_realtime_futu 那边记录的教训一样）。资讯搜索这个功能不追求
    复用连接，干脆自己开一个独立短连接，连接+查询+关闭全在同一个子线程里
    做完，既不碰共享 ctx，又有超时兜底，卡住了子线程自己泄漏，主线程最多
    等 timeout 秒就拿到空结果退回财新兜底。
    """
    if not _FUTU_SDK_AVAILABLE:
        return pd.DataFrame()

    def _fetch():
        ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            return ctx.get_search_news(keyword, max_count=max_count, news_sub_type=ft.NewsSubType.NEWS)
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    try:
        result = _run_with_timeout(_fetch, timeout=8, default=None)
    except Exception:
        result = None
    if result is None:
        return pd.DataFrame()
    ret, data = result
    if ret != ft.RET_OK or data is None or data.empty:
        return pd.DataFrame()

    def _full_date(s: str) -> str:
        # publish_time 只给"7/17"这种月/日，没有年份——都是最近的资讯，直接拼当前年份。
        try:
            m, d = s.strip().split("/")
            today = datetime.now()
            year = today.year
            guess = datetime(year, int(m), int(d))
            if guess > today + timedelta(days=1):
                year -= 1
            return f"{year}-{int(m):02d}-{int(d):02d}"
        except Exception:
            return s

    df = data.copy()
    df["日期"] = df["publish_time"].apply(_full_date)
    df = df.sort_values("日期", ascending=False)
    df = df.rename(columns={"title": "新闻标题", "source": "分类"})
    return df[["日期", "新闻标题", "分类", "url"]]


_FUTU_KTYPE_MAP = {"日K": "K_DAY", "周K": "K_WEEK", "月K": "K_MON"}
_FUTU_DAYS_BACK = {"日K": 90, "周K": 730, "月K": 1825}


@st.cache_data(ttl=30, show_spinner=False)
def get_stock_kline_futu(symbol: str, market: str, period: str) -> pd.DataFrame:
    """走 Futu OpenD 拿真实K线（日/周/月），不是拿日线硬凑的。只支持港股/美股。

    "今日分时" 不走这个函数——那是真正的每分钟连续走势图，跟这里的K线柱状图
    是两种不同的图表形态，见 get_stock_intraday_futu。

    用 request_history_kline —— 这个接口不需要订阅，只占历史K线额度，
    同一股票30天内重复查也不重复扣。
    """
    if market not in ("HK", "US") or period not in _FUTU_KTYPE_MAP:
        return pd.DataFrame()
    code = f"HK.{symbol}" if market == "HK" else f"US.{symbol}"
    ktype = getattr(ft.KLType, _FUTU_KTYPE_MAP[period])
    days_back = _FUTU_DAYS_BACK[period]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # 这个接口实测延迟不稳定（偶尔卡住半天不返回）——套超时兜底，这条路径只在
    # 用户主动切周期时触发，卡的话最多耽误这一次点击（_futu_call本身已经保证
    # 连接和调用在同一个线程，这里的timeout只是"等结果"的超时，不是线程隔离）。
    result = _futu_call(
        lambda ctx: ctx.request_history_kline(
            code, start=start, end=end, ktype=ktype, autype=ft.AuType.QFQ, max_count=1000,
        ),
        timeout=8, default=None,
    )
    if result is None:
        return pd.DataFrame()
    ret, data, _ = result
    if ret != ft.RET_OK or data is None or data.empty:
        return pd.DataFrame()
    df = data.rename(columns={
        "time_key": "日期", "open": "开盘", "close": "收盘",
        "high": "最高", "low": "最低", "volume": "成交量",
    })
    df["日期"] = pd.to_datetime(df["日期"])
    for col in ("开盘", "收盘", "最高", "最低", "成交量"):
        df[col] = df[col].astype(float)
    return df[["日期", "开盘", "收盘", "最高", "最低", "成交量"]]


_futu_subscribed = {}  # code -> 订阅时刻(time.time())，用来做超过1分钟才允许反订阅的判断
_FUTU_SUB_LIMIT = 80  # 免费账户额度上限100，留点余量给别的功能用


def _futu_ensure_subscribed(ctx, code: str) -> bool:
    """分时数据（RT_DATA）是订阅制的，调用前必须先 subscribe，且额度有限。

    简单的额度管理：已订阅就直接放行；额度满了就找一个订阅超过60秒的（Futu规定
    订阅后至少1分钟才能反订阅）踢掉腾地方；实在腾不出来就订阅失败，上层退回K线兜底。

    只会在 _futu_call 派给 worker 线程的任务内部被调用，同步直调 ctx 的方法
    即可——不需要再套一层线程超时（那样反而会制造"连接线程≠调用线程"的问题）。
    """
    now = time.time()
    if code in _futu_subscribed:
        return True
    if len(_futu_subscribed) >= _FUTU_SUB_LIMIT:
        evictable = [c for c, t in _futu_subscribed.items() if now - t > 60]
        if not evictable:
            return False
        oldest = min(evictable, key=lambda c: _futu_subscribed[c])
        try:
            ctx.unsubscribe([oldest], [ft.SubType.RT_DATA])
        except Exception:
            pass
        _futu_subscribed.pop(oldest, None)
    try:
        ret, _ = ctx.subscribe([code], [ft.SubType.RT_DATA])
    except Exception:
        return False
    if ret != ft.RET_OK:
        return False
    _futu_subscribed[code] = now
    return True


def _futu_last_day_intraday(ctx, code: str) -> pd.DataFrame:
    """今日没有分时数据时（周末/节假日/还没开盘）的兜底——用历史1分钟K线接口
    （不需要订阅），取最近一个交易日的分钟线当分时用。跟只剩日K比，好歹还是
    分时的形状。同样只在 _futu_call 的 worker 线程任务里被调用，同步直调。
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        ret, data, _ = ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_1M, autype=ft.AuType.QFQ, max_count=1000,
        )
    except Exception:
        return pd.DataFrame()
    if ret != ft.RET_OK or data is None or data.empty:
        return pd.DataFrame()
    data = data.rename(columns={"time_key": "时间", "close": "价格", "volume": "成交量"})
    data["时间"] = pd.to_datetime(data["时间"])
    data["日期_only"] = data["时间"].dt.date
    last_date = data["日期_only"].max()
    data = data[data["日期_only"] == last_date]
    data["价格"] = data["价格"].astype(float)
    data["成交量"] = data["成交量"].astype(float)
    return data[["时间", "价格", "成交量"]]


def _futu_intraday_by_code(code: str) -> pd.DataFrame:
    """真分时数据的公共取数逻辑，个股和指数都走这条路，区别只在 code 怎么拼。

    订阅+取数这一步整体包进同一个 _futu_call，保证跟ctx有关的操作都在
    Futu专属worker线程里连续完成，不会因为中途换线程调用ctx方法而卡死。
    """
    def _fetch(ctx):
        if not _futu_ensure_subscribed(ctx, code):
            return ("empty", None)
        try:
            ret, raw = ctx.get_rt_data(code)
        except Exception:
            ret, raw = None, None
        if ret == ft.RET_OK and raw is not None and not raw.empty:
            return ("data", raw)
        return ("last_day", None)

    kind, raw = _futu_call(_fetch, timeout=10, default=("empty", None))
    if kind == "data":
        data = raw
        # is_blank=True 是午间休市那种没有真实成交的占位行（价格是拿上一个真实价格填的），
        # 保留会在图上画出一段假的平线——这些行本来就不该出现在真分时曲线里。
        if "is_blank" in data.columns:
            data = data[~data["is_blank"].astype(bool)]
        df = data.rename(columns={"time": "时间", "cur_price": "价格", "volume": "成交量"})
        df["时间"] = pd.to_datetime(df["时间"])
        df["价格"] = df["价格"].astype(float)
        df["成交量"] = df["成交量"].astype(float)
        df = df[df["价格"] > 0]
        if not df.empty:
            return df[["时间", "价格", "成交量"]]
        kind = "last_day"
    if kind == "last_day":
        return _futu_call(lambda ctx: _futu_last_day_intraday(ctx, code), timeout=10, default=pd.DataFrame())
    return pd.DataFrame()


def _sina_minute_intraday(sina_code: str) -> pd.DataFrame:
    """新浪分钟线的公共取数逻辑，个股和指数都走这条路，区别只在代码格式怎么拼。

    今天没有分时数据（周末/节假日/还没开盘）就退到接口返回的历史里最近一个
    交易日的分时——跟只剩日K比，好歹还是分时的形状，视觉上更一致。

    这里原来是裸调用 ak.stock_zh_a_minute，没有 _with_retry 也没有 try/except——
    调用方（get_stock_intraday_a/get_index_intraday_a）又是"分时K（今日）"这个
    st.radio默认选中的第一个选项（app.py），意味着新浪分时接口一抖动，打开任意
    A股个股/指数详情页的默认视图就直接整页报错，而不是设计文档里说好的"退化成
    日K"。改成异常兜底返回空DataFrame——跟这个函数一贯"取不到就返回空表"的
    约定一致，调用方原有的 `if intraday.empty` 判断不用改就能正确降级。
    """
    try:
        with _akshare_js_lock:
            df = ak.stock_zh_a_minute(symbol=sina_code, period="1")
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    today = datetime.now().strftime("%Y-%m-%d")
    todays = df[df["day"].str.startswith(today)]
    if todays.empty:
        date_only = df["day"].str[:10]
        last_date = date_only.max()
        todays = df[date_only == last_date]
    if todays.empty:
        return pd.DataFrame()
    df = todays.rename(columns={"day": "时间", "close": "价格", "volume": "成交量"})
    df["时间"] = pd.to_datetime(df["时间"])
    df["价格"] = df["价格"].astype(float)
    df["成交量"] = df["成交量"].astype(float)
    return df[["时间", "价格", "成交量"]]


@st.cache_data(ttl=20, show_spinner=False)
def get_stock_intraday_a(symbol: str) -> pd.DataFrame:
    """A股个股真分时——走新浪的分钟线接口（ak.stock_zh_a_minute），不是 BaoStock。
    BaoStock 的 5 分钟线是 EOD 数据，交易时段内查不到"今天"；这个新浪接口实测
    是真新鲜的，一直更新到最新一分钟。一次会拉回最近多天历史（接口不支持只要
    某一天），这里过滤出今天的。
    """
    return _sina_minute_intraday(f"{_sina_symbol(symbol)}{symbol}")


@st.cache_data(ttl=20, show_spinner=False)
def get_index_intraday_a(code: str) -> pd.DataFrame:
    """A股指数真分时，code 是 BaoStock 格式（如 sh.000001），换成新浪格式（sh000001）。"""
    return _sina_minute_intraday(code.replace(".", ""))


@st.cache_data(ttl=20, show_spinner=False)
def get_stock_intraday_futu(symbol: str, market: str) -> pd.DataFrame:
    """真正的分时走势——今天从开盘到现在每分钟的价格连续曲线，不是K线柱状图。

    走 Futu 的 get_rt_data，订阅制接口（跟不需要订阅的历史K线是两码事）。
    刚订阅上那一刻数据可能还没推送到，接口本身返回码是成功的但行数是0，
    这里不做同步等待重试——留给上层根据空结果自然退化到日K，避免页面卡住等推送。
    """
    if market not in ("HK", "US"):
        return pd.DataFrame()
    code = f"HK.{symbol}" if market == "HK" else f"US.{symbol}"
    return _futu_intraday_by_code(code)


# 恒生系列指数在 Futu 里走独立的指数代码（跟股票代码格式不一样）。
# 美股指数 Futu 目前不支持原生代码（实测 US.SPX/US.IXIC/US.DJI 都查不到），
# A股在这个账号下压根没有 Futu 权限，所以指数分时只做港股这一档。
_HK_INDEX_FUTU_CODE = {"恒生指数": "800000", "恒生科技": "800700", "国企指数": "800100"}


@st.cache_data(ttl=20, show_spinner=False)
def get_index_intraday_futu(name: str, market: str, index_prev_close: float | None = None) -> pd.DataFrame:
    """指数版真分时。港股走原生指数代码；美股没有原生指数代码，用对应ETF的分时
    价格按比例缩放成指数点位的估算值（要传 index_prev_close 才能换算，不传就
    只支持港股）。"""
    if market == "HK":
        if name not in _HK_INDEX_FUTU_CODE:
            return pd.DataFrame()
        return _futu_intraday_by_code(f"HK.{_HK_INDEX_FUTU_CODE[name]}")

    if market == "US":
        etf = _US_INDEX_ETF_PROXY.get(name)
        if not etf or not index_prev_close:
            return pd.DataFrame()
        code = f"US.{etf}"
        ret, snap = _futu_call(lambda ctx: ctx.get_market_snapshot([code]), timeout=5, default=(None, None))
        if ret != ft.RET_OK or snap is None or snap.empty:
            return pd.DataFrame()
        etf_prev_close = float(snap.iloc[0]["prev_close_price"])
        if not etf_prev_close:
            return pd.DataFrame()
        df = _futu_intraday_by_code(code)
        if df.empty:
            return df
        df = df.copy()
        df["价格"] = df["价格"] * (index_prev_close / etf_prev_close)
        return df

    return pd.DataFrame()


def _a_index_snapshot_tencent(code: str) -> dict | None:
    """A股指数的实时快照，走腾讯行情接口，跟个股实时价（get_stock_realtime）
    是同一套字段格式——指数代码在腾讯那边跟个股一样能查，不是独立的一套接口。
    code 是 BaoStock 格式（如 sh.000001），转成腾讯格式（sh000001）。
    """
    tc_code = code.replace(".", "")
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={tc_code}", timeout=8)
        fields = _tencent_quote_fields(r.content.decode("gbk", errors="ignore"))
    except Exception:
        return None
    if fields is None:
        return None
    last, prev = float(fields[3]), float(fields[4])
    if not prev:
        return None
    change = last - prev
    return {"最新": last, "涨跌": change, "涨跌幅": change / prev * 100}


def _hk_index_snapshot_futu(name: str) -> dict | None:
    """恒生系列指数的实时快照（get_multi_index_snapshot 的港股分支用），
    不需要订阅，无 OpenD 连接或指数不在名单里就返回 None，让上层退回新浪日线兜底。
    """
    futu_code = _HK_INDEX_FUTU_CODE.get(name)
    if not futu_code:
        return None
    code = f"HK.{futu_code}"
    ret, data = _futu_call(lambda ctx: ctx.get_market_snapshot([code]), default=(None, None))
    if ret != ft.RET_OK or data is None or data.empty:
        return None
    row = data.iloc[0]
    last = float(row["last_price"])
    prev = float(row["prev_close_price"])
    if not prev:
        return None
    change = last - prev
    return {"最新": last, "涨跌": change, "涨跌幅": change / prev * 100}


# Futu 不支持美股原生指数代码（实测 US.SPX/US.IXIC/US.DJI 全部查不到），
# 用对应ETF的实时涨跌幅做代理——ETF跟踪对应指数基本1:1，涨跌方向和幅度
# 可信，只是绝对点位跟真实指数会有极小的跟踪误差。
_US_INDEX_ETF_PROXY = {"标普500": "SPY", "纳斯达克100": "QQQ", "道琼斯": "DIA"}


def _us_index_snapshot_futu(name: str, index_prev_close: float) -> dict | None:
    """用ETF实时涨跌幅 + 指数自己的昨收（来自新浪EOD数据）换算出指数的估算实时点位。"""
    etf = _US_INDEX_ETF_PROXY.get(name)
    if not etf or not index_prev_close:
        return None
    code = f"US.{etf}"
    ret, data = _futu_call(lambda ctx: ctx.get_market_snapshot([code]), default=(None, None))
    if ret != ft.RET_OK or data is None or data.empty:
        return None
    row = data.iloc[0]
    etf_last = float(row["last_price"])
    etf_prev = float(row["prev_close_price"])
    if not etf_prev:
        return None
    pct = (etf_last - etf_prev) / etf_prev * 100
    last = index_prev_close * (1 + pct / 100)
    return {"最新": last, "涨跌": last - index_prev_close, "涨跌幅": pct}


def _tencent_symbol(symbol: str, market: str) -> str:
    """把股票代码转成腾讯行情接口(qt.gtimg.cn)要的前缀格式。"""
    if market == "HK":
        return f"hk{symbol}"
    if market == "US":
        return f"us{symbol.upper()}"
    return f"{_sina_symbol(symbol)}{symbol}"


def _tencent_quote_fields(text: str) -> list[str] | None:
    """腾讯行情接口单条返回解析成字段列表，格式不对/没数据返回None。"""
    if '"' not in text:
        return None
    raw = text.split('"')[1]
    fields = raw.split("~")
    if len(fields) < 35 or not fields[3]:
        return None
    return fields


@st.cache_data(ttl=86400, show_spinner=False)
def _fund_name_table() -> pd.DataFrame:
    """场外(OTC)基金全量代码/简称表，一天缓存一次——这张表2万7千多行，不是
    行情接口，一天更新一次完全够用，不用像股价那样3秒刷新。"""
    try:
        return ak.fund_name_em()
    except Exception:
        return pd.DataFrame()


def _fund_name_by_code(symbol: str) -> str:
    df = _fund_name_table()
    if df.empty:
        return symbol
    row = df[df["基金代码"] == symbol]
    return row.iloc[0]["基金简称"] if not row.empty else symbol


def search_otc_fund_by_name(query: str) -> dict | None:
    """OTC联接基金(比如"广发恒生科技ETF联接(QDII)C")没有交易所代码，
    search_stock_by_name(BaoStock)和_A_FUND_NAME_MAP(手动维护的场内ETF
    别名表)都覆盖不到——这类基金全市场有几万只，不可能手动维护别名表，
    只能用akshare的全量基金名录做子串匹配。返回最短匹配的那个(名字越短
    越可能是用户想要的那一个，不是子串匹配到的某个长尾变体)，找不到
    返回None。"""
    df = _fund_name_table()
    if df.empty:
        return None
    hits = df[df["基金简称"].str.contains(query, na=False, regex=False)]
    if hits.empty:
        return None
    hits = hits.assign(_len=hits["基金简称"].str.len()).sort_values("_len")
    row = hits.iloc[0]
    return {"symbol": row["基金代码"], "name": row["基金简称"]}


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_otc_fund_quote(symbol: str) -> dict:
    """OTC联接基金——跟场内ETF不是一回事，没有实时盘口，一个交易日只在
    收盘后更新一次单位净值（T-1，不是"今天"的价格，是上一个披露日的）。
    "今日收益"这类3秒刷新的功能对这类标的没有意义，但至少能记持仓算
    浮盈浮亏——用户明确要求过要能添加自己买的场外基金，有比没有强。

    30分钟缓存——净值一天只披露一次，被get_stock_realtime(ttl=3)包着的
    持仓列表/今日收益fragment每3秒就会调一次这个函数，不加缓存等于每3秒
    真打一次akshare请求，纯粹浪费还可能把这个本来就不算特别稳的接口打
    出限流，加缓存后这块请求量降到可以忽略。
    """
    try:
        df = ak.fund_open_fund_info_em(symbol=symbol, indicator="单位净值走势")
    except Exception:
        return {}
    if df is None or df.empty or "单位净值" not in df.columns:
        return {}
    df = df.sort_values("净值日期")
    last, prev = df.iloc[-1], (df.iloc[-2] if len(df) > 1 else df.iloc[-1])
    last_price, prev_close = float(last["单位净值"]), float(prev["单位净值"])
    return {
        "代码": symbol, "名称": _fund_name_by_code(symbol),
        "最新价": last_price, "昨收": prev_close,
        "今开": last_price, "最高": last_price, "最低": last_price,
        "成交额": None, "更新时间": str(last["净值日期"]),
        "数据源": "场外基金净值(T-1)",
    }


# 上金所现货合约——没有交易所代码，用户可能搜"AU9999"这种行业内简写，
# 内部映射到akshare(ak.spot_hist_sge)要求的"Au99.99"格式。跟_US_NAME_MAP里
# 用ETF代表大宗商品是两条不同的路：那边是"没有更合适的就用最像的ETF代理"，
# 这里是用户明确想要上金所人民币计价的现货合约本身，两者都保留，用户
# 搜哪个给哪个。
_SGE_SYMBOL_MAP = {
    "AU9999": "Au99.99", "沪金": "Au99.99", "SGE黄金": "Au99.99",
    "AG9999": "Ag99.99", "沪银": "Ag99.99", "SGE白银": "Ag99.99",
}


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_sge_spot_quote(symbol: str) -> dict:
    """上金所现货——T-1日线数据（akshare没有这个的实时盘口接口），道理跟
    OTC基金一样：没有实时刷新的意义，但至少能记录持仓算浮盈浮亏。30分钟
    缓存的理由同_fetch_otc_fund_quote——不加缓存会被3秒刷新的持仓fragment
    重复打接口，数据本身一天根本不会变。"""
    sge_code = _SGE_SYMBOL_MAP.get(symbol.upper())
    if not sge_code:
        return {}
    try:
        df = ak.spot_hist_sge(symbol=sge_code)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    df = df.sort_values("date")
    last, prev = df.iloc[-1], (df.iloc[-2] if len(df) > 1 else df.iloc[-1])
    return {
        "代码": symbol, "名称": f"上海金交所{sge_code}",
        "最新价": float(last["close"]), "昨收": float(prev["close"]),
        "今开": float(last["open"]), "最高": float(last["high"]), "最低": float(last["low"]),
        "成交额": None, "更新时间": str(last["date"]),
        "数据源": "上金所现货(T-1)",
    }


@st.cache_data(ttl=3, show_spinner=False)
def get_stock_realtime(symbol: str, market: str = "A") -> dict:
    """真正的实时行情，港股/美股优先走本地 Futu OpenD 网关，A股 + Futu查不到时
    走腾讯行情接口(qt.gtimg.cn)兜底——跟富途互补，一个管港美股实时+分时，
    一个管A股 + 港美股在Futu掉线时的兜底。

    之前这条兜底路径走的是新浪(hq.sinajs.cn)：把跳动周期从15秒缩到3秒后，
    新浪/东财这两个免费接口的请求量直接翻了5倍，实测被限流/拒绝了
    （hq.sinajs.cn直接Network unreachable），用户反馈"invest agent加载变慢"。
    腾讯这条线批量查/单只查都测过，实测稳定，索性把新浪整个换掉，不再维护
    "先试新浪、超时再退腾讯"这条更复杂也更容易被拖慢的双轨路径。

    之前这里是从日线历史数据里取最后一行——那是"最近收盘价"，交易时段内
    跟用户自己在别的地方看到的实时价格对不上。这个接口是轻量单股查询，
    只查一只股票、不拉全市场。缓存 TTL 跟详情页/持仓的实时价格区块
    （st.fragment 每 3 秒自动刷新）对齐——TTL 比刷新周期长的话，fragment
    每次"刷新"其实只是重画同一份缓存值，数字并不会真的跳动。
    """

    futu_data = get_stock_realtime_futu(symbol, market)
    if futu_data:
        return futu_data

    def _fetch():
        code = _tencent_symbol(symbol, market)
        r = requests.get(f"https://qt.gtimg.cn/q={code}", timeout=8)
        fields = _tencent_quote_fields(r.content.decode("gbk", errors="ignore"))
        if fields is None:
            return {}
        # 成交额单位A股是"万元"，港股/美股这个字段本身就是原始货币单位，
        # 只有A股需要乘10000（跟field[35]里拼进去的精确成交额反推验证过）。
        turnover = float(fields[37]) if fields[37] else None
        if turnover is not None and market == "A":
            turnover *= 10000
        return {
            "代码": symbol, "名称": fields[1],
            "最新价": float(fields[3]), "今开": float(fields[5]),
            "昨收": float(fields[4]), "最高": float(fields[33]), "最低": float(fields[34]),
            "成交额": turnover,
            "更新时间": fields[30],
        }

    tencent_data = _with_retry(_fetch, retries=1, backoff=2, throttle=False)
    if tencent_data:
        return tencent_data

    # 交易所行情(Futu/腾讯)都查不到，A股范围内再试两类非交易所标的：
    # 场外联接基金(OTC，走净值不走盘口)、上金所现货合约。两者都是T-1
    # 数据，"今日收益"这类实时刷新场景对它们没意义，但至少能记持仓。
    if market == "A":
        otc_data = _fetch_otc_fund_quote(symbol)
        if otc_data:
            return otc_data
        sge_data = _fetch_sge_spot_quote(symbol)
        if sge_data:
            return sge_data
    return {}


_MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD"}
_FX_CACHE_PATH = Path(__file__).parent / "data" / "fx_rate_cache.json"

# 实测记录（2026-08-21，同一天从Mac和VPS两台机器分别测）：东财push2的CNH
# 汇率端点(push2.eastmoney.com/api/qt/stock/get?secid=133.USDCNH)连续多次
# 502，不是网络问题，是这个端点本身不稳定——所以这里把新浪当主源、东财降级
# 成第二尝试，不是随手选的顺序。两条路径都要用离岸(CNH)不用在岸(CNY)，
# 离岸/在岸差价约0.1%，主源切兜底时口径要一致，否则总资产会莫名跳一下。
_SINA_FX_CODE = {"USD": "fx_susdcnh", "HKD": "fx_shkdcny"}
_EASTMONEY_FX_SECID = {"USD": "133.USDCNH", "HKD": "133.HKDCNH"}


def _fetch_fx_sina(currency: str) -> float | None:
    code = _SINA_FX_CODE.get(currency)
    if not code:
        return None
    r = requests.get(
        f"https://hq.sinajs.cn/list={code}",
        headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"},
        timeout=8,
    )
    text = r.content.decode("gbk", errors="ignore")
    fields = text.split('"')[1].split(",") if '"' in text else []
    if len(fields) < 3:
        return None
    bid, ask = float(fields[1]), float(fields[2])
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


def _fetch_fx_eastmoney(currency: str) -> float | None:
    secid = _EASTMONEY_FX_SECID.get(currency)
    if not secid:
        return None
    r = requests.get(
        f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f59",
        timeout=8,
    )
    data = r.json().get("data")
    if not data or not data.get("f43"):
        return None
    scale = 10 ** data.get("f59", 4)  # f59是小数位数，读不到就按已知的4位兜底
    return data["f43"] / scale


def _load_fx_cache() -> dict:
    try:
        return json.loads(_FX_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_fx_cache(cache: dict):
    try:
        _FX_CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass


@st.cache_data(ttl=1800, show_spinner=False)
def get_fx_rate(currency: str, quote: str = "CNY") -> tuple[float | None, str]:
    """currency兑quote的汇率，目前只支持quote="CNY"（HKD/USD → CNY，A股本身
    就是CNY不用转）。1800秒缓存——汇率日内波动幅度远小于股价，不需要像行情
    那样3秒刷新，全塞进热路径纯粹浪费还会把限流风险引到最热的路径上。

    返回 (汇率, 数据时间说明)——第二个值不是摆设：两条实时源都失败时会退回
    本地缓存的上一次成功值，这时候必须让调用方能告诉用户"这个汇率不是实时的、
    是XX时候取的"，不能悄悄拿一个过期数字充当实时数据用。
    """
    if currency == quote:
        return 1.0, "实时"
    if currency not in _MARKET_CURRENCY.values():
        return None, "不支持的币种"

    for fetch, name in ((_fetch_fx_sina, "新浪"), (_fetch_fx_eastmoney, "东财")):
        try:
            rate = _with_retry(lambda f=fetch: f(currency), retries=1, backoff=2, throttle=False)
        except Exception:
            rate = None
        if rate:
            cache = _load_fx_cache()
            cache[currency] = {"rate": rate, "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            _save_fx_cache(cache)
            return rate, "实时"

    # 两条实时源都失败——读本地缓存的上一次成功值，不编一个常数糊弄
    cached = _load_fx_cache().get(currency)
    if cached:
        return cached["rate"], f"非实时，取自{cached['fetched_at']}"
    return None, "汇率获取失败"


def to_cny(amount: float, currency: str) -> tuple[float | None, str]:
    rate, note = get_fx_rate(currency)
    if rate is None:
        return None, note
    return amount * rate, note


@st.cache_data(ttl=600, show_spinner=False)
def get_financial_abstract(symbol: str, market: str = "A") -> pd.DataFrame:
    """财务摘要指标。A股是东财"股票财务摘要"接口；港股/美股是东财对应的分析指标接口，
    字段跟A股完全不是一回事（更细、列更多），直接原样返回给AI消化，不强行对齐格式。

    _with_retry 重试耗尽后是 raise 而不是返回 None——调用方（app.py 的"财务摘要"模块）
    原来直接拿这个函数的返回值，东财接口一抖动、重试也失败，异常会一路冒穿到Streamlit
    渲染层，整个详情页直接崩掉报错，而不是页面本来设计好的"暂无财务数据"降级提示。
    这里改成自己吞掉异常返回空DataFrame，跟其它"取数失败就给空表"的getter（比如
    get_stock_intraday_a）保持同一个"不会raise、只会返回空"的调用约定，调用方原有的
    `if fin is not None and not fin.empty` 判断不用改就能正确降级。
    """
    try:
        if market == "HK":
            df = _with_retry(lambda: ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="年度"))
        elif market == "US":
            df = _with_retry(lambda: ak.stock_financial_us_analysis_indicator_em(symbol=symbol, indicator="年报"))
        else:
            df = _with_retry(lambda: ak.stock_financial_abstract(symbol=symbol))
    except Exception:
        return pd.DataFrame()
    # 东财原始数据里有些指标（比如没有过并购的公司的"商誉"）某些期数就是
    # None——这是真实的"这项不适用/未披露"，不是取数失败，但None被
    # st.dataframe/.to_string()原样显示成字面的"None"三个字母，用户会
    # 以为是bug。统一换成"—"（这个项目里"没有数据"的通用占位符，别处
    # 一直这么用），显示给用户和喂给AI的文本口径保持一致，不用改两处。
    return df.fillna("—")


@st.cache_data(ttl=600, show_spinner=False)
def get_valuation_percentile(symbol: str, market: str, period: str = "近三年") -> dict:
    """PE(TTM)/PB 的历史分位——不是给一个孤立的静态倍数（那样没法回答"这个估值
    算贵还是便宜"），而是用百度股市通的历史序列本地算"现价估值在过去三年自己
    的区间里处于什么分位"，跟_price_position_text算52周价格分位是同一个思路，
    只是换成估值维度。

    只支持A股/港股（ak.stock_zh_valuation_baidu / stock_hk_valuation_baidu）。
    美股同名接口(stock_us_valuation_baidu)2026-08-25实测对AAPL/BILI等任意
    代码都返回JSONDecodeError（接口本身挂了或被墙，不是参数问题），不强行
    降级伪造分位数，直接返回空字典——调用方(advisor.py _valuation_text)对
    美股改用Futu快照里的静态PE/PB兜底，没有历史分位就如实说明，不编。
    """
    if market not in ("A", "HK"):
        return {}
    fn = ak.stock_zh_valuation_baidu if market == "A" else ak.stock_hk_valuation_baidu
    result: dict = {}
    for label, indicator in (("pe_ttm", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            df = _with_retry(lambda: fn(symbol=symbol, indicator=indicator, period=period))
        except Exception:
            continue
        if df is None or df.empty or "value" not in df.columns:
            continue
        vals = df["value"].dropna()
        if vals.empty:
            continue
        cur = float(vals.iloc[-1])
        pct = float((vals < cur).mean() * 100)
        result[label] = {"current": cur, "percentile": pct, "years": round(len(vals) / 250, 1)}
    return result


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_news(keyword: str, limit: int = 10) -> pd.DataFrame:
    """个股相关新闻——只返回真正提到这家公司的条目，不拿不相关的大盘资讯充数。

    原本调东财的关键词搜索接口，实测发现它已经被反爬拦截了——不管传什么关键词，
    返回的都是同一份缓存假数据（连 JSONP 回调标识都一模一样）。这类接口层面的
    伪装拦截没法靠改参数绕过，所以换成财新的大盘资讯源（get_market_news），
    本地按公司名关键词过滤出相关条目。以前这里查不到就退化展示大盘资讯凑数，
    容易让用户误以为是这只股票的新闻——现在改成查不到就如实说没有，大盘资讯
    单独用 get_market_news() 给以后的首页板块用，不在个股页面里混着展示。

    这个源本身不带发布时间字段，但 url 里嵌着日期（.../database.caixin.com/
    2026-07-17/...），从这提取出日期用来排序——不然拿到的条目顺序不保证新旧，
    可能把三五天前的旧闻排在最前面，用户根本看不出这是不是"最新"资讯。
    """
    df = get_market_news()
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["日期"] = df["url"].str.extract(r"/(\d{4}-\d{2}-\d{2})/")
    df = df.sort_values("日期", ascending=False, na_position="last")

    matched = df[df["summary"].str.contains(keyword, na=False)]
    result = matched.head(limit).copy()
    if result.empty:
        return pd.DataFrame()
    # 这个数据源只有一段摘要文字，没有"标题"和"正文"的区分，不再截断硬造一个假标题——
    # 摘要全文当标题用。
    result = result.rename(columns={"summary": "新闻标题", "tag": "分类"})
    return result[["日期", "新闻标题", "分类", "url"]]


_GLOBAL_INDEX_YAHOO_SYMBOLS = {
    "日经225": "%5EN225", "富时100": "%5EFTSE", "德国DAX": "%5EGDAXI",
    "印度SENSEX": "%5EBSESN",
    # 首页地图之前太空，补的3个填空区域用的指数
    "巴西IBOVESPA": "%5EBVSP", "澳大利亚ASX200": "%5EAXJO", "新加坡STI": "%5ESTI",
}


def _yahoo_index_snapshot(symbol: str) -> dict | None:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            timeout=8,
        )
        meta = r.json()["chart"]["result"][0]["meta"]
        last = float(meta["regularMarketPrice"])
        prev = float(meta.get("previousClose") or meta.get("chartPreviousClose"))
    except Exception:
        return None
    if not prev:
        return None
    change = last - prev
    return {"最新": last, "涨跌": change, "涨跌幅": change / prev * 100}


@st.cache_data(ttl=60, show_spinner=False)
def get_global_indices() -> dict[str, dict]:
    """首页世界地图用的几个国际指数（日经225/富时100/德国DAX/印度SENSEX/
    巴西IBOVESPA/澳大利亚ASX200/新加坡STI，韩国KOSPI用户反馈拿掉了）——
    查过东财"全球指数"接口(index_global_spot_em)，实测这次开发时连续多次
    失败（JSONDecodeError，大概率被限流跳过去了一个不是
    JSON的响应体，跟涨跌停股池同一个push2.eastmoney.com域名下的问题）；
    也试过Twelve Data的免费API key，这5个国际指数在免费层直接返回"index
    unavailable"（付费套餐才能用）。改用Yahoo Finance的公开chart接口
    （不需要key/信用卡，只要带一个像样的User-Agent），实测这5个指数
    数据都能查到、数值跟东财那次能查到时的结果一致，目前看比东财这条
    线稳定得多。

    这个接口不支持一次查多个symbol（只能一个个查），5个指数用线程池并发
    查，避免顺序查5次网络请求的延迟叠加。单个查询失败不影响其它——返回
    {指数名: {...}}，查不到的指数不出现在dict里，调用方按key存在与否
    判断，不拿假数据凑数。
    """
    with ThreadPoolExecutor(max_workers=len(_GLOBAL_INDEX_YAHOO_SYMBOLS)) as ex:
        results = list(ex.map(
            lambda kv: (kv[0], _yahoo_index_snapshot(kv[1])),
            _GLOBAL_INDEX_YAHOO_SYMBOLS.items(),
        ))
    return {name: snap for name, snap in results if snap is not None}


@st.cache_data(ttl=300, show_spinner=False)
def get_market_news() -> pd.DataFrame:
    """大盘/宏观资讯，补充个股新闻覆盖不到的面。"""
    return _with_retry(ak.stock_news_main_cx, throttle=False)  # 财新，不是东财


def get_index_news(name: str, limit: int = 10) -> tuple:
    """指数版的资讯，优先走 get_futu_news（真按"这个指数"关键词搜，港股/美股/A股
    指数都覆盖得到，恒生科技指数不会被喂一堆跟它毫无关系的全球宏观新闻）。

    Futu 连不上（本地没装 OpenD）时才退回财新兜底——但财新那份大盘资讯偏
    全球宏观/A股，对港股/美股指数来说本来就文不对题，所以退回财新时只做
    严格关键词匹配，匹配不到就如实说没有，不再拿不相关的资讯硬凑（这曾经
    导致恒生科技指数页面显示一堆油价、谷歌股价这类完全不相关的内容）。
    返回 (DataFrame, 来源标记："futu"/"caixin")。
    """
    futu_news = get_futu_news(name, max_count=limit)
    if futu_news is not None and not futu_news.empty:
        return futu_news, "futu"

    df = get_market_news()
    if df is None or df.empty:
        return pd.DataFrame(), "caixin"

    df = df.copy()
    df["日期"] = df["url"].str.extract(r"/(\d{4}-\d{2}-\d{2})/")
    df = df.sort_values("日期", ascending=False, na_position="last")

    matched = df[df["summary"].str.contains(name, na=False)]
    result = matched.head(limit).copy()
    if result.empty:
        return pd.DataFrame(), "caixin"
    result = result.rename(columns={"summary": "新闻标题", "tag": "分类"})
    return result[["日期", "新闻标题", "分类", "url"]], "caixin"


@st.cache_data(ttl=600, show_spinner=False)
def get_stock_notices(symbol: str) -> pd.DataFrame:
    """A股官方公告——监管强制披露，来自东财公告中心，永远免费，不存在付费墙这回事，
    比新闻评论类内容更"一手"（财报、分红、股东会决议这些直接是公司自己发的）。
    只支持A股，港股/美股没有对应的免费公告聚合源，那两个市场还是走 get_stock_news。
    """
    df = _with_retry(lambda: ak.stock_individual_notice_report(security=symbol, symbol="全部"))
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"公告标题": "新闻标题", "公告日期": "日期", "公告类型": "分类", "网址": "url"})
    df = df.sort_values("日期", ascending=False)
    return df[["日期", "新闻标题", "分类", "url"]].head(10)
