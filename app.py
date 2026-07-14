"""科学理财 Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import re
import streamlit as st
from datetime import datetime, timedelta

from data_sources import (
    get_stock_history,
    get_stock_realtime,
    get_financial_abstract,
    get_stock_news,
)
from analysis import cross_validate
from tracker import log_analysis, get_history, get_due_for_review, record_review
from charts import build_candlestick, compute_stats

st.set_page_config(page_title="科学理财 Agent", page_icon="📊", layout="wide")

st.title("📊 科学理财 Agent")
st.caption("行情数据 + 财务数据 + 新闻资讯，AI 交叉核实后呈现依据链 —— 不直接给买卖建议，判断权始终在你手里。")

tab_analyze, tab_history = st.tabs(["新建分析", "历史回看"])

with tab_analyze:
    col1, col2 = st.columns([2, 1])
    with col1:
        symbol = st.text_input("股票代码（如 600519）", value="", max_chars=6)
    with col2:
        st.write("")
        st.write("")
        run = st.button("开始分析", type="primary", use_container_width=True)

    if run and symbol:
        symbol = symbol.strip()
        if not re.match(r"^\d{6}$", symbol):
            st.error("股票代码格式不对，应为 6 位纯数字，例如 600519、000001。")
            st.stop()

        with st.spinner("拉取行情数据..."):
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
            try:
                hist = get_stock_history(symbol, start, end)
            except Exception as e:
                st.error(f"行情数据获取失败：{e}")
                st.stop()

        with st.spinner("拉取实时快照..."):
            try:
                spot = get_stock_realtime(symbol)
            except Exception as e:
                st.warning(f"实时快照获取失败（不影响后续分析）：{e}")
                spot = {}

        with st.spinner("拉取财务数据..."):
            try:
                fin = get_financial_abstract(symbol)
            except Exception as e:
                st.warning(f"财务数据获取失败（不影响后续分析）：{e}")
                fin = None

        with st.spinner("拉取相关新闻..."):
            try:
                news = get_stock_news(symbol, limit=8)
            except Exception as e:
                st.warning(f"新闻获取失败（不影响后续分析）：{e}")
                news = None

        if hist is None or hist.empty:
            st.error("没有获取到行情数据，检查一下股票代码是否正确。")
            st.stop()

        st.subheader("K线图")
        st.plotly_chart(build_candlestick(hist), use_container_width=True)

        st.subheader("统计指标")
        st.caption("以下数字是本地直接算出来的，不经过 AI —— 跟下面的 AI 文字分析是两条独立的证据链。")
        stats = compute_stats(hist)
        cols = st.columns(len(stats))
        for col, (label, value) in zip(cols, stats.items()):
            col.metric(label, value)

        history_summary = hist.tail(20).to_string(index=False)
        history_summary += "\n\n统计指标（本地计算，非AI生成）：" + "，".join(
            f"{k}={v}" for k, v in stats.items()
        )
        financial_summary = fin.head(10).to_string(index=False) if fin is not None and not fin.empty else "无可用数据"
        news_summary = (
            "\n".join(f"- {row['新闻标题']}：{row['新闻内容'][:100]}" for _, row in news.iterrows())
            if news is not None and not news.empty
            else "无相关新闻"
        )

        with st.spinner("AI 正在交叉核实新闻与数据..."):
            try:
                result = cross_validate(symbol, history_summary, financial_summary, news_summary)
            except Exception as e:
                st.error(f"分析失败：{e}")
                st.stop()

        st.subheader("交叉验证分析")
        st.markdown(result)

        current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
        log_analysis(symbol, float(current_price), result)
        st.success(f"已记录本次分析（当时价格 {current_price}），过几天回来在「历史回看」里能看到后续走势对照。")

        if news is not None and not news.empty:
            with st.expander("原始新闻列表"):
                st.dataframe(news, use_container_width=True)

with tab_history:
    st.subheader("历史分析回看")

    due = get_due_for_review(min_age_days=7)
    if due:
        st.info(f"有 {len(due)} 条分析记录满 7 天了，补录一下后续价格才能看到对照效果。")
        for row in due:
            c1, c2, c3 = st.columns([2, 2, 1])
            c1.write(f"{row['symbol']}（{row['created_at'][:10]}，当时价 {row['price_at_analysis']}）")
            new_price = c2.number_input("现在价格", key=f"review_{row['id']}", min_value=0.0, step=0.01)
            if c3.button("补录", key=f"btn_{row['id']}") and new_price:
                record_review(row["id"], new_price)
                st.rerun()

    st.divider()
    records = get_history(limit=50)
    if not records:
        st.write("还没有分析记录，去「新建分析」跑一个吧。")
    for r in records:
        change = ""
        if r["review_price"]:
            pct = (r["review_price"] - r["price_at_analysis"]) / r["price_at_analysis"] * 100
            change = f" → 回看价 {r['review_price']}（{pct:+.1f}%）"
        with st.expander(f"{r['symbol']} · {r['created_at'][:16]} · 当时价 {r['price_at_analysis']}{change}"):
            st.markdown(r["analysis_text"])
