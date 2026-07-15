"""科学理财 Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import os
import re
import pandas as pd
import streamlit as st
import streamlit.components.v1 as _cv1
from datetime import datetime, timedelta

from data_sources import (
    get_stock_history,
    get_stock_realtime,
    get_financial_abstract,
    get_stock_news,
    get_benchmark_history,
    search_stock_by_name,
    get_stock_name,
    check_stock_valid,
)
from analysis import cross_validate, summarize_financials
from tracker import (
    log_analysis, get_history, get_due_for_review, record_review,
    add_to_watchlist, remove_from_watchlist, is_in_watchlist, get_watchlist,
)
from charts import (
    build_candlestick, compute_stats, build_return_histogram,
    build_benchmark_comparison, build_multi_comparison,
)
from auth import (
    _check_user, _register_user, _create_token, _validate_token,
    _invalidate_token, _hash_pw, _user_exists,
)

for _k in ("SUPABASE_URL", "SUPABASE_KEY"):
    if _k not in os.environ:
        try:
            os.environ[_k] = st.secrets[_k]
        except Exception:
            pass

st.set_page_config(page_title="科学理财 Agent", layout="wide")


def _show_login_page():
    st.markdown(
        "<div style='text-align:center;padding:60px 0 24px'>"
        "<div style='font-size:1.5rem;font-weight:600;margin:8px 0 4px'>科学理财 Agent</div>"
        "<div style='font-size:0.85rem;color:#888'>行情 + 财务 + 新闻交叉验证 · 登录后开始使用</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        tab_l, tab_r = st.tabs(["登录", "注册"])
        with tab_l:
            _em = st.text_input("邮箱", key="li_email", placeholder="your@email.com")
            _pw = st.text_input("密码", type="password", key="li_pw")
            if st.button("登录", type="primary", use_container_width=True, key="do_login"):
                _ok, _msg = _check_user(_em, _pw)
                if _ok:
                    _tok = _create_token(_em)
                    st.query_params["_auth"] = _tok
                    st.session_state["logged_in"] = True
                    st.session_state["user_email"] = _em
                    st.session_state["_token"] = _tok
                    _cv1.html(
                        f'<script>try{{window.parent.localStorage.setItem("fa_auth_tok","{_tok}");}}catch(e){{}}</script>',
                        height=1,
                    )
                    st.rerun()
                else:
                    st.error(_msg)
        with tab_r:
            _rem = st.text_input("邮箱", key="reg_email", placeholder="your@email.com")
            _rpw = st.text_input("密码（至少6位）", type="password", key="reg_pw")
            _rpw2 = st.text_input("确认密码", type="password", key="reg_pw2")
            if st.button("注册账号", type="primary", use_container_width=True, key="do_reg"):
                if not _rem or "@" not in _rem:
                    st.error("请输入有效邮箱")
                elif len(_rpw) < 6:
                    st.error("密码至少6位")
                elif _rpw != _rpw2:
                    st.error("两次密码不一致")
                elif _user_exists(_rem):
                    st.error("该邮箱已注册（跟 math-agent 共用同一套账号，那边注册过这里也能直接登）")
                else:
                    try:
                        _register_user(_rem, _hash_pw(_rpw))
                        st.success("注册成功，请切换到登录标签页")
                    except Exception as _e:
                        st.error(f"注册失败：{_e}")


# ── localStorage 自动登录（关闭浏览器后用书签/快捷方式打开也能恢复）────────────
_cv1.html("""
<script>
(function() {
    try {
        var url = new URL(window.parent.location.href);
        if (!url.searchParams.get('_auth')) {
            var t = window.parent.localStorage.getItem('fa_auth_tok');
            if (t) {
                url.searchParams.set('_auth', t);
                window.parent.history.replaceState(null, '', url.toString());
                setTimeout(function() {
                    if (!new URL(window.parent.location.href).searchParams.get('_auth')) return;
                    window.parent.location.replace(url.toString());
                }, 800);
            }
        }
    } catch(e) {}
})();
</script>
""", height=1)

_stored_token = st.query_params.get("_auth", "") or ""
if _stored_token and not st.session_state.get("logged_in"):
    _auto_email = _validate_token(_stored_token)
    if _auto_email:
        st.session_state["logged_in"] = True
        st.session_state["user_email"] = _auto_email
        st.session_state["_token"] = _stored_token
    else:
        try:
            del st.query_params["_auth"]
        except Exception:
            pass
        _cv1.html(
            '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");}catch(e){}</script>',
            height=1,
        )

if not st.session_state.get("logged_in"):
    _show_login_page()
    st.stop()

with st.sidebar:
    _uemail = st.session_state.get("user_email", "")
    _uemail_safe = _uemail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    st.markdown(f"<p style='font-size:0.8rem;color:#888'>{_uemail_safe}</p>", unsafe_allow_html=True)
    if st.button("退出登录", use_container_width=True):
        _tok = st.session_state.pop("_token", None)
        if _tok:
            _invalidate_token(_tok)
        try:
            del st.query_params["_auth"]
        except Exception:
            pass
        _cv1.html(
            '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");}catch(e){}</script>',
            height=1,
        )
        st.session_state["logged_in"] = False
        st.session_state.pop("user_email", None)
        st.rerun()

st.title("科学理财 Agent")
st.caption("行情数据 + 财务数据 + 新闻资讯，AI 交叉核实后呈现依据链 —— 不直接给买卖建议，判断权始终在你手里。")

tab_watchlist, tab_analyze, tab_compare, tab_history = st.tabs(["自选股", "新建分析", "多股对比", "历史回看"])

with tab_watchlist:
    _email = st.session_state["user_email"]
    watched = get_watchlist(_email)
    if not watched:
        st.write("还没有关注任何股票——去「新建分析」分析一只股票，结果页顶部能一键加入自选。")
    else:
        for item in watched:
            wc1, wc2, wc3, wc4 = st.columns([2, 2, 1, 1])
            try:
                wspot = get_stock_realtime(item["symbol"])
            except Exception:
                wspot = {}
            wc1.write(f"**{item['name']}**（{item['symbol']}）")
            if wspot and wspot.get("最新价"):
                wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
                wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
                color = "red" if wchange >= 0 else "green"
                wc2.markdown(
                    f"{wspot['最新价']:.2f} "
                    f"<span style='color:{color}'>{wchange:+.2f} ({wchange_pct:+.2f}%)</span>",
                    unsafe_allow_html=True,
                )
            else:
                wc2.write("行情获取失败")
            if wc3.button("分析", key=f"wl_analyze_{item['symbol']}"):
                st.session_state["_active_symbol"] = item["symbol"]
                st.session_state.pop("_analysis_cache", None)
                st.info("已定位到该股票，切换到「新建分析」标签页查看结果。")
            if wc4.button("移除", key=f"wl_remove_{item['symbol']}"):
                remove_from_watchlist(_email, item["symbol"])
                st.rerun()

with tab_analyze:
    market = st.radio("市场", ["A股", "港股", "美股"], horizontal=True, key="_market_select")
    market_code = {"A股": "A", "港股": "HK", "美股": "US"}[market]
    placeholder = {"A股": "600519 / 贵州茅台", "港股": "00700（腾讯控股）", "美股": "AAPL（苹果）"}[market]

    if market_code != "A":
        st.caption("港股/美股目前只支持直接输代码，暂不支持按名称搜索、财务摘要和新闻（后续再补）。")

    col1, col2 = st.columns([2, 1])
    with col1:
        query = st.text_input(f"股票代码或名称（如 {placeholder}）", value="", key="_query_input")
    with col2:
        st.write("")
        st.write("")
        run = st.button("开始分析", type="primary", use_container_width=True)

    symbol = None

    if run and query:
        query = query.strip()
        if market_code != "A":
            # 港股/美股：phase 1 先只支持直接输代码，不做名称搜索/退市校验
            symbol = query.upper() if market_code == "US" else query
        elif re.match(r"^\d{6}$", query):
            valid, msg_or_name = check_stock_valid(query)
            if not valid:
                st.error(msg_or_name)
                st.stop()
            symbol = query
            st.caption(f"匹配到：{msg_or_name}（{symbol}）")
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
        st.session_state["_active_symbol"] = symbol
        st.session_state["_active_market"] = market_code
        st.session_state.pop("_analysis_cache", None)  # 新点了一次分析，之前缓存的结果作废

    active_symbol = st.session_state.get("_active_symbol")
    active_market = st.session_state.get("_active_market", "A")

    if active_symbol:
        symbol = active_symbol

        if "_analysis_cache" not in st.session_state:
            with st.spinner("拉取行情数据..."):
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
                try:
                    hist = get_stock_history(symbol, start, end, market=active_market)
                except Exception as e:
                    st.error(f"行情数据获取失败：{e}")
                    st.stop()

            with st.spinner("拉取实时快照..."):
                try:
                    spot = get_stock_realtime(symbol, market=active_market)
                except Exception as e:
                    st.warning(f"实时快照获取失败（不影响后续分析）：{e}")
                    spot = {}

            if active_market == "A":
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
            else:
                fin, news, benchmark = None, None, None

            if hist is None or hist.empty:
                st.error("没有获取到行情数据，检查一下股票代码是否正确。")
                st.stop()

            stats = compute_stats(hist)

            history_summary = hist.tail(20).to_string(index=False)
            if spot and spot.get("最新价"):
                history_summary += (
                    f"\n\n实时行情快照（用户此刻正在看到的价格，{spot.get('更新时间', '')}）："
                    f"最新价{spot['最新价']}，今开{spot.get('今开')}，"
                    f"最高{spot.get('最高')}，最低{spot.get('最低')}，昨收{spot.get('昨收')}"
                )
            history_summary += "\n\n统计指标（本地计算，非AI生成）：" + "，".join(
                f"{k}={v}" for k, v in stats.items()
            )
            if active_market != "A":
                financial_summary = "暂不支持（港股/美股财务数据接口还没接，仅先支持行情分析）"
                news_summary = "暂不支持（港股/美股新闻源还没接）"
            else:
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

            fin_summary_text = ""
            if fin is not None and not fin.empty:
                with st.spinner("AI 正在总结财务数据..."):
                    try:
                        fin_summary_text = summarize_financials(symbol, financial_summary)
                    except Exception:
                        fin_summary_text = ""

            current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
            log_analysis(st.session_state["user_email"], symbol, float(current_price), result)

            st.session_state["_analysis_cache"] = {
                "hist": hist, "spot": spot, "fin": fin, "news": news,
                "benchmark": benchmark, "stats": stats, "result": result,
                "fin_summary_text": fin_summary_text,
            }

        cache = st.session_state["_analysis_cache"]
        hist, spot, fin, news = cache["hist"], cache["spot"], cache["fin"], cache["news"]
        benchmark, stats, result = cache["benchmark"], cache["stats"], cache["result"]
        fin_summary_text = cache.get("fin_summary_text", "")

        if spot and spot.get("最新价"):
            change = spot["最新价"] - spot["昨收"]
            change_pct = change / spot["昨收"] * 100 if spot["昨收"] else 0
            qcol1, qcol2, qcol3, qcol4, qcol5 = st.columns([2, 1, 1, 1, 1])
            qcol1.metric(
                spot.get("名称", symbol),
                f"{spot['最新价']:.2f}",
                f"{change:+.2f} ({change_pct:+.2f}%)",
            )
            qcol2.metric("今开", f"{spot.get('今开', 0):.2f}")
            qcol3.metric("最高", f"{spot.get('最高', 0):.2f}")
            qcol4.metric("最低", f"{spot.get('最低', 0):.2f}")
            with qcol5:
                st.write("")
                _watched_now = is_in_watchlist(st.session_state["user_email"], symbol)
                if _watched_now:
                    if st.button("移除自选", key="wl_toggle"):
                        remove_from_watchlist(st.session_state["user_email"], symbol)
                        st.rerun()
                else:
                    if st.button("加入自选", key="wl_toggle"):
                        add_to_watchlist(st.session_state["user_email"], symbol, spot.get("名称", symbol))
                        st.rerun()
            st.caption(f"更新时间：{spot.get('更新时间', '未知')}（新浪实时行情，非收盘价）")

        st.divider()
        st.subheader("行情与统计")
        st.caption("本区块的数字和图表全部本地直接算出来，不经过 AI —— 跟下面 AI 的文字分析是两条独立的证据链。")

        with st.container(border=True):
            if active_market == "A":
                period_options = {"分时K（今日）": ("5", 1), "日K": ("d", 90), "周K": ("w", 730), "月K": ("m", 1825)}
                period_label = st.radio(
                    "K线周期", list(period_options.keys()), horizontal=True, key="_kline_period"
                )
                freq, days_back = period_options[period_label]
                chart_end = datetime.now().strftime("%Y%m%d")
                chart_start = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
                try:
                    chart_hist = get_stock_history(symbol, chart_start, chart_end, frequency=freq, market=active_market)
                except Exception as e:
                    st.warning(f"该周期数据获取失败：{e}")
                    chart_hist = hist
            else:
                st.caption("港股/美股暂时只支持日K，周期切换后续再补。")
                chart_hist = hist

            if chart_hist is not None and not chart_hist.empty:
                st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)
            else:
                st.caption("该周期暂无数据。")

            cols = st.columns(len(stats))
            for col, (label, value) in zip(cols, stats.items()):
                col.metric(label, value)

            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.markdown("**每日涨跌幅分布**")
                st.plotly_chart(build_return_histogram(hist), use_container_width=True)
            with chart_col2:
                if active_market != "A":
                    st.markdown("**基准对比**")
                    st.caption("港股/美股暂不支持基准指数对比。")
                elif benchmark is not None and not benchmark.empty:
                    st.markdown("**对比沪深300（起点=100）**")
                    st.plotly_chart(build_benchmark_comparison(hist, benchmark), use_container_width=True)
                else:
                    st.markdown("**对比沪深300**")
                    st.caption("基准数据暂时获取不到，不影响其他分析。")

        st.subheader("财务摘要")
        if fin is not None and not fin.empty:
            st.dataframe(fin, use_container_width=True, hide_index=True)
            if fin_summary_text:
                st.markdown(fin_summary_text)
        else:
            st.caption("暂无财务数据。")

        st.divider()
        st.subheader("AI 交叉验证分析")
        with st.container(border=True):
            st.markdown(result)

        st.caption("本次分析已自动记录，过几天回来在「历史回看」里能看到后续走势对照。")

        if news is not None and not news.empty:
            with st.expander("原始新闻列表"):
                st.dataframe(news, use_container_width=True)

with tab_compare:
    st.subheader("多股对比")
    st.caption("输入2-5只股票，用逗号分隔（代码或名称都行），走势归一化到同一起点，直接看谁涨得多。")

    cmp_query = st.text_input("股票列表", placeholder="600519,000858,贵州茅台", key="cmp_input")
    include_benchmark = st.checkbox("同时对比沪深300", value=True, key="cmp_benchmark")
    cmp_run = st.button("开始对比", type="primary", key="cmp_run")

    if cmp_run and cmp_query:
        raw_items = [x.strip() for x in re.split(r"[,，]", cmp_query) if x.strip()]
        if len(raw_items) < 2:
            st.error("至少输入2只股票才能对比。")
        elif len(raw_items) > 5:
            st.error("最多支持5只股票，太多了图会看不清。")
        else:
            resolved = {}  # 显示名 -> symbol
            failed = []
            for item in raw_items:
                if re.match(r"^\d{6}$", item):
                    ok, name_or_msg = check_stock_valid(item)
                    if ok:
                        resolved[f"{name_or_msg}（{item}）"] = item
                    else:
                        failed.append(f"{item}：{name_or_msg}")
                else:
                    matches = search_stock_by_name(item)
                    if matches:
                        m = matches[0]
                        resolved[f"{m['name']}（{m['code']}）"] = m["code"]
                    else:
                        failed.append(f"{item}：没找到匹配的股票")

            if failed:
                st.warning("这几个没解析成功，已跳过：\n" + "\n".join(f"- {f}" for f in failed))

            if len(resolved) < 2:
                st.error("有效股票不足2只，没法对比。")
            else:
                with st.spinner("拉取行情数据..."):
                    end = datetime.now().strftime("%Y%m%d")
                    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
                    hist_by_name = {}
                    for name, sym in resolved.items():
                        try:
                            h = get_stock_history(sym, start, end)
                            if h is not None and not h.empty:
                                hist_by_name[name] = h
                        except Exception:
                            pass
                    if include_benchmark:
                        try:
                            bm = get_benchmark_history(start, end)
                            if bm is not None and not bm.empty:
                                hist_by_name["沪深300"] = bm
                        except Exception:
                            pass

                if len(hist_by_name) < 2:
                    st.error("拉取到的有效数据不足2只，没法对比。")
                else:
                    st.plotly_chart(build_multi_comparison(hist_by_name), use_container_width=True)

                    st.markdown("**统计对比**")
                    stat_rows = []
                    for name, h in hist_by_name.items():
                        s = compute_stats(h)
                        s["股票"] = name
                        stat_rows.append(s)
                    stat_df = pd.DataFrame(stat_rows).set_index("股票")
                    st.dataframe(stat_df, use_container_width=True)

with tab_history:
    st.subheader("历史分析回看")

    due = get_due_for_review(st.session_state["user_email"], min_age_days=7)
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
    records = get_history(st.session_state["user_email"], limit=50)
    if not records:
        st.write("还没有分析记录，去「新建分析」跑一个吧。")
    for r in records:
        change = ""
        if r["review_price"]:
            pct = (r["review_price"] - r["price_at_analysis"]) / r["price_at_analysis"] * 100
            change = f" → 回看价 {r['review_price']}（{pct:+.1f}%）"
        with st.expander(f"{r['symbol']} · {r['created_at'][:16]} · 当时价 {r['price_at_analysis']}{change}"):
            st.markdown(r["analysis_text"])
