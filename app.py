"""科学理财 Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import re
import streamlit as st
from datetime import datetime, timedelta

from data_sources import (
    get_stock_history,
    get_stock_realtime,
    get_financial_abstract,
    get_stock_news,
    get_benchmark_history,
    search_stock_by_name,
    get_stock_name,
)
from analysis import cross_validate
from tracker import log_analysis, get_history, get_due_for_review, record_review
from charts import build_candlestick, compute_stats, build_return_histogram, build_benchmark_comparison

st.set_page_config(page_title="科学理财 Agent", layout="wide")

st.title("科学理财 Agent")
st.caption("行情数据 + 财务数据 + 新闻资讯，AI 交叉核实后呈现依据链 —— 不直接给买卖建议，判断权始终在你手里。")

tab_analyze, tab_history = st.tabs(["新建分析", "历史回看"])

with tab_analyze:
    col1, col2 = st.columns([2, 1])
    with col1:
        query = st.text_input("股票代码或名称（如 600519 / 贵州茅台）", value="")
    with col2:
        st.write("")
        st.write("")
        run = st.button("开始分析", type="primary", use_container_width=True)

    symbol = None

    if run and query:
        query = query.strip()
        if re.match(r"^\d{6}$", query):
            symbol = query
        else:
            with st.spinner("按名称搜索..."):
                try:
                    matches = search_stock_by_name(query)
                except Exception as e:
                    st.error(f"名称搜索失败：{e}")
                    st.stop()
            if not matches:
                st.error(f"没找到叫「{query}」的A股个股，检查一下名称或者直接输代码。")
                st.stop()
            elif len(matches) == 1:
                symbol = matches[0]["code"]
                st.caption(f"匹配到：{matches[0]['name']}（{symbol}）")
            else:
                st.session_state["_candidates"] = matches
                st.session_state.pop("_picked_symbol", None)

    if "_candidates" in st.session_state and not symbol:
        options = {f"{m['name']}（{m['code']}）": m["code"] for m in st.session_state["_candidates"]}
        picked = st.selectbox("找到多个匹配，选一个：", list(options.keys()))
        if st.button("确认选择并分析"):
            symbol = options[picked]
            st.session_state.pop("_candidates", None)

    if symbol:
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
                stock_name = get_stock_name(symbol)
                news = get_stock_news(stock_name, limit=8)
            except Exception as e:
                st.warning(f"新闻获取失败（不影响后续分析）：{e}")
                news = None

        with st.spinner("拉取沪深300基准..."):
            try:
                benchmark = get_benchmark_history(start, end)
            except Exception:
                benchmark = None

        if hist is None or hist.empty:
            st.error("没有获取到行情数据，检查一下股票代码是否正确。")
            st.stop()

        st.divider()
        st.subheader("行情与统计")
        st.caption("本区块的数字和图表全部本地直接算出来，不经过 AI —— 跟下面 AI 的文字分析是两条独立的证据链。")

        with st.container(border=True):
            st.plotly_chart(build_candlestick(hist), use_container_width=True)

            stats = compute_stats(hist)
            cols = st.columns(len(stats))
            for col, (label, value) in zip(cols, stats.items()):
                col.metric(label, value)

            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.markdown("**每日涨跌幅分布**")
                st.plotly_chart(build_return_histogram(hist), use_container_width=True)
            with chart_col2:
                if benchmark is not None and not benchmark.empty:
                    st.markdown("**对比沪深300（起点=100）**")
                    st.plotly_chart(build_benchmark_comparison(hist, benchmark), use_container_width=True)
                else:
                    st.markdown("**对比沪深300**")
                    st.caption("基准数据暂时获取不到，不影响其他分析。")

        st.subheader("财务摘要")
        if fin is not None and not fin.empty:
            st.dataframe(fin, use_container_width=True, hide_index=True)
        else:
            st.caption("暂无财务数据。")

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

        st.divider()
        st.subheader("AI 交叉验证分析")
        with st.container(border=True):
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
