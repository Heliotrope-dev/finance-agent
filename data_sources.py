"""数据层 —— 封装 AkShare 调用，统一加重试/限流间隔/缓存。"""

import contextlib
import io
import queue as _queue
import threading
import time
from datetime import datetime, timedelta

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

_baostock_lock = threading.Lock()  # BaoStock的login/logout是全局会话，并发调用要加锁


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


def _benchmark_history_a(start_date: str, end_date: str, index_code: str) -> pd.DataFrame:
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


@st.cache_data(ttl=300, show_spinner=False)
def get_benchmark_history(start_date: str, end_date: str, market: str = "A") -> pd.DataFrame:
    """基准指数历史收盘价：A股用沪深300，港股用恒生指数，美股用标普500。"""
    if market == "HK":
        df = ak.stock_hk_index_daily_sina(symbol="HSI")
        df = df.rename(columns={"date": "日期", "close": "收盘"})
        df["日期"] = pd.to_datetime(df["日期"])
        start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
        return df[(df["日期"] >= start) & (df["日期"] <= end)][["日期", "收盘"]]
    if market == "US":
        df = ak.index_us_stock_sina(symbol=".INX")
        df = df.rename(columns={"date": "日期", "close": "收盘"})
        df["日期"] = pd.to_datetime(df["日期"])
        start, end = pd.to_datetime(start_date), pd.to_datetime(end_date)
        return df[(df["日期"] >= start) & (df["日期"] <= end)][["日期", "收盘"]]
    return _benchmark_history_a(start_date, end_date, "sh.000300")


_MULTI_INDICES = {
    "A": [("上证指数", "sh.000001"), ("深证成指", "sz.399001"), ("创业板指", "sz.399006")],
    "HK": [("恒生指数", "HSI"), ("恒生科技", "HSTECH"), ("国企指数", "HSCEI")],
    "US": [("标普500", ".INX"), ("纳斯达克100", ".NDX"), ("道琼斯", ".DJI")],
}


def _one_index_snapshot(market: str, name: str, code: str) -> dict | None:
    """单个指数的快照，给 get_multi_index_snapshot 并发调用用（3个指数不再串行等）。"""
    try:
        if market == "A":
            sina_snap = _a_index_snapshot_sina(code)
            if sina_snap:
                return {"名称": name, **sina_snap}
            # 新浪实时快照失败时的兜底——BaoStock日线是EOD数据，交易时段内会滞后一天。
            # BaoStock 的 login/logout 是全局会话，不是线程安全的；这里3个指数是并发
            # 跑的，加锁避免真走到这条兜底路径时多个线程同时login/logout互相打架。
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
            if len(rows) < 2:
                return None
            last, prev = float(rows[-1][1]), float(rows[-2][1])
        elif market == "HK":
            futu_snap = _hk_index_snapshot_futu(name)
            if futu_snap:
                return {"名称": name, **futu_snap}
            # Futu不可用时的兜底——新浪的指数日线接口是EOD数据，交易时段内会滞后一天。
            df = ak.stock_hk_index_daily_sina(symbol=code)
            if len(df) < 2:
                return None
            last, prev = float(df.iloc[-1]["close"]), float(df.iloc[-2]["close"])
        else:
            df = ak.index_us_stock_sina(symbol=code)
            if len(df) < 2:
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


@st.cache_data(ttl=60, show_spinner=False)
def get_multi_index_snapshot(market: str) -> list[dict]:
    """给行情页顶部的指数卡片用：每个市场固定几个核心指数，各自最新值+涨跌。

    这里特意不用线程池并发拉——实测过一次，AkShare 内部某些接口用 py_mini_racer
    （V8引擎，用来跑一段JS解密响应）做首次初始化不是线程安全的，多个线程同时
    第一次触发会直接把整个 Streamlit 进程带崩（FATAL级别，不是能catch的异常）。
    串行虽然慢一点，但这是能稳定跑的版本。
    """
    indices = _MULTI_INDICES.get(market, [])
    return [r for r in (_one_index_snapshot(market, name, code) for name, code in indices) if r]


@st.cache_data(ttl=300, show_spinner=False)
def get_index_history(code: str, market: str, period: str = "日K") -> pd.DataFrame:
    """指数K线。三个市场的指数接口都只给日线，周K/月K用pandas重采样凑，
    没有分时数据（指数没有Futu那种实时分时源），分时选项退化成展示日K。
    """
    if market == "A":
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=1825)).strftime("%Y-%m-%d")
        with contextlib.redirect_stdout(io.StringIO()):
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
        raw = ak.stock_hk_index_daily_sina(symbol=code)
        df = raw.rename(columns={"date": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量"})
    else:
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
    df = _with_retry(ak.stock_market_activity_legu)
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


@st.cache_data(ttl=300, show_spinner=False)
def get_limit_pool(kind: str = "up", limit: int = 10) -> pd.DataFrame:
    """涨停股池(kind='up')/跌停股池(kind='down')，按涨跌幅排序取前 limit 条。只有A股有这个概念。"""
    date_str = datetime.now().strftime("%Y%m%d")
    fn = ak.stock_zt_pool_em if kind == "up" else ak.stock_zt_pool_dtgc_em
    df = _with_retry(lambda: fn(date=date_str))
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
}


@st.cache_data(ttl=600, show_spinner=False)
def get_hk_famous_movers(limit: int = 15) -> pd.DataFrame:
    """港股没有涨跌停制度，退化成"知名股涨跌幅榜"。

    stock_hk_famous_spot_em（东财）实测连不上，跟东财一贯的不稳定是一回事。
    改用已经验证稳定的 stock_hk_spot()（新浪全市场快照，25-30秒）拉回来后，
    本地筛出一份手动维护的知名港股清单，不依赖那个不稳定的东财接口。
    """
    df = _with_retry(ak.stock_hk_spot, retries=0)
    if df is None or df.empty or "涨跌幅" not in df.columns:
        return pd.DataFrame()
    df = df[df["代码"].isin(_HK_FAMOUS_CODES)]
    df = df.sort_values("涨跌幅", ascending=False).head(limit)
    keep = [c for c in ("代码", "中文名称", "最新价", "涨跌幅", "涨跌额") if c in df.columns]
    return df[keep].rename(columns={"中文名称": "名称"}).reset_index(drop=True)


_US_FAMOUS_CODES = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX",
    "AMD", "INTC", "AVGO", "ORCL", "CRM", "ADBE", "PYPL", "UBER",
    "DIS", "KO", "PEP", "NKE",
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
}


def search_quote_futu(keyword: str) -> list[dict]:
    """Futu 的全市场模糊搜索（get_search_quote），支持中英文/拼音，覆盖全市场股票，
    不是手动维护的名单——只在本机连得上 OpenD 时可用，连不上返回空列表。
    """
    ctx = _get_futu_ctx()
    if ctx is None:
        return []
    result = _run_with_timeout(lambda: ctx.get_search_quote(keyword, 10), timeout=6, default=None)
    if result is None:
        return []
    ret, data = result
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


@st.cache_data(ttl=300, show_spinner=False)
def get_us_famous_movers(limit: int = 15) -> pd.DataFrame:
    """美股知名股涨跌幅榜。

    stock_us_famous_spot_em（东财）今晚忽好忽坏，重试也救不回来。改用新浪，
    而且新浪这个接口支持一次请求批量查多只股票（逗号分隔），不用像单股实时
    行情那样一只只查、每次还要等全局限流的3秒间隔——一次请求20只，几乎瞬间。
    这里不走 get_stock_realtime/_with_retry 那条路，就是因为不想被那个为
    东财设计的全局限流拖慢，新浪这个接口从没在今晚测出过需要限流的迹象。
    """
    codes = ",".join(f"gb_{c.lower()}" for c in _US_FAMOUS_CODES)
    r = requests.get(
        f"https://hq.sinajs.cn/list={codes}",
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=10,
    )
    text = r.content.decode("gbk", errors="ignore")

    rows = []
    for code, line in zip(_US_FAMOUS_CODES, text.strip().split("\n")):
        if '"' not in line:
            continue
        raw = line.split('"')[1]
        fields = raw.split(",")
        if len(fields) < 27 or not fields[1]:
            continue
        rows.append({
            "代码": code, "名称": fields[0], "最新价": float(fields[1]),
            "涨跌幅": float(fields[2]), "涨跌额": float(fields[4]),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("涨跌幅", ascending=False).head(limit)
    return df.reset_index(drop=True)


def _fetch_history_hk(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """港股日线，新浪源。stock_hk_daily 不接受日期范围参数，返回全部历史，本地按日期筛。"""
    try:
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


@st.cache_data(ttl=300, show_spinner=False)
def get_stock_history(symbol: str, start_date: str, end_date: str, frequency: str = "d", market: str = "A") -> pd.DataFrame:
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
        df = _with_retry(lambda: _fetch_history_hk(symbol, start_date, end_date))
        return _append_today_bar(df, symbol, market) if frequency == "d" else df
    if market == "US":
        df = _with_retry(lambda: _fetch_history_us(symbol, start_date, end_date))
        return _append_today_bar(df, symbol, market) if frequency == "d" else df

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
                return _append_today_bar(df, symbol, market)
        except Exception:
            continue
    raise RuntimeError("三个数据源（BaoStock/东财/新浪）全部获取失败，稍后再试。")


_futu_ctx = None
_futu_ctx_checked = False


def _run_with_timeout(fn, timeout=8, default=None):
    """只用在 request_history_kline 这一条可选路径上——实测这个接口延迟不稳定，
    偶尔卡住十几秒到几分钟不返回，且不受"同步调用/清干净连接"这些条件影响。
    拿子线程跑，主线程最多等 timeout 秒就放弃走兜底；卡住的线程会泄漏，
    但这条路径只在用户主动切换K线周期时触发，频率低，好过点一下周期切换整页卡死。
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


def _get_futu_ctx():
    """本地 Futu OpenD 网关（127.0.0.1:11111）的连接句柄，只在本机跑了 OpenD 时可用。

    VPS 上没装 OpenD，这里连不上是预期情况，静默返回 None 走新浪兜底，
    不能让部署环境因为缺这个本地网关而报错。
    """
    global _futu_ctx, _futu_ctx_checked
    if _futu_ctx_checked:
        return _futu_ctx
    _futu_ctx_checked = True
    if not _FUTU_SDK_AVAILABLE:
        return None

    # 连接本身绝大多数时候是毫秒级，但实测偶尔也会异常久不返回（跟 OpenD 那边的
    # 状态有关，具体原因没能稳定复现），一样套超时兜底，不能让这一步成为唯一
    # 没有保护、能把整条链路拖死的地方。
    def _connect():
        ctx = ft.OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, _ = ctx.get_global_state()
        if ret != ft.RET_OK:
            ctx.close()
            return None
        return ctx

    try:
        _futu_ctx = _run_with_timeout(_connect, timeout=6, default=None)
    except Exception:
        _futu_ctx = None
    return _futu_ctx


def get_stock_realtime_futu(symbol: str, market: str) -> dict:
    """走本地 Futu OpenD 网关拿真实时快照，只支持港股/美股（A股无权限）。

    market 检查放在最前面——A股走这函数是必然返回空的，没必要为此白连一次 Futu。
    """
    if market not in ("HK", "US"):
        return {}
    ctx = _get_futu_ctx()
    if ctx is None:
        return {}
    code = f"HK.{symbol}" if market == "HK" else f"US.{symbol}"
    # 注意：这里故意不用 _run_with_timeout 包一层新线程去调用——实测 ctx 在一个线程里
    # 创建、又从另一个线程调用请求方法，会导致 SDK 内部状态错乱直接卡死不返回。
    # ctx 本身的连接已经在 _get_futu_ctx() 里做过超时保护，这里就同步直调。
    try:
        ret, data = ctx.get_market_snapshot([code])
    except Exception:
        return {}
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
        "更新时间": str(row["update_time"]),
        "数据源": "Futu实时",
    }


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
    ctx = _get_futu_ctx()
    if ctx is None:
        return pd.DataFrame()
    code = f"HK.{symbol}" if market == "HK" else f"US.{symbol}"
    ktype = getattr(ft.KLType, _FUTU_KTYPE_MAP[period])
    days_back = _FUTU_DAYS_BACK[period]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    # 这个接口实测延迟不稳定（偶尔卡住半天不返回，跟连接状态/线程模型都无关），
    # 套超时兜底——这条路径只在用户主动切周期时触发，卡的话最多耽误这一次点击。
    result = _run_with_timeout(
        lambda: ctx.request_history_kline(
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
    """
    now = time.time()
    if code in _futu_subscribed:
        return True
    if len(_futu_subscribed) >= _FUTU_SUB_LIMIT:
        evictable = [c for c, t in _futu_subscribed.items() if now - t > 60]
        if not evictable:
            return False
        oldest = min(evictable, key=lambda c: _futu_subscribed[c])
        _run_with_timeout(lambda: ctx.unsubscribe([oldest], [ft.SubType.RT_DATA]), timeout=5, default=None)
        _futu_subscribed.pop(oldest, None)
    result = _run_with_timeout(lambda: ctx.subscribe([code], [ft.SubType.RT_DATA]), timeout=8, default=None)
    if result is None:
        return False
    ret, _ = result
    if ret != ft.RET_OK:
        return False
    _futu_subscribed[code] = now
    return True


def _futu_last_day_intraday(ctx, code: str) -> pd.DataFrame:
    """今日没有分时数据时（周末/节假日/还没开盘）的兜底——用历史1分钟K线接口
    （不需要订阅），取最近一个交易日的分钟线当分时用。跟只剩日K比，好歹还是
    分时的形状。这条路径本来就有超时保护，卡了也就是这次兜底没拿到，不影响别的。
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    result = _run_with_timeout(
        lambda: ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_1M, autype=ft.AuType.QFQ, max_count=1000,
        ),
        timeout=8, default=None,
    )
    if result is None:
        return pd.DataFrame()
    ret, data, _ = result
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
    """真分时数据的公共取数逻辑，个股和指数都走这条路，区别只在 code 怎么拼。"""
    ctx = _get_futu_ctx()
    if ctx is None:
        return pd.DataFrame()
    if not _futu_ensure_subscribed(ctx, code):
        return pd.DataFrame()
    result = _run_with_timeout(lambda: ctx.get_rt_data(code), timeout=8, default=None)
    data = None
    if result is not None:
        ret, raw = result
        if ret == ft.RET_OK and raw is not None and not raw.empty:
            data = raw
    if data is None:
        return _futu_last_day_intraday(ctx, code)
    # is_blank=True 是午间休市那种没有真实成交的占位行（价格是拿上一个真实价格填的），
    # 保留会在图上画出一段假的平线——这些行本来就不该出现在真分时曲线里。
    if "is_blank" in data.columns:
        data = data[~data["is_blank"].astype(bool)]
    df = data.rename(columns={"time": "时间", "cur_price": "价格", "volume": "成交量"})
    df["时间"] = pd.to_datetime(df["时间"])
    df["价格"] = df["价格"].astype(float)
    df["成交量"] = df["成交量"].astype(float)
    df = df[df["价格"] > 0]
    if df.empty:
        return _futu_last_day_intraday(ctx, code)
    return df[["时间", "价格", "成交量"]]


def _sina_minute_intraday(sina_code: str) -> pd.DataFrame:
    """新浪分钟线的公共取数逻辑，个股和指数都走这条路，区别只在代码格式怎么拼。

    今天没有分时数据（周末/节假日/还没开盘）就退到接口返回的历史里最近一个
    交易日的分时——跟只剩日K比，好歹还是分时的形状，视觉上更一致。
    """
    df = ak.stock_zh_a_minute(symbol=sina_code, period="1")
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
        ctx = _get_futu_ctx()
        if ctx is None:
            return pd.DataFrame()
        result = _run_with_timeout(lambda: ctx.get_market_snapshot([code]), timeout=5, default=None)
        if result is None:
            return pd.DataFrame()
        ret, snap = result
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


def _a_index_snapshot_sina(code: str) -> dict | None:
    """A股指数的实时快照，走新浪单股快照接口，跟个股实时价（get_stock_realtime）
    是同一套字段格式——指数代码在新浪那边跟个股一样能查，不是独立的一套接口。
    code 是 BaoStock 格式（如 sh.000001），转成新浪格式（sh000001）。
    """
    sina_code = code.replace(".", "")
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={sina_code}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10,
        )
        text = r.content.decode("gbk", errors="ignore")
        raw = text.split('"')[1]
        fields = raw.split(",")
    except Exception:
        return None
    if len(fields) < 4 or not fields[3]:
        return None
    last, prev = float(fields[3]), float(fields[2])
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
    ctx = _get_futu_ctx()
    if ctx is None:
        return None
    code = f"HK.{futu_code}"
    # 同步直调，不套线程超时——理由同 get_stock_realtime_futu：ctx 跨线程调用会导致
    # SDK 内部状态错乱卡死，get_market_snapshot 本身够快，不需要额外超时保护。
    try:
        ret, data = ctx.get_market_snapshot([code])
    except Exception:
        return None
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
    ctx = _get_futu_ctx()
    if ctx is None:
        return None
    code = f"US.{etf}"
    try:
        ret, data = ctx.get_market_snapshot([code])
    except Exception:
        return None
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


@st.cache_data(ttl=30, show_spinner=False)
def get_stock_realtime(symbol: str, market: str = "A") -> dict:
    """真正的实时行情，港股/美股优先走本地 Futu OpenD 网关，没有就退回新浪。

    之前这里是从日线历史数据里取最后一行——那是"最近收盘价"，交易时段内
    跟用户自己在别的地方看到的实时价格对不上。这个接口是新浪的轻量单股查询，
    只查一只股票、不拉全市场，缓存 TTL 也缩短到 30 秒，更贴近"实时"。

    A股/港股/美股三个市场 hq.sinajs.cn 返回的字段顺序完全不一样，各写各的解析。
    """

    futu_data = get_stock_realtime_futu(symbol, market)
    if futu_data:
        return futu_data

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
            # 名称,现价,涨跌幅,时间戳,涨跌额,今开,最高,最低,...,昨收(第27个字段,index 26)
            # 之前误用 fields[8] 当昨收，实测那个字段是别的东西（不是昨收），
            # 算出来的涨跌幅离谱到几十个点。改成直接用新浪自己算好的涨跌幅/涨跌额
            # （field[2]/field[4]），昨收改用真正对得上的 field[26]（用涨跌额反推验证过）。
            if len(fields) < 27 or not fields[1]:
                return {}
            return {
                "代码": symbol, "名称": fields[0],
                "最新价": float(fields[1]), "今开": float(fields[5]),
                "昨收": float(fields[26]), "最高": float(fields[6]), "最低": float(fields[7]),
                "涨跌额": float(fields[4]), "涨跌幅": float(fields[2]),
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
def get_financial_abstract(symbol: str, market: str = "A") -> pd.DataFrame:
    """财务摘要指标。A股是东财"股票财务摘要"接口；港股/美股是东财对应的分析指标接口，
    字段跟A股完全不是一回事（更细、列更多），直接原样返回给AI消化，不强行对齐格式。"""
    if market == "HK":
        return _with_retry(lambda: ak.stock_financial_hk_analysis_indicator_em(symbol=symbol, indicator="年度"))
    if market == "US":
        return _with_retry(lambda: ak.stock_financial_us_analysis_indicator_em(symbol=symbol, indicator="年报"))
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
