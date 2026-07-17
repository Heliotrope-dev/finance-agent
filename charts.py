"""统计/可视化层 —— K线图 + 均线 + 成交量，外加真正算出来的统计指标。

这里的数字都是本地用 pandas 直接算的，不经过 AI，
跟 analysis.py 里 AI 的文字判断是两条独立的证据链。
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def _compute_macd(close: pd.Series) -> pd.DataFrame:
    """标准MACD：EMA12/EMA26算DIF，DIF的9日EMA是DEA，柱状图=2*(DIF-DEA)。"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    hist_bar = 2 * (dif - dea)
    return pd.DataFrame({"DIF": dif, "DEA": dea, "MACD": hist_bar})


def _session_minutes(market: str) -> list[str]:
    """一个交易日按分钟展开的时间框架，用来给分时图垫底——同花顺分时图的横轴
    从开盘一直画到收盘，不是画到"你几点点进来看"；港股还要跳过午间休市那一段，
    不能在图上空出一大截白板。"""
    if market == "A":
        morning = pd.date_range("09:30", "11:30", freq="1min").strftime("%H:%M").tolist()
        afternoon = pd.date_range("13:00", "15:00", freq="1min").strftime("%H:%M").tolist()
        return morning + afternoon
    if market == "HK":
        morning = pd.date_range("09:30", "12:00", freq="1min").strftime("%H:%M").tolist()
        afternoon = pd.date_range("13:00", "16:00", freq="1min").strftime("%H:%M").tolist()
        return morning + afternoon
    return pd.date_range("09:30", "16:00", freq="1min").strftime("%H:%M").tolist()


def build_intraday_line(intraday: pd.DataFrame, prev_close: float | None = None, market: str = "HK") -> go.Figure:
    """真正的分时走势图——价格折线 + 均价线 + 成交量，跟K线柱状图是两种图。

    intraday 需要有 时间/价格/成交量 列。参照同花顺分时图的习惯：
    - 横轴铺满整个交易时段（含港股午休跳过），不是只画到当前实际拿到数据的那一分钟
    - Y轴按价格实际波动范围缩放，不是从0起——不然涨跌趋势在图上会被压成一条直线
    - 加一条均价线（成交量加权累计均价，同花顺"均价"字段的算法）
    - 价格线/填充色跟着涨跌变红绿（相对昨收），不是死一种颜色
    - 成交量柱子逐笔按涨跌上色（这一笔比上一笔涨→红，跌→绿），不是统一灰色
    """
    df = intraday.copy()
    df["hm"] = df["时间"].dt.strftime("%H:%M")
    df = df.drop_duplicates(subset="hm", keep="last")

    last_price = float(df["价格"].iloc[-1])
    base = prev_close if prev_close else float(df["价格"].iloc[0])
    up = last_price >= base
    line_color = "#e02020" if up else "#22a06b"
    fill_color = "rgba(224,32,32,0.08)" if up else "rgba(34,160,107,0.08)"

    # 指数没有真实成交量（指数本身不是被直接交易的标的，Futu的分时接口对指数
    # 返回的成交量是0），这种情况下按成交量加权算均价没有意义——分母全是0，
    # 之前的写法会 fillna 成价格本身，均价线跟价格线完全重合，橙线糊住红绿线。
    # 有真实成交量就用成交量加权均价，没有就退化成普通累计均价。
    has_volume = df["成交量"].sum() > 0
    if has_volume:
        cum_amount = (df["价格"] * df["成交量"]).cumsum()
        cum_volume = df["成交量"].cumsum().replace(0, pd.NA)
        df["均价"] = (cum_amount / cum_volume).ffill().fillna(df["价格"])
    else:
        df["均价"] = df["价格"].expanding().mean()

    prev_tick = df["价格"].shift(1).fillna(base)
    df["量色"] = ["#e02020" if p >= pt else "#22a06b" for p, pt in zip(df["价格"], prev_tick)]

    # 铺满整个交易时段的时间框架，实际数据按 hm（HH:MM）左连接上去——还没走到的
    # 分钟自然是空值，图上就是留白，而不是把横轴压缩到"现在"就截断。
    session = pd.DataFrame({"hm": _session_minutes(market)})
    merged = session.merge(df[["hm", "价格", "均价", "成交量", "量色"]], on="hm", how="left")

    # 指数没有真实成交量，成交量面板画出来就是一片空白——不如干脆不画这个面板，
    # 图表只留价格这一部分，比留一个空面板更诚实、也更好看。
    if has_volume:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            row_heights=[0.7, 0.3], vertical_spacing=0.08,
            subplot_titles=("", "成交量"),
        )
    else:
        fig = make_subplots(rows=1, cols=1)

    fig.add_trace(
        go.Scatter(
            x=merged["hm"], y=merged["价格"], mode="lines", connectgaps=False,
            line=dict(width=1.5, color=line_color), name="价格",
            fill="tozeroy", fillcolor=fill_color,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=merged["hm"], y=merged["均价"], mode="lines", connectgaps=False,
            line=dict(width=1, color="#f59e0b"), name="均价",
        ),
        row=1, col=1,
    )
    if prev_close:
        fig.add_hline(y=prev_close, line=dict(width=1, color="#999", dash="dash"), row=1, col=1)

    price_min = min(df["价格"].min(), prev_close or df["价格"].min())
    price_max = max(df["价格"].max(), prev_close or df["价格"].max())
    pad = max((price_max - price_min) * 0.15, price_max * 0.005)
    fig.update_yaxes(range=[price_min - pad, price_max + pad], side="right", row=1, col=1)
    fig.update_xaxes(type="category", nticks=8, row=1, col=1)

    if has_volume:
        # 成交量按同花顺习惯换算成"万"为单位显示，柱子细一点、轴放右边，
        # hover 直接显示"量: X万"，不是原始股数那种一长串数字。
        vol_wan = merged["成交量"] / 10000
        fig.add_trace(
            go.Bar(
                x=merged["hm"], y=vol_wan, marker_color=merged["量色"], name="成交量",
                hovertemplate="%{x}<br>量: %{y:.2f}万<extra></extra>",
            ),
            row=2, col=1,
        )
        fig.update_yaxes(side="right", ticksuffix="万", row=2, col=1)
        fig.update_xaxes(type="category", nticks=8, row=2, col=1)

    fig.update_layout(
        height=480 if has_volume else 340,
        margin=dict(l=10, r=10, t=20, b=10),
        showlegend=False,
        bargap=0.15,
    )
    return fig


def build_candlestick(hist: pd.DataFrame) -> go.Figure:
    """K线图 + MA5/MA20 + 成交量 + MACD，三个子图。hist 需要有 日期/开盘/收盘/最高/最低/成交量 列。"""
    df = hist.copy()
    df["MA5"] = df["收盘"].rolling(5).mean()
    df["MA20"] = df["收盘"].rolling(20).mean()
    macd = _compute_macd(df["收盘"].astype(float))

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.5, 0.2, 0.3], vertical_spacing=0.09,
        subplot_titles=("", "成交量", "MACD"),
    )

    fig.add_trace(
        go.Candlestick(
            x=df["日期"],
            open=df["开盘"],
            high=df["最高"],
            low=df["最低"],
            close=df["收盘"],
            increasing_line_color="#ef4444",
            decreasing_line_color="#22c55e",
            name="K线",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=df["MA5"], line=dict(width=1, color="#f59e0b"), name="MA5"),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=df["MA20"], line=dict(width=1, color="#3b82f6"), name="MA20"),
        row=1,
        col=1,
    )

    vol_colors = [
        "#ef4444" if c >= o else "#22c55e" for o, c in zip(df["开盘"], df["收盘"])
    ]
    fig.add_trace(
        go.Bar(x=df["日期"], y=df["成交量"], marker_color=vol_colors, name="成交量"),
        row=2,
        col=1,
    )

    macd_colors = ["#ef4444" if v >= 0 else "#22c55e" for v in macd["MACD"]]
    fig.add_trace(
        go.Bar(x=df["日期"], y=macd["MACD"], marker_color=macd_colors, name="MACD柱"),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=macd["DIF"], line=dict(width=1, color="#f59e0b"), name="DIF"),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=macd["DEA"], line=dict(width=1, color="#3b82f6"), name="DEA"),
        row=3,
        col=1,
    )

    fig.update_layout(
        height=820,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(title_standoff=8)
    return fig


def compute_technical_signal(hist: pd.DataFrame) -> str:
    """本地算好的技术面信号摘要，喂给AI做交叉验证用——逼AI去对照这几条硬信号，
    而不是自由发挥写一段"技术面尚可"这种空话。均线/MACD都是本地pandas算的，
    跟AI的判断是两条独立证据链，AI只是被要求去核对这几条信号跟消息面是否一致。
    """
    close = hist["收盘"].astype(float)
    if len(close) < 20:
        return "数据不足20天，均线信号暂不可靠。"
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    macd = _compute_macd(close)

    ma_cross = "无明显交叉"
    if len(ma5) >= 2:
        prev_diff = ma5.iloc[-2] - ma20.iloc[-2]
        curr_diff = ma5.iloc[-1] - ma20.iloc[-1]
        if prev_diff <= 0 < curr_diff:
            ma_cross = "MA5上穿MA20（金叉，短期转强信号）"
        elif prev_diff >= 0 > curr_diff:
            ma_cross = "MA5下穿MA20（死叉，短期转弱信号）"
        elif curr_diff > 0:
            ma_cross = "MA5位于MA20上方（多头排列）"
        else:
            ma_cross = "MA5位于MA20下方（空头排列）"

    macd_bar = macd["MACD"].iloc[-1]
    macd_state = f"MACD柱{'为正' if macd_bar >= 0 else '为负'}（{'多头动能' if macd_bar >= 0 else '空头动能'}）"

    price_vs_ma20 = "高于" if close.iloc[-1] >= ma20.iloc[-1] else "低于"

    return (
        f"{ma_cross}；{macd_state}；当前价格{price_vs_ma20}MA20"
        f"（现价{close.iloc[-1]:.2f}，MA20为{ma20.iloc[-1]:.2f}）。"
    )


def compute_realtime_signal(spot: dict, intraday: pd.DataFrame | None = None) -> str:
    """技术面信号（compute_technical_signal）只看日线收盘价，AI写出来的分析
    就只能是"最近几天怎么样"这种偏宏观的话，看不出"今天这一刻"的走势。这里
    专门算一段基于实时快照+分时数据的"盘中信号"：现价相对今日开盘/最高/最低
    的位置、距离今日高低点的百分比，有分时数据的话再加一段最近这一段时间
    （分时序列后半段 vs 前半段均价）的短期动量方向。全是本地算好的数字，
    不是AI编的，喂给AI能让它把总结落到"今天具体怎么走的"，不是只谈天数级别
    的宏观趋势。
    """
    if not spot or not spot.get("最新价"):
        return "暂无实时快照数据。"

    last = spot["最新价"]
    open_p = spot.get("今开")
    high = spot.get("最高")
    low = spot.get("最低")
    prev_close = spot.get("昨收")

    parts = []
    if prev_close:
        chg_pct = (last - prev_close) / prev_close * 100
        parts.append(f"现价{last:.2f}，较昨收{'上涨' if chg_pct >= 0 else '下跌'}{abs(chg_pct):.2f}%")
    if open_p:
        from_open = (last - open_p) / open_p * 100
        parts.append(f"较今日开盘{'涨' if from_open >= 0 else '跌'}{abs(from_open):.2f}%")
    if high and low and high > low:
        pos_in_range = (last - low) / (high - low) * 100
        parts.append(f"处于今日振幅区间的{pos_in_range:.0f}%位置（今高{high:.2f}/今低{low:.2f}）")
        if high - last < (high - low) * 0.05:
            parts.append("非常接近今日最高点")
        elif last - low < (high - low) * 0.05:
            parts.append("非常接近今日最低点")

    if intraday is not None and not intraday.empty and "价格" in intraday.columns and len(intraday) >= 10:
        prices = intraday["价格"].astype(float)
        mid = len(prices) // 2
        first_half_avg = prices.iloc[:mid].mean()
        second_half_avg = prices.iloc[mid:].mean()
        if second_half_avg > first_half_avg * 1.001:
            parts.append("盘中后半段均价高于前半段，短期呈上行动量")
        elif second_half_avg < first_half_avg * 0.999:
            parts.append("盘中后半段均价低于前半段，短期呈下行动量")
        else:
            parts.append("盘中价格基本走平，没有明显方向")

    return "；".join(parts) + "。" if parts else "实时数据字段不全，暂不能给出盘中信号。"


def compute_stats(hist: pd.DataFrame) -> dict:
    """真正的统计计算：区间收益率、年化波动率、最大回撤、夏普比率(简化版)。"""
    close = hist["收盘"].astype(float)
    daily_ret = close.pct_change().dropna()

    period_return = (close.iloc[-1] / close.iloc[0] - 1) * 100
    annualized_vol = daily_ret.std() * (252 ** 0.5) * 100

    cummax = close.cummax()
    drawdown = (close - cummax) / cummax
    max_drawdown = drawdown.min() * 100

    mean_daily = daily_ret.mean()
    std_daily = daily_ret.std()
    sharpe_like = (mean_daily / std_daily) * (252 ** 0.5) if std_daily else 0.0

    return {
        "区间收益率": f"{period_return:+.2f}%",
        "年化波动率": f"{annualized_vol:.2f}%",
        "最大回撤": f"{max_drawdown:.2f}%",
        "夏普比率(简化)": f"{sharpe_like:.2f}",
        "样本天数": f"{len(close)}天",
    }


def build_return_histogram(hist: pd.DataFrame) -> go.Figure:
    """每日涨跌幅分布直方图 —— 比单一波动率数字更直观：是经常小波动还是偶尔巨震。"""
    daily_ret = hist["收盘"].astype(float).pct_change().dropna() * 100

    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=daily_ret,
            nbinsx=25,
            marker_color="#3b82f6",
            marker_line=dict(color="#1e293b", width=0.5),
        )
    )
    fig.add_vline(x=0, line_dash="dash", line_color="#94a3b8", line_width=1)
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="单日涨跌幅 (%)",
        yaxis_title="出现天数",
        bargap=0.05,
    )
    return fig


def build_benchmark_comparison(hist: pd.DataFrame, benchmark: pd.DataFrame, benchmark_name: str = "沪深300") -> go.Figure:
    """把个股和基准指数都从 100 起点开始画，直接对比谁涨得多、谁跑赢了。"""
    stock = hist[["日期", "收盘"]].copy()
    stock["归一化"] = stock["收盘"] / stock["收盘"].iloc[0] * 100

    bm = benchmark.copy()
    bm["归一化"] = bm["收盘"] / bm["收盘"].iloc[0] * 100

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=stock["日期"], y=stock["归一化"], name="个股", line=dict(color="#ef4444", width=2))
    )
    fig.add_trace(
        go.Scatter(
            x=bm["日期"], y=bm["归一化"], name=benchmark_name, line=dict(color="#94a3b8", width=2, dash="dot")
        )
    )
    fig.update_layout(
        height=320,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="走势（起点=100）",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig


_MULTI_COLORS = ["#ef4444", "#3b82f6", "#22c55e", "#f59e0b", "#a855f7"]


def build_multi_comparison(hist_by_name: dict) -> go.Figure:
    """多只股票（或指数）放一起对比，起点都归一化到100，跟 build_benchmark_comparison 是一回事，
    只是不限制成两方对比，任意几只都能放一起画。hist_by_name: {显示名: 行情DataFrame}。"""
    fig = go.Figure()
    for i, (name, df) in enumerate(hist_by_name.items()):
        s = df[["日期", "收盘"]].copy()
        s["归一化"] = s["收盘"] / s["收盘"].iloc[0] * 100
        fig.add_trace(
            go.Scatter(
                x=s["日期"], y=s["归一化"], name=name,
                line=dict(color=_MULTI_COLORS[i % len(_MULTI_COLORS)], width=2),
            )
        )
    fig.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=10, b=10),
        yaxis_title="走势（起点=100）",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig
