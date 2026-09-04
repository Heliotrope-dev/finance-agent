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

# ── 图表统一视觉 ──────────────────────────────────────────────────────────
# 2026-09-04：这些图之前全部吃 Plotly 的默认样式——默认字体、默认网格（横竖
# 都画）、默认深色轴线、默认 hover 气泡。默认样式的问题不是丑，是"没有观点"：
# 它对所有图一视同仁，于是每张图都长得像随手生成的示意图。真正的财经图表是
# 减法做出来的：轴线去掉、竖网格去掉、刻度短线去掉、横网格淡到几乎看不见，
# 让读者的注意力只落在数据本身的形状上。这里把这套减法收成一个函数，所有图
# 统一走它，而不是每个函数各自写一份 update_layout。
_CHART_FONT = "Inter, -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"
_CHART_INK = "#17181C"
_CHART_MUTED = "#82858E"
_CHART_FAINT = "#A8ABB3"
_CHART_GRID = "rgba(23,24,28,0.055)"
# 图上的"辅助线"（均价/MA/MACD的DIF与DEA/基准线）统一走灰阶，靠深浅区分，
# 不再各挑一个彩色。理由同 _MULTI_COLORS 那段：饱和色是留给涨跌的，一条橙色
# 均线压在红绿K线上，读者要分神判断"这个橙色是什么意思"。
_AUX_STRONG = "#5B6470"
_AUX_SOFT = "#A6ADB6"


def _style_subplot_titles(fig):
    """把 make_subplots 生成的子图标题改成左对齐小灰字。

    默认是居中的深色中号字，很像模板图。标题在这里的作用是索引（"下面这块是
    成交量"），不是装饰，应该待在左上角安静地待着，不该在图正上方居中占一行。
    刻意放在 make_subplots 刚建完图、还没添加任何自定义注释的时候调用——那时
    layout.annotations 里有且只有子图标题，按位置改最稳；等到图画完再去
    annotations 里靠 xref 认哪些是子图标题，Plotly 不同版本用的是
    "paper" 还是 "x domain" 并不一致，容易全部认漏。
    """
    for ann in fig.layout.annotations:
        ann.update(
            font=dict(family=_CHART_FONT, size=11, color=_CHART_MUTED),
            x=0, xanchor="left",
        )
    return fig


def _apply_chart_theme(fig, height=None, *, legend=False, grid="y", margin=None, hovermode=None):
    """给一张图套上全站统一的图表视觉。

    grid 控制画哪个方向的网格线："y" 只画横线（绝大多数时序图该用这个——
    竖网格对"看走势"没有帮助，只是噪声），"xy" 两向都画，"none" 都不画。
    """
    fig.update_layout(
        font=dict(family=_CHART_FONT, size=11, color=_CHART_MUTED),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=margin or dict(l=6, r=16, t=18, b=6),
        # 默认的 hover 气泡是跟着线的颜色走的深色块，一条红线配一个红气泡，
        # 花且抢眼。统一成白底+发丝边框+墨色字，跟页面上的卡片是同一套语言。
        hoverlabel=dict(
            bgcolor="#FFFFFF", bordercolor="rgba(23,24,28,0.12)",
            font=dict(family=_CHART_FONT, size=11, color=_CHART_INK),
        ),
        showlegend=legend,
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, x=0, xanchor="left",
            bgcolor="rgba(0,0,0,0)", borderwidth=0,
            font=dict(family=_CHART_FONT, size=11, color=_CHART_MUTED),
        ),
    )
    if height is not None:
        fig.update_layout(height=height)
    if hovermode is not None:
        fig.update_layout(hovermode=hovermode)
    axis_common = dict(
        showline=False, zeroline=False, ticks="",
        tickfont=dict(family=_CHART_FONT, size=10, color=_CHART_FAINT),
        title_font=dict(family=_CHART_FONT, size=10, color=_CHART_MUTED),
    )
    fig.update_xaxes(showgrid=(grid == "xy"), gridcolor=_CHART_GRID, gridwidth=1, **axis_common)
    fig.update_yaxes(showgrid=(grid in ("y", "xy")), gridcolor=_CHART_GRID, gridwidth=1, **axis_common)
    return fig


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
    # 从 theme.py 换算，不再硬编码。原来写死的 rgba(224,32,32)/rgba(34,160,107)
    # 是 2026-09-04 换色板之前的旧红旧绿，跟页面上其它地方的涨跌色已经不是同一个
    # 色号了——同一屏里两种红，是那种说不出哪里怪但就是不舒服的来源。
    fill_color = _hex_to_rgba(UP_COLOR if up else DOWN_COLOR, 0.07)

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
    # 同上：成交量柱的红绿也从 theme.py 取，不写死。
    df["量色"] = [UP_COLOR if p >= pt else DOWN_COLOR for p, pt in zip(df["价格"], prev_tick)]

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
        _style_subplot_titles(fig)
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
            line=dict(width=1, color=_AUX_STRONG), name="均价",
        ),
        row=1, col=1,
    )
    if prev_close:
        fig.add_hline(y=prev_close, line=dict(width=1, color="rgba(23,24,28,0.18)"), row=1, col=1)

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

    fig.update_layout(bargap=0.15)
    _apply_chart_theme(fig, height=480 if has_volume else 340, hovermode="x unified")
    return fig


def build_candlestick(hist: pd.DataFrame) -> go.Figure:
    """K线图 + MA5/MA20 + 成交量 + MACD，三个子图。hist 需要有 日期/开盘/收盘/最高/最低/成交量 列。"""
    df = hist.copy()
    df["MA5"] = df["收盘"].rolling(5).mean()
    df["MA20"] = df["收盘"].rolling(20).mean()
    macd = _compute_macd(df["收盘"].astype(float))

    # 主图占比从0.5提到0.58，MACD从0.3压到0.26——三块面板原来接近等分，
    # 价格才是主角，成交量和MACD是辅证，不该跟主图抢高度。子图间距也收窄，
    # 三块之间原来空出很大一片，整张图显得散。
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.58, 0.16, 0.26], vertical_spacing=0.055,
        subplot_titles=("", "成交量", "MACD"),
    )
    _style_subplot_titles(fig)

    # 阳线空心、阴线实心——这是专业行情软件的通行画法，不是随手选的样式。
    # 原来阴阳线都填实底，一屏九十根全是实心色块，整张图闷得看不出结构；
    # 阳线改成只描边不填色之后，视觉重量立刻降下来，而且"这一段是涨是跌"
    # 靠实心/空心的疏密就能一眼扫出来，不用逐根去分辨红绿。
    # 描边统一 1px：默认 2px 在日线密集的时候会把相邻两根糊在一起。
    fig.add_trace(
        go.Candlestick(
            x=df["日期"],
            open=df["开盘"],
            high=df["最高"],
            low=df["最低"],
            close=df["收盘"],
            increasing=dict(line=dict(color=UP_COLOR, width=1), fillcolor="rgba(0,0,0,0)"),
            decreasing=dict(line=dict(color=DOWN_COLOR, width=1), fillcolor=DOWN_COLOR),
            whiskerwidth=0,
            name="K线",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=df["MA5"], line=dict(width=1, color=_AUX_STRONG), name="MA5"),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=df["MA20"], line=dict(width=1, color=_AUX_SOFT), name="MA20"),
        row=1,
        col=1,
    )

    vol_colors = [
        UP_COLOR if c >= o else DOWN_COLOR for o, c in zip(df["开盘"], df["收盘"])
    ]
    # 成交量柱去掉描边并压低不透明度：它是辅证，不该跟上面的K线一样浓。
    fig.add_trace(
        go.Bar(
            x=df["日期"], y=df["成交量"], marker_color=vol_colors, name="成交量",
            marker_line_width=0, opacity=0.5,
        ),
        row=2,
        col=1,
    )

    macd_colors = [UP_COLOR if v >= 0 else DOWN_COLOR for v in macd["MACD"]]
    fig.add_trace(
        go.Bar(
            x=df["日期"], y=macd["MACD"], marker_color=macd_colors, name="MACD柱",
            marker_line_width=0, opacity=0.55,
        ),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=macd["DIF"], line=dict(width=1, color=_AUX_STRONG), name="DIF"),
        row=3,
        col=1,
    )
    fig.add_trace(
        go.Scatter(x=df["日期"], y=macd["DEA"], line=dict(width=1, color=_AUX_SOFT), name="DEA"),
        row=3,
        col=1,
    )

    # bargap 从默认的0.2放大到0.45——柱子变细，柱与柱之间留出空隙。原来的柱子
    # 又宽又挤，成交量那一栏看着像一堵墙而不是一组数据。
    fig.update_layout(xaxis_rangeslider_visible=False, bargap=0.45)
    _apply_chart_theme(fig, height=700, legend=True, margin=dict(l=6, r=16, t=26, b=6))
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
            marker_color=_AUX_SOFT,
            marker_line=dict(color="rgba(23,24,28,0.25)", width=0.5),
        )
    )
    fig.add_vline(x=0, line_color="rgba(23,24,28,0.22)", line_width=1)
    fig.update_layout(xaxis_title="单日涨跌幅 (%)", yaxis_title="出现天数", bargap=0.05)
    _apply_chart_theme(fig, height=320)
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
            x=bm["日期"], y=bm["归一化"], name=benchmark_name, line=dict(color=_AUX_SOFT, width=1.5, dash="dot")
        )
    )
    fig.update_layout(yaxis_title="走势（起点=100）")
    _apply_chart_theme(fig, height=320, legend=True, hovermode="x unified")
    return fig


# 第一个和第三个位置分别用品牌红/绿（跟UP_COLOR/DOWN_COLOR保持同一个色号），
# 后面几个是纯粹的分类色（蓝/橙/紫/青），不代表涨跌方向，只用来区分不同标的。
# 凑够6个——持仓对比入口（app.py _show_compare_dialog）最多允许勾选6只，
# 凑够6个颜色能保证6只同时对比时每条线颜色都不重复。
# 2026-09-03修真实bug：_DONUT_MAX_SLICES是8，但这份调色板原来只有6个颜色，
# 持仓超过6支时第7支开始颜色按i%len(_MULTI_COLORS)折返，会跟前面某个已经用过
# 的颜色撞色——用户截图发现AI模拟炒股页7支持仓里"小米"和"国债"两块饼图颜色
# 完全一样分不清。补到9个颜色，覆盖满_DONUT_MAX_SLICES=8还留一个余量，新增
# 的三个颜色（黄/粉/靛蓝）特意选在色相环上跟原有6个颜色（红/蓝/绿/橙/紫/青）
# 拉开距离，不是随手加深浅相近的颜色凑数。
# 2026-09-04整套换掉。原来是 红/蓝/绿/橙/紫/青/黄/粉/靛 的彩虹分类色，跟
# 现在这套界面有两处硬冲突：
#   一是全站只有一条配色规矩——饱和色只留给涨跌。饼图里一块饱和蓝、一块饱和
#     紫，页面上最跳的东西就不再是涨跌数字了。
#   二是这套色板的第1个和第3个直接用了 UP_COLOR/DOWN_COLOR，于是饼图里会出现
#     一块正红和一块正绿，但它们在这里只表示"第1支"和"第3支"，不表示涨跌——
#     同一个颜色在同一个页面上表示两件事，是最容易读错的那种冲突。
# 换成一组去饱和的编辑型色板：彼此在色相和明度上都拉得开、认得出是不同的类别，
# 但没有一个会被误读成"涨"或"跌"，也不会跟界面的墨色灰阶打架。
_MULTI_COLORS = [
    "#2F3A45",  # 深石板
    "#7C8B9A",  # 钢灰
    "#8C6A4F",  # 陶土
    "#5A7480",  # 蓝灰
    "#B9A17B",  # 沙
    "#7E8C6E",  # 橄榄
    "#9A8AA3",  # 灰紫
    "#AF8578",  # 赤陶
    "#B4BCC4",  # 浅钢灰
]


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
    fig.update_layout(yaxis_title="走势（起点=100）")
    _apply_chart_theme(fig, height=380, legend=True, hovermode="x unified")
    return fig


_DONUT_MAX_SLICES = 8


def _fill_month_gaps(pts: list[dict]) -> list[dict]:
    """月频序列补齐缺失的月份，缺的那期用 value=None 占位。

    2026-09-04用户发现"美国失业率在今年一月到六月期间缺了数据"。核对下来
    确实是数据源本身的缺口：富途返回的失业率序列从 2026-01-31 直接跳到
    2026-06-30，中间四个月没有（2025-09 也缺），而同期的非农序列是完整的。

    这件事必须显式处理，不能靠画法糊过去。横轴改成分类轴之后，缺失的月份
    如果不占位，图上就是 1月紧挨着 6月——比之前时间轴留一个空档更糟：那个
    空档至少还能看出"这里有问题"，而等距排列会让人以为这两期本来就相邻，
    等于用图表撒了个谎。补上 None 占位之后，折线在缺口处会断开、柱状图会
    空一格，读者一眼就知道这段没有数据，而不是数值真的连续。

    只对判定为月频的序列生效（相邻两期间隔在26到35天之间的占多数）——日频
    和不规则序列原样返回，不去猜它该有哪些期。
    """
    if len(pts) < 3:
        return pts
    try:
        from datetime import date as _date

        def _d(p):
            y, m, dd = p["date"][:10].split("-")
            return _date(int(y), int(m), int(dd))

        parsed = [(_d(p), p) for p in pts if p.get("date")]
    except Exception:
        return pts
    if len(parsed) < 3:
        return pts
    parsed.sort(key=lambda t: t[0])
    gaps = [(parsed[i + 1][0] - parsed[i][0]).days for i in range(len(parsed) - 1)]
    monthly = sum(1 for g in gaps if 26 <= g <= 35)
    if monthly < len(gaps) * 0.6:
        return pts

    out = []
    for i, (d, p) in enumerate(parsed):
        out.append(p)
        if i + 1 >= len(parsed):
            break
        nxt = parsed[i + 1][0]
        y, m = d.year, d.month
        while True:
            m += 1
            if m > 12:
                m, y = 1, y + 1
            # 用每月1号只是为了排序和生成标签，这一期本来就没有数据
            if _date(y, m, 1) >= _date(nxt.year, nxt.month, 1):
                break
            out.append({"date": f"{y:04d}-{m:02d}-01", "value": None,
                        "predict": None, "previous": None})
    return out


def build_macro_series_chart(series: dict) -> go.Figure:
    """宏观指标图。series 是 data_sources.get_macro_series 的返回：
    {"name","unit","points":[{date,value,predict,previous}]}。

    实际值和市场预期并排画，不靠悬停。用户明确要求"这个预期的真实的最好分开来
    表示，不是刚刚悬停的那个"——之前把预期收进 tooltip，是为了躲开"柱顶一道
    黑横杠"那个问题（横杠压在柱子上既看不出偏离又划穿数值标签），但那等于把
    这张图一半的信息藏了起来：宏观数据真正被交易的维度就是"实际有没有打到
    预期"，要悬停才看得到，等于没有。并排双柱两个都看得见，高低直接比，
    既不遮挡也不需要交互。

    横轴用分类轴而不是时间轴。这些是逐月的离散读数，本来就该等距排列；用
    时间轴的话，数据源缺哪一期就会在图上留一个没有解释的空档（实测美国CPI
    同比缺 2025-09 那一期，画出来 Sep 和 Nov 之间凭空空一格，用户直接问
    "中间有个空格咋回事"）。缺失的期数不占位置，是这类周期性柱状图的常规
    画法，也不会暗示错误的时间跨度。

    水平值序列（利率、失业率这种在某个水平附近小幅波动的）仍然走折线：
    柱状图从0起画，4.1%和4.5%的柱子几乎一样高，等于什么都没表达。
    """
    pts = _fill_month_gaps((series or {}).get("points") or [])
    if not pts:
        return go.Figure()
    is_pct = series.get("unit") == "PERCENT"
    mul = 100 if is_pct else 1
    actual = [None if p["value"] is None else p["value"] * mul for p in pts]
    predict = [None if p["predict"] is None else p["predict"] * mul for p in pts]
    has_predict = any(v is not None for v in predict)

    def _label(d: str) -> str:
        # "2025-04-30" -> "25-04"：分类轴上每个刻度都要画，完整日期太长会挤成一团
        return f"{d[2:4]}-{d[5:7]}" if len(d) >= 7 else d

    x = [_label(p["date"]) for p in pts]

    def _fmt(v):
        if v is None:
            return ""
        if is_pct:
            return f"{v:.1f}%"
        av = abs(v)
        if av >= 1000:
            return f"{v/1000:.0f}k"
        return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"

    _vals_all = [v for v in actual if v is not None]
    _flat = False
    if _vals_all:
        _mx, _mn = max(_vals_all), min(_vals_all)
        _flat = _mn >= 0 and _mx > 0 and (_mx - _mn) / _mx < 0.25

    fig = go.Figure()

    if _flat:
        # 水平值：实际画实线，预期画浅色虚线，同样是两条分开的东西，不叠在一起
        label_idx = set()
        if _vals_all:
            label_idx = {0, len(actual) - 1,
                         actual.index(max(_vals_all)), actual.index(min(_vals_all))}
        if has_predict:
            fig.add_trace(
                go.Scatter(
                    x=x, y=predict, name="市场预期", mode="lines",
                    line=dict(color=_AUX_SOFT, width=1.4, dash="dot"),
                    hovertemplate="%{x}　预期 %{y:,.2f}<extra></extra>",
                )
            )
        fig.add_trace(
            go.Scatter(
                x=x, y=actual, name="实际值", mode="lines+markers+text",
                line=dict(color=_CHART_INK, width=1.6),
                marker=dict(size=4, color=_CHART_INK),
                text=[_fmt(v) if i in label_idx else "" for i, v in enumerate(actual)],
                textposition="top center",
                textfont=dict(size=10, color=_AUX_STRONG),
                hovertemplate="%{x}　实际 %{y:,.2f}<extra></extra>",
            )
        )
        # 折线这一支比柱状那支更高（320 vs 260）。这类序列的纵轴是自适应的、
        # 跨度本来就窄（失业率整段只有4.0%~4.5%这半个百分点），矮画布会把
        # 曲线压成一条几乎平的线，自适应纵轴省下来的分辨率又被高度吃回去了。
        # 柱状图靠柱长表达，高度矮一点不影响读数；折线靠斜率表达，必须给够
        # 垂直空间才看得出拐点。
        _apply_chart_theme(fig, height=320, legend=has_predict,
                           margin=dict(l=6, r=16, t=26, b=6))
        fig.update_xaxes(type="category")
        fig.update_yaxes(autorange=True, rangemode="normal")
        if is_pct:
            fig.update_yaxes(ticksuffix="%")
        return fig

    fig.add_trace(
        go.Bar(
            x=x, y=actual, name="实际值",
            marker_color=_AUX_STRONG, marker_line_width=0,
            text=[_fmt(v) for v in actual], textposition="outside",
            textfont=dict(size=10, color=_AUX_STRONG), cliponaxis=False,
            hovertemplate="%{x}　实际 %{y:,.2f}<extra></extra>",
        )
    )
    if has_predict:
        # 预期用同色系的浅色实心块，不用描边空心块：空心块在浅色背景上
        # 只剩一圈细线，视觉重量比实际值轻太多，一眼扫过去会看漏。
        fig.add_trace(
            go.Bar(
                x=x, y=predict, name="市场预期",
                marker_color=_AUX_SOFT, marker_line_width=0,
                hovertemplate="%{x}　预期 %{y:,.2f}<extra></extra>",
            )
        )
    fig.update_layout(barmode="group", bargap=0.32, bargroupgap=0.06)
    # 只给实际值标数字。两组柱子都标的话28个数字挤在一起反而都读不清，
    # 而预期的具体数值不是重点——重点是它比实际高还是低，那个用柱高比就够了。
    fig.update_layout(uniformtext=dict(minsize=9, mode="show"))
    fig.update_xaxes(type="category")
    if is_pct:
        fig.update_yaxes(ticksuffix="%")
    # 纵轴上下都要留出标签的位置。标签画在柱子外侧：正值在柱顶之上、负值在
    # 柱底之下，如果范围刚好卡在最大最小值上，负值那一侧的标签会直接压到x轴
    # 的月份刻度上（实测非农 26-01 的"-156k"和刻度文字叠在一起）。按整个数值
    # 跨度的12%上下各留一档，比按最大值乘系数更稳——含负值的序列用乘系数会
    # 把零轴附近的留白算错。
    _both = [v for v in (actual + predict) if v is not None]
    if _both:
        _hi, _lo = max(_both), min(0, min(_both))
        _pad = (_hi - _lo) * 0.12 or 1
        fig.update_yaxes(range=[_lo - (_pad if _lo < 0 else 0), _hi + _pad])
    _apply_chart_theme(fig, height=260, legend=has_predict,
                       margin=dict(l=6, r=16, t=26, b=6))
    return fig


def build_fed_watch_chart(rows: list[dict]) -> go.Figure:
    """CME FedWatch 利率概率图。rows: [{"meeting","range","prob"}]。

    2026-09-04为首页宏观议题专区新增。选横向分组条形而不是折线或饼图：
    这份数据的问题是"某次会议，市场认为利率落在哪个区间"，本质是几个离散
    区间的概率分布，条形长度最直观；横向摆是因为区间标签是"3.50-3.75%"
    这种长字符串，竖着放会挤成一团。

    同一次会议的多个区间用同一个颜色（按会议分组着色），沿用图表那套去饱和
    色板——概率高低靠长度表达，不需要再用颜色区分一次，颜色只用来区分会议。
    """
    if not rows:
        return go.Figure()
    meetings = list(dict.fromkeys(r["meeting"] for r in rows))
    fig = go.Figure()
    for i, mt in enumerate(meetings):
        sub = [r for r in rows if r["meeting"] == mt]
        # 区间从低到高排，图上从下往上就是利率由低到高，符合直觉
        sub.sort(key=lambda r: r["range"])
        fig.add_trace(
            go.Bar(
                y=[r["range"] for r in sub],
                x=[r["prob"] for r in sub],
                name=mt, orientation="h",
                marker_color=_MULTI_COLORS[i % len(_MULTI_COLORS)],
                marker_line_width=0,
                hovertemplate="%{y}　%{x:.1f}%<extra>" + mt + "</extra>",
            )
        )
    fig.update_layout(barmode="group", bargap=0.35, bargroupgap=0.06)
    fig.update_xaxes(ticksuffix="%", range=[0, 100])
    _apply_chart_theme(fig, height=max(200, 46 * max(len(set(r["range"] for r in rows)), 3)),
                       legend=True, grid="xy", margin=dict(l=6, r=16, t=26, b=6))
    return fig


# 环形图专用色板。跟折线用的 _MULTI_COLORS 分开，因为同一个颜色在两种图里
# 的观感完全不同：折线是细线，深色只是克制；环形图是大面积色块，_MULTI_COLORS
# 打头那个近黑的深石板 #2F3A45 一旦落在占比最大的扇区上，整个圆就是一坨黑。
# 用户反馈"这个持仓的图看着太不吉利了，这颜色跟死了一样"——在中文财经语境里
# 这个观感是实打实的问题，不是审美偏好。
#
# 这一组全是中明度的暖中性色：陶土、雾蓝、沙金、燕麦这类，饱和度依然压得很低
# 以维持整站克制的调子，但没有任何一个接近黑，大面积铺开也不压抑。
# 刻意避开高饱和的正红和正绿——这两个色在本项目里有明确的涨跌语义，占比图跟
# 涨跌无关，用了会让人误读成"这块在涨/在跌"。
_DONUT_COLORS = [
    "#A8896B",  # 暖陶土
    "#8CA0AE",  # 雾蓝
    "#C9B18C",  # 沙金
    "#9A8AA3",  # 灰紫
    "#B08B8B",  # 灰玫
    "#8E9BA6",  # 石青
    "#BFAE9B",  # 燕麦
    "#93A08C",  # 灰橄榄
    "#C2A6A0",  # 藕荷
]


def build_position_donut(
    holdings: list[dict], total_value_cny: float, currency_symbol: str = "¥", show_legend: bool = False,
) -> go.Figure:
    """持仓占比环形图。holdings: [{"label": "名称（代码）", "value_cny": 折算后市值}, ...]，
    调用方（app.py）负责按金额降序排好、汇率折算好——这里不做排序也不做汇率转换，
    避免图表模块反过来依赖data_sources。配色直接复用_MULTI_COLORS（用户明确要求
    按手绘草图原样用红绿等颜色，不用另外发明"避开红绿"的调色板，即使跟涨跌红绿
    语义有重叠也接受）。超过_DONUT_MAX_SLICES个持仓，尾部合并成"其他"用中性灰，
    避免小额持仓挤占过多颜色/图例空间。总资产放hole中间的annotation里，
    会跟着图一起响应式缩放，不写在图外的HTML里。

    currency_symbol（2026-09-03新增）：原本这个函数只服务持仓页(人民币¥)，AI模拟炒股页
    2026-09-02已经改成全部按美元展示，复用这个函数画它的两个新饼图时如果还写死¥就会
    显示错误的币种符号。加一个参数控制符号，字段名依然叫value_cny（沿用旧签名，
    不强改调用方already-working的字典结构），调用方传美元数字进来时这个字段名只是
    历史遗留，不影响实际显示——显示内容完全由currency_symbol决定。

    show_legend（2026-09-03新增，默认False不影响持仓页原有效果）：用户反馈"旁边标一下
    什么颜色对应什么不然看不懂"——环形图上原本只有hover悬浮才看得到名称，AI模拟炒股页
    新加的两个饼图不是鼠标常驻的场景，需要常驻图例。放在图表下方(orientation="h")而不是
    右侧，因为这两个饼图2026-09-03是用st.columns并排放的，右侧图例会把本来就不宽的图
    再挤窄。持仓页那个饼图沿用默认False，不受影响。"""
    if len(holdings) > _DONUT_MAX_SLICES:
        head = holdings[: _DONUT_MAX_SLICES - 1]
        tail_value = sum(h["value_cny"] for h in holdings[_DONUT_MAX_SLICES - 1 :])
        rows = head + [{"label": "其他", "value_cny": tail_value}]
    else:
        rows = holdings

    labels = [r["label"] for r in rows]
    values = [r["value_cny"] for r in rows]
    colors = [
        NEUTRAL_COLOR if r["label"] == "其他" else _DONUT_COLORS[i % len(_DONUT_COLORS)]
        for i, r in enumerate(rows)
    ]

    fig = go.Figure(
        go.Pie(
            labels=labels, values=values, hole=0.62,
            marker=dict(colors=colors, line=dict(color="#fff", width=2)),
            textinfo="percent", textposition="inside",
            hovertemplate=f"%{{label}}<br>{currency_symbol}%{{value:,.0f}}（%{{percent}}）<extra></extra>",
            sort=False,
        )
    )
    fig.add_annotation(
        text=f"总资产<br><b style='font-size:1.3em'>{currency_symbol}{total_value_cny:,.0f}</b>",
        showarrow=False, font=dict(size=13), align="center",
    )
    fig.update_layout(
        height=300 if not show_legend else 360,
        margin=dict(l=10, r=10, t=10, b=10),
        showlegend=show_legend,
        legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5, font=dict(size=11)) if show_legend else None,
    )
    return fig


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def build_sim_equity_curve(points: list[dict], baseline: float = 100_000.0, granularity: str = "day") -> go.Figure:
    """AI模拟盘收益曲线——points是[{"run_at": 北京时区datetime, "assets_hkd": 浮点数}]，
    按sim_agent每次运行(开盘时段每5分钟一次)时的资产快照点连线，不插值编造中间点，
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

    # 起始本金参考线。原来是点线+左上角一块带框注释，注释框压在曲线上方很占
    # 地方。改成一条极淡的实线，标签挪到右端轴外——参考线的作用是"让人一眼
    # 看出现在在它上面还是下面"，它自己不该抢戏。
    fig.add_hline(y=baseline, line=dict(color="rgba(23,24,28,0.18)", width=1))

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

    # 曲线本身。这里改了三处，都是"看着像AI生成的示意图"的直接来源：
    #
    # 1. 去掉 shape="spline"/smoothing。给金融时序做平滑曲线是硬伤——两个真实
    #    快照点之间被插出一段并不存在的圆润走势，图上那个"最高点"可能根本没
    #    发生过。净值曲线必须是直线段连点，数据是什么形状就画什么形状。
    # 2. 去掉 fill="tozeroy"。净值从来不是从0起算的，填充到0意味着那一大片
    #    色块下沿是个假的基准，视觉上还会把曲线本身的波动压扁看不出来。
    # 3. 去掉每个点的 marker。5分钟一个点，一天几十上百个圆点连成一串珠子，
    #    信息量为零。只在最后一个点保留一个实心圆当"当前位置"的锚。
    fig.add_trace(
        go.Scatter(
            x=df_plot["run_at"], y=df_plot["assets_hkd"], mode="lines", connectgaps=False,
            line=dict(color=line_color, width=1.75, shape="linear"),
            hovertemplate="%{x|%m-%d %H:%M}　$%{y:,.0f}<extra></extra>",
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
                    # 原来是红色虚线+"港股开盘"这类文字标注。一天四条红虚线
                    # 加四段文字压在曲线上，是这张图上最吵的一层；而且红色在
                    # 全站只表示"涨"，用它画时间分隔线属于语义挪用。改成极淡的
                    # 中性竖线、不带文字：分隔信息保留（配合下面的rangebreaks，
                    # 断点位置本来就一眼可见），噪声去掉。
                    fig.add_vline(x=boundary, line=dict(color="rgba(23,24,28,0.10)", width=1))

    # 当前净值。原来是"带白底、带边框、带箭头"的注释框，三层装饰只为显示一个
    # 数字。改成曲线末端一个实心圆点 + 紧挨着的纯色文字，不带框不带箭头——
    # 让数字直接长在线的末端，是财经图表里最省事也最好看的一种收尾。
    last = df.iloc[-1]
    fig.add_trace(
        go.Scatter(
            x=[last["run_at"]], y=[last["assets_hkd"]], mode="markers",
            marker=dict(size=6, color=line_color), hoverinfo="skip", showlegend=False,
        )
    )
    fig.add_annotation(
        x=last["run_at"], y=last["assets_hkd"], text=f"  ${last['assets_hkd']:,.0f}",
        showarrow=False, xanchor="left", yanchor="middle",
        font=dict(family=_CHART_FONT, size=12, color=line_color),
    )

    y_min = min(baseline, df["assets_hkd"].min())
    y_max = max(baseline, df["assets_hkd"].max())
    y_pad = max((y_max - y_min) * 0.12, baseline * 0.002)

    # X轴范围显式锁定在数据实际的[min,max]（右边留一点padding避免最后一个
    # 端点标注被裁掉）——不再靠autorange自己算，理由见上面vline那段注释。
    x_span = data_max - data_min
    x_pad = max(x_span * 0.04, pd.Timedelta(minutes=5))

    fig.update_layout(
        height=300,
        # 右边多留一截：末值数字是贴着曲线末端往右画的，留窄了会被裁掉。
        margin=dict(l=6, r=76, t=22, b=6),
        yaxis=dict(
            # 去掉"总资产（美元）"这个轴标题——刻度本身已经是 $ 开头，再写一遍
            # 是冗余，而且竖排的轴标题会把整张图往右挤。
            range=[y_min - y_pad, y_max + y_pad], zeroline=False,
            tickprefix="$", tickformat=",.0f",
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
            # "天"视图固定30分钟一格（2026-09-03从1小时调窄，AI决策频率同一天
            # 改成5分钟一次之后数据点变密，用户要求刻度密度也跟着细一档）；
            # 折线本身还是按快照实际节奏(5分钟一个点)连，刻度间隔只影响轴上
            # 标签疏密，不影响连线精细度——真实数据永远是5分钟粒度，这条
            # dtick只决定隔多久在轴上画一个刻度文字，不是重新采样。"周"/"月"
            # 视图数据跨度更大，密刻度会挤爆，继续用nticks让Plotly自己按
            # 当前range挑合适间隔。
            **(dict(dtick=30 * 60 * 1000) if granularity == "day" else dict(nticks=8)),
        ),
    )
    # 起始本金那条线的标签放在轴外右侧，跟末值数字上下错开，互不遮挡。
    fig.add_annotation(
        xref="paper", x=1.0, y=baseline, text=f"  起始 ${baseline:,.0f}",
        showarrow=False, xanchor="left", yanchor="middle",
        font=dict(family=_CHART_FONT, size=10, color=_CHART_FAINT),
    )
    _apply_chart_theme(fig, height=300, margin=dict(l=6, r=76, t=22, b=6))
    return fig

def build_fed_rate_path_chart(series: dict) -> go.Figure:
    """美联储政策利率的历次调整路径。series 来自 data_sources.get_fed_rate_path。

    2026-09-04新增，替掉原来那张两周日频的联邦基金利率图（利率两周内根本不动，
    画出来是一条平线）。

    画成阶梯线而不是普通折线：政策利率不是连续变量，它在两次会议之间是一个
    水平不动的常数，开会那天一次性跳到新水平。普通折线会把两个点之间画成
    斜线，暗示"这段时间利率在缓慢下降"，那是错的——利率在那段时间一动没动。
    阶梯线如实表达"维持一段、然后跳一下"这个真实形态。

    每个拐点标出变动后的水平和变动幅度（-25bp这样），因为"降了多少"跟"降到
    多少"是两个不同的信息，机构看利率路径两个都要看。
    """
    pts = (series or {}).get("points") or []
    if len(pts) < 2:
        return go.Figure()
    mul = 100 if series.get("unit") == "PERCENT" else 1
    x = [p["date"] for p in pts]
    y = [p["value"] * mul for p in pts]

    labels = []
    for i, v in enumerate(y):
        if i == 0:
            labels.append(f"{v:.2f}%")
            continue
        delta_bp = round((v - y[i - 1]) * 100)
        if delta_bp == 0:
            # 收尾那个"维持至今"的点不重复标水平，标了跟前一个一模一样
            labels.append("")
        else:
            labels.append(f"{v:.2f}%　{delta_bp:+d}bp")

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x, y=y, mode="lines+markers+text",
            line=dict(color=_CHART_INK, width=1.8, shape="hv"),
            marker=dict(size=5, color=_CHART_INK),
            text=labels, textposition="top right",
            textfont=dict(size=10, color=_AUX_STRONG),
            hovertemplate="%{x}　目标利率上限 %{y:.2f}%<extra></extra>",
            cliponaxis=False,
        )
    )
    _apply_chart_theme(fig, height=250, legend=False, margin=dict(l=6, r=48, t=26, b=6))
    fig.update_yaxes(ticksuffix="%", autorange=True, rangemode="normal")
    return fig
