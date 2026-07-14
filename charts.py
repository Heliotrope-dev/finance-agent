"""统计/可视化层 —— K线图 + 均线 + 成交量，外加真正算出来的统计指标。

这里的数字都是本地用 pandas 直接算的，不经过 AI，
跟 analysis.py 里 AI 的文字判断是两条独立的证据链。
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def build_candlestick(hist: pd.DataFrame) -> go.Figure:
    """K线图 + MA5/MA20 + 成交量子图。hist 需要有 日期/开盘/收盘/最高/最低/成交量 列。"""
    df = hist.copy()
    df["MA5"] = df["收盘"].rolling(5).mean()
    df["MA20"] = df["收盘"].rolling(20).mean()

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03
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

    fig.update_layout(
        height=520,
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig


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
