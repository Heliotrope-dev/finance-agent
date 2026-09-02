"""统计/可视化层 —— K线图 + 均线 + 成交量，外加真正算出来的统计指标。

这里的数字都是本地用 pandas 直接算的，不经过 AI，
跟 analysis.py 里 AI 的文字判断是两条独立的证据链。
"""

from datetime import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from theme import UP_COLOR, DOWN_COLOR, NEUTRAL_COLOR


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
    line_color = UP_COLOR if up else DOWN_COLOR
    # rgba版本的填充色没法直接复用UP_COLOR/DOWN_COLOR这两个十六进制字符串
    # （CSS的rgba()要十进制分量），这两组数字是UP_COLOR(#e02020)/DOWN_COLOR
    # (#22a06b)按十六进制拆开换算过来的，跟上面line_color是同一个颜色，
    # 只是多了0.08的透明度做填充，不是另外瞎起的一个颜色。
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

    # connectgaps=True——分时数据偶尔会有个别分钟丢tick（数据源那边的稀疏点，
    # 不是真的停牌），之前connectgaps=False会让这种缺口在图上断开一截，
    # 用户反馈"看着像断了"。改成True后线会自动跨过缺口连起来，今天还没走到
    # 的未来时段本来就没有相邻数据点可连，不受影响，不会画出假的未来走势。
    fig.add_trace(
        go.Scatter(
            x=merged["hm"], y=merged["价格"], mode="lines", connectgaps=True,
            line=dict(width=1.5, color=line_color), name="价格",
            fill="tozeroy", fillcolor=fill_color,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=merged["hm"], y=merged["均价"], mode="lines", connectgaps=True,
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
            increasing_line_color=UP_COLOR,
            decreasing_line_color=DOWN_COLOR,
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
        UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(df["开盘"], df["收盘"])
    ]
    fig.add_trace(
        go.Bar(x=df["日期"], y=df["成交量"], marker_color=vol_colors, name="成交量"),
        row=2,
        col=1,
    )

    macd_colors = [UP_COLOR if v >= 0 else DOWN_COLOR for v in macd["MACD"]]
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
    if len(close) < 3:
        # 样本不足3天时算不出可靠的日收益率标准差：close只有1行时daily_ret是
        # 空Series，只有2行时daily_ret也只有1个元素——pandas对单元素Series的
        # .std()同样返回NaN（样本标准差数学上需要至少2个观测值）。NaN在
        # Python里是真值，下面"if std_daily else 0.0"这层保护形同虚设，
        # 会算出显示给用户的字面"nan%"。样本太少时直接不给这组统计量，
        # 比硬凑出一个NaN更诚实。（用真实数据测过：2行输入不加这层判断
        # 依然会漏出nan，只挡len<2不够。）
        return {}
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
        # 之前这里写的是"#ef4444"——本文件开头theme.py的说明里提到的"旧charts.py
        # 红"，跟app.py/candlestick统一用的品牌红(UP_COLOR="#e02020")肉眼能看出
        # 不是同一个红，这里补上遗漏的一处。
        go.Scatter(x=stock["日期"], y=stock["归一化"], name="个股", line=dict(color=UP_COLOR, width=2))
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


# 第一个和第三个位置分别用品牌红/绿（跟UP_COLOR/DOWN_COLOR保持同一个色号），
# 后面几个是纯粹的分类色（蓝/橙/紫/青），不代表涨跌方向，只用来区分不同标的。
# 凑够6个——持仓对比入口（app.py _show_compare_dialog）最多允许勾选6只，
# 凑够6个颜色能保证6只同时对比时每条线颜色都不重复。
_MULTI_COLORS = [UP_COLOR, "#3b82f6", DOWN_COLOR, "#f59e0b", "#a855f7", "#0891b2"]


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


_DONUT_MAX_SLICES = 8


def build_position_donut(holdings: list[dict], total_value_cny: float) -> go.Figure:
    """持仓占比环形图。holdings: [{"label": "名称（代码）", "value_cny": 折人民币市值}, ...]，
    调用方（app.py）负责按金额降序排好、汇率折算好——这里不做排序也不做汇率转换，
    避免图表模块反过来依赖data_sources。配色直接复用_MULTI_COLORS（用户明确要求
    按手绘草图原样用红绿等颜色，不用另外发明"避开红绿"的调色板，即使跟涨跌红绿
    语义有重叠也接受）。超过_DONUT_MAX_SLICES个持仓，尾部合并成"其他"用中性灰，
    避免小额持仓挤占过多颜色/图例空间。总资产放hole中间的annotation里，
    会跟着图一起响应式缩放，不写在图外的HTML里。"""
    if len(holdings) > _DONUT_MAX_SLICES:
        head = holdings[: _DONUT_MAX_SLICES - 1]
        tail_value = sum(h["value_cny"] for h in holdings[_DONUT_MAX_SLICES - 1 :])
        rows = head + [{"label": "其他", "value_cny": tail_value}]
    else:
        rows = holdings

    labels = [r["label"] for r in rows]
    values = [r["value_cny"] for r in rows]
    colors = [
        NEUTRAL_COLOR if r["label"] == "其他" else _MULTI_COLORS[i % len(_MULTI_COLORS)]
        for i, r in enumerate(rows)
    ]

    fig = go.Figure(
        go.Pie(
            labels=labels, values=values, hole=0.62,
            marker=dict(colors=colors, line=dict(color="#fff", width=2)),
            textinfo="percent", textposition="inside",
            hovertemplate="%{label}<br>¥%{value:,.0f}（%{percent}）<extra></extra>",
            sort=False,
        )
    )
    fig.add_annotation(
        text=f"总资产<br><b style='font-size:1.3em'>¥{total_value_cny:,.0f}</b>",
        showarrow=False, font=dict(size=13), align="center",
    )
    fig.update_layout(
        height=300,
        margin=dict(l=10, r=10, t=10, b=10),
        showlegend=False,
    )
    return fig


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def build_sim_equity_curve(points: list[dict], baseline: float = 100_000.0, granularity: str = "day") -> go.Figure:
    """AI模拟盘收益曲线——points是[{"run_at": 北京时区datetime, "assets_hkd": 浮点数}]，
    按sim_agent每次运行(开盘时段每15分钟一次)时的资产快照点连线，不插值编造中间点，
    数据本身就是按这个节奏产生的，早期稀疏是正常状态。

    颜色跟着这个项目"红涨绿跌"的既定约定动态选——不能像K线图那样固定用UP_COLOR
    画线，净值曲线本身有涨有跌，固定红色在净值下跌时会跟"红=涨"这个全局约定
    自相矛盾、误导用户。baseline画一条起始本金的虚线参考——用户一眼就能
    看出"现在比起点高还是低"，不用自己心算差值。2026-09-02起baseline/points
    传进来的都已经是美元口径的数字（调用方app.py._render_ai_sim_dashboard
    统一折算过），这个函数本身不做任何币种换算，只是照单画图、标签固定用
    "$"前缀。

    granularity（"day"/"week"/"month"）控制X轴刻度密度——"天"视图数据点是
    5分钟一个，刻度按5分钟画正合适；"周"/"月"视图跨度拉长后再按5分钟画
    刻度会挤成一团看不清，改成按天为单位、只显示月-日。
    """
    df = pd.DataFrame(points).sort_values("run_at").reset_index(drop=True)
    is_up = df["assets_hkd"].iloc[-1] >= df["assets_hkd"].iloc[0]
    line_color = UP_COLOR if is_up else DOWN_COLOR

    fig = go.Figure()

    fig.add_hline(
        y=baseline, line=dict(color=NEUTRAL_COLOR, width=1, dash="dot"),
        annotation_text=f"起始 ${baseline:,.0f}", annotation_position="top left",
        annotation_font=dict(size=10, color=NEUTRAL_COLOR),
    )

    # 用户明确要求"港美股都不能交易的时间段不用显示收益，因为一直不变一直是
    # 直线"——快照本身只在开盘时段记录(sim_snapshot.py没开盘直接跳过不落库)，
    # 但相邻两个交易时段之间（比如港股16:00收盘到美股21:30开盘这几个小时）
    # 中间没有数据点，画连续折线时会拿收盘前最后一个点直接连到开盘后第一个
    # 点，形成一条横跨休市时间的斜线——视觉上容易被误读成"这段时间也有真实
    # 波动"。这里按采样节奏(5分钟)判断相邻点间隔，超过正常节奏一大截(20分钟，
    # 留够抖动余量)就判定为跨休市的空档，插入一个NaN把线断开，两段各自成
    # 独立线段，不画连接休市时段的假线。
    _GAP_THRESHOLD = pd.Timedelta(minutes=20)
    rows = [df.iloc[0].to_dict()]
    gap_bounds: list[tuple] = []
    for i in range(1, len(df)):
        prev_t, cur_t = df.iloc[i - 1]["run_at"], df.iloc[i]["run_at"]
        if cur_t - prev_t > _GAP_THRESHOLD:
            rows.append({"run_at": prev_t + (cur_t - prev_t) / 2, "assets_hkd": float("nan")})
            gap_bounds.append((prev_t, cur_t))
        rows.append(df.iloc[i].to_dict())
    df_plot = pd.DataFrame(rows)

    fig.add_trace(
        go.Scatter(
            x=df_plot["run_at"], y=df_plot["assets_hkd"], mode="lines+markers", connectgaps=False,
            line=dict(color=line_color, width=2.5, shape="spline", smoothing=0.35),
            marker=dict(size=5, color=line_color, line=dict(color="#fff", width=1)),
            fill="tozeroy", fillcolor=_hex_to_rgba(line_color, 0.08),
            hovertemplate="%{x|%m-%d %H:%M}<br>$%{y:,.0f}<extra></extra>",
        )
    )

    # 用户明确要求"港美股交易开盘收盘时间用红色标注其他一律黑色"——在图上
    # 用红色虚线标出这段时间范围内实际出现过的港股/美股开盘、收盘时刻，
    # 其余轴刻度/文字保持默认黑色不动。用zoneinfo按当地时区真实换算，不
    # 硬编码固定偏移——美股有夏令时，硬编码会有小半年是错的。"月"视图跨度
    # 太长，几十条线会糊成一片，不画；"周"视图只画线不加文字注释，避免
    # 好几天的标签叠在一起看不清；"天"视图数据点本来就密集，线+文字都加上。
    #
    # 真实故障纠偏（2026-09-01）：早期版本不管边界落不落在实际数据范围内，
    # 见到"当天"就把港股/美股各自的开盘收盘都画上去——结果比如重置后只有
    # 22点多的几个数据点，但"当天"港股09:30开盘的边界也被画了出来，
    # Plotly的X轴autorange会自动撑大到覆盖所有vline的位置，变成"09:30到
    # 次日04:00"接近19小时的轴，真实数据只占最右边一小段，中间一大片
    # 全是空白；叠加下面天视图固定5分钟一格的dtick，19小时/5分钟=228格，
    # 挤在图表宽度里刻度文字全部重叠糊成一片乱码。修复两处：只画落在实际
    # 数据时间范围内的边界线（不在范围内的边界对这次展示没有意义，不该
    # 反过来把轴撑大）；X轴range显式锁定在数据实际的[min,max]（留一点点
    # padding），不再任由vline把范围往外拉。
    data_min, data_max = df["run_at"].min(), df["run_at"].max()
    if granularity in ("day", "week") and not df["run_at"].empty:
        _dates = sorted({t.date() for t in df["run_at"]})
        _cn = ZoneInfo("Asia/Shanghai")
        for d in _dates:
            for tz_name, label_prefix in (("Asia/Hong_Kong", "港股"), ("America/New_York", "美股")):
                for t, suffix in ((dtime(9, 30), "开盘"), (dtime(16, 0), "收盘")):
                    boundary = datetime.combine(d, t, tzinfo=ZoneInfo(tz_name)).astimezone(_cn)
                    if not (data_min <= boundary <= data_max):
                        continue
                    fig.add_vline(
                        x=boundary, line=dict(color=UP_COLOR, width=1, dash="dash"),
                        opacity=0.5,
                        annotation_text=(f"{label_prefix}{suffix}" if granularity == "day" else None),
                        annotation_font=dict(size=9, color=UP_COLOR),
                        annotation_position="top",
                    )

    last = df.iloc[-1]
    fig.add_annotation(
        x=last["run_at"], y=last["assets_hkd"], text=f"${last['assets_hkd']:,.0f}",
        showarrow=True, arrowhead=0, arrowcolor=line_color, ax=0, ay=-28,
        font=dict(size=12, color=line_color, weight="bold"),
        bgcolor="rgba(255,255,255,0.92)", bordercolor=line_color, borderwidth=1, borderpad=3,
    )

    y_min = min(baseline, df["assets_hkd"].min())
    y_max = max(baseline, df["assets_hkd"].max())
    y_pad = max((y_max - y_min) * 0.25, baseline * 0.002)

    # X轴范围显式锁定在数据实际的[min,max]（右边留一点padding避免最后一个
    # 端点标注被裁掉）——不再靠autorange自己算，理由见上面vline那段注释。
    x_span = data_max - data_min
    x_pad = max(x_span * 0.04, pd.Timedelta(minutes=5))

    fig.update_layout(
        height=300,
        margin=dict(l=10, r=10, t=36, b=10),
        yaxis=dict(
            title="总资产（美元）", range=[y_min - y_pad, y_max + y_pad],
            gridcolor="rgba(0,0,0,0.06)", zeroline=False,
        ),
        xaxis=dict(
            # 用户明确要求"港股收盘到美股开盘那段空白砍掉"——休市空档不但
            # 折线不连（上面的NaN断点），连轴上对应的这段宽度也不留，跟股票
            # 软件"隐藏非交易时段"是同一个效果。用Plotly的rangebreaks，边界
            # 直接复用上面已经识别出来的休市空档(gap_bounds)，不用另外猜
            # 固定的收盘/开盘时间点——数据实际断在哪就砍哪段，跟当天真实
            # 港股/美股时间表自动对齐。
            rangebreaks=[dict(bounds=[str(a), str(b)]) for a, b in gap_bounds],
            range=[data_min - x_pad, data_max + x_pad],
            tickformat="%H:%M" if granularity == "day" else "%m-%d",
            # "天"视图固定1小时一格（用户明确要求）；折线本身还是按快照实际
            # 节奏(5分钟一个点)连，刻度间隔只影响轴上标签疏密，不影响连线
            # 精细度。"周"/"月"视图数据跨度更大，1小时会挤爆，继续用nticks
            # 让Plotly自己按当前range挑合适间隔。
            **(dict(dtick=60 * 60 * 1000) if granularity == "day" else dict(nticks=8)),
            gridcolor="rgba(0,0,0,0.04)",
        ),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
    )
    return fig
