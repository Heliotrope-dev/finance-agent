"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

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

st.set_page_config(page_title="Invest Agent", layout="wide")


def _show_login_page():
    st.markdown(
        "<div style='text-align:center;padding:60px 0 24px'>"
        "<div style='font-size:1.5rem;font-weight:600;margin:8px 0 4px'>Invest Agent</div>"
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


def _build_stock_analysis(symbol: str, market: str, email: str) -> dict:
    """拉数据+跑AI分析，返回结果字典。详情页和「新建分析」标签页各自独立调用，
    互不影响缓存生命周期（各自决定什么时候该重新拉数据）。"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    hist = get_stock_history(symbol, start, end, market=market)
    if hist is None or hist.empty:
        raise RuntimeError("没有获取到行情数据，检查一下股票代码是否正确。")

    try:
        spot = get_stock_realtime(symbol, market=market)
    except Exception:
        spot = {}
    try:
        fin = get_financial_abstract(symbol, market=market)
    except Exception:
        fin = None
    try:
        stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
        news = get_stock_news(stock_name, limit=8)
    except Exception:
        news = None
    try:
        benchmark = get_benchmark_history(start, end, market=market)
    except Exception:
        benchmark = None

    stats = compute_stats(hist)
    history_summary = hist.tail(20).to_string(index=False)
    if spot and spot.get("最新价"):
        history_summary += (
            f"\n\n实时行情快照（用户此刻正在看到的价格，{spot.get('更新时间', '')}）："
            f"最新价{spot['最新价']}，今开{spot.get('今开')}，"
            f"最高{spot.get('最高')}，最低{spot.get('最低')}，昨收{spot.get('昨收')}"
        )
    history_summary += "\n\n统计指标（本地计算，非AI生成）：" + "，".join(f"{k}={v}" for k, v in stats.items())
    financial_summary = fin.head(10).to_string(index=False) if fin is not None and not fin.empty else "无可用数据"
    news_summary = (
        "\n".join(f"- {row['新闻标题']}：{row['新闻内容'][:100]}" for _, row in news.iterrows())
        if news is not None and not news.empty else "无相关新闻"
    )

    result = cross_validate(symbol, history_summary, financial_summary, news_summary)

    fin_summary_text = ""
    if fin is not None and not fin.empty:
        try:
            fin_summary_text = summarize_financials(symbol, financial_summary)
        except Exception:
            fin_summary_text = ""

    current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
    log_analysis(email, symbol, float(current_price), result)

    return {
        "hist": hist, "spot": spot, "fin": fin, "news": news,
        "benchmark": benchmark, "stats": stats, "result": result,
        "fin_summary_text": fin_summary_text,
    }


def _render_stock_detail(symbol: str, market: str, name: str):
    if st.button("← 返回自选股"):
        st.session_state.pop("_detail_symbol", None)
        st.session_state.pop("_detail_market", None)
        st.session_state.pop("_detail_name", None)
        st.rerun()

    st.markdown(
        f"""
        <div style='background:#e02020;margin:-1rem -1rem 0 -1rem;padding:14px 24px'>
            <div style='color:#fff;font-size:1.2rem;font-weight:700'>{name}</div>
            <div style='color:#fff;font-size:0.85rem;opacity:0.85'>{symbol} · {market}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cache_key = f"_detail_cache_{symbol}_{market}"
    if cache_key not in st.session_state:
        with st.spinner("加载中..."):
            try:
                st.session_state[cache_key] = _build_stock_analysis(symbol, market, st.session_state["user_email"])
            except Exception as e:
                st.error(f"加载失败：{e}")
                return

    cache = st.session_state[cache_key]
    hist, spot, fin = cache["hist"], cache["spot"], cache["fin"]
    benchmark, stats, result = cache["benchmark"], cache["stats"], cache["result"]
    fin_summary_text = cache.get("fin_summary_text", "")

    if spot and spot.get("最新价"):
        change = spot["最新价"] - spot.get("昨收", spot["最新价"])
        change_pct = change / spot["昨收"] * 100 if spot.get("昨收") else 0
        color = "#e02020" if change >= 0 else "#22a06b"
        st.markdown(
            f"<div style='margin:12px 0'>"
            f"<span style='font-size:2rem;font-weight:700;color:{color}'>{spot['最新价']:.2f}</span>&nbsp;&nbsp;"
            f"<span style='font-size:1.1rem;color:{color}'>{change:+.2f} ({change_pct:+.2f}%)</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        hcol1, hcol2, hcol3 = st.columns(3)
        hcol1.metric("最高", f"{spot.get('最高', 0):.2f}")
        hcol2.metric("最低", f"{spot.get('最低', 0):.2f}")
        hcol3.metric("今开", f"{spot.get('今开', 0):.2f}")

    st.divider()
    if market == "A":
        period_options = {"分时K（今日）": ("5", 1), "日K": ("d", 90), "周K": ("w", 730), "月K": ("m", 1825)}
        period_label = st.radio("K线周期", list(period_options.keys()), horizontal=True, key="_detail_kline_period")
        freq, days_back = period_options[period_label]
        c_end = datetime.now().strftime("%Y%m%d")
        c_start = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            chart_hist = get_stock_history(symbol, c_start, c_end, frequency=freq, market=market)
        except Exception:
            chart_hist = hist
    else:
        chart_hist = hist
    if chart_hist is not None and not chart_hist.empty:
        st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)

    st.divider()
    st.subheader("AI 分析")
    with st.container(border=True):
        st.markdown(result)

    st.subheader("财务摘要")
    if fin is not None and not fin.empty:
        st.dataframe(fin, use_container_width=True, hide_index=True)
        if fin_summary_text:
            st.markdown(fin_summary_text)
    else:
        st.caption("暂无财务数据。")


@st.dialog("确认删除")
def _confirm_delete_dialog(email: str, symbol: str, name: str):
    st.write(f"确定要把「{name}」（{symbol}）从自选股里删除吗？")
    dc1, dc2 = st.columns(2)
    if dc1.button("确认删除", type="primary", use_container_width=True):
        remove_from_watchlist(email, symbol)
        st.rerun()
    if dc2.button("取消", use_container_width=True):
        st.rerun()


if st.session_state.get("_detail_symbol"):
    _render_stock_detail(
        st.session_state["_detail_symbol"],
        st.session_state.get("_detail_market", "A"),
        st.session_state.get("_detail_name", st.session_state["_detail_symbol"]),
    )
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

st.markdown(
    """
    <div style='background:#e02020;margin:-1rem -1rem 0 -1rem;padding:14px 24px;
                display:flex;align-items:center;justify-content:space-between'>
        <span style='color:#fff;font-size:1.3rem;font-weight:700;letter-spacing:.02em'>☰ &nbsp;Invest Agent</span>
        <span style='color:#fff;font-size:0.8rem;opacity:0.85'>行情 · 财务 · 新闻交叉验证</span>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=60, show_spinner=False)
def _index_snapshot(idx_market: str):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
    df = get_benchmark_history(start, end, market=idx_market)
    if df is None or len(df) < 2:
        return None
    last, prev = float(df.iloc[-1]["收盘"]), float(df.iloc[-2]["收盘"])
    change = last - prev
    pct = change / prev * 100 if prev else 0
    return last, change, pct


_INDEX_LABELS = {"A": "上证指数", "HK": "恒生指数", "US": "标普500"}
idx_pick = st.radio("大盘指数", list(_INDEX_LABELS.values()), horizontal=True, key="_idx_pick", label_visibility="collapsed")
idx_market_pick = {v: k for k, v in _INDEX_LABELS.items()}[idx_pick]
try:
    idx_snap = _index_snapshot(idx_market_pick)
except Exception:
    idx_snap = None
if idx_snap:
    idx_last, idx_change, idx_pct = idx_snap
    idx_color = "#e02020" if idx_change >= 0 else "#22a06b"
    st.markdown(
        f"<div style='margin:-8px 0 12px'>"
        f"<span style='font-size:1.4rem;font-weight:700;color:{idx_color}'>{idx_last:,.2f}</span>&nbsp;"
        f"<span style='color:{idx_color}'>{idx_change:+.2f} ({idx_pct:+.2f}%)</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _auto_detect_market(q: str) -> str | None:
    if re.match(r"^\d{6}$", q):
        return "A"
    if re.match(r"^\d{4,5}$", q):
        return "HK"
    if re.match(r"^[A-Za-z.]{1,6}$", q):
        return "US"
    return None


qcol, bcol = st.columns([5, 1])
quick_query = qcol.text_input(
    "🔍 快速搜索代码，跳K线图（按名称搜索请用下面「新建分析」）",
    value="", key="_quick_search", placeholder="600519 / 00700 / AAPL",
)
if bcol.button("搜索", key="_quick_search_btn", use_container_width=True) and quick_query:
    q = quick_query.strip()
    detected = _auto_detect_market(q)
    if detected is None:
        st.warning("这个格式看着像名称，快速搜索只支持直接输代码——用下面「新建分析」标签页按名称搜。")
    else:
        sym = q.zfill(5) if detected == "HK" else (q.upper() if detected == "US" else q)
        st.session_state["_active_symbol"] = sym
        st.session_state["_active_market"] = detected
        st.session_state.pop("_analysis_cache", None)
        st.success(f"已定位到 {sym}（{ {'A':'A股','HK':'港股','US':'美股'}[detected] }），切换到「新建分析」标签页查看结果。")

tab_market, tab_watchlist, tab_analyze, tab_compare, tab_history = st.tabs(
    ["行情", "自选股", "新建分析", "多股对比", "历史回看"]
)

with tab_market:
    mkt_pick = st.radio("市场", ["A股", "港股", "美股"], horizontal=True, key="_market_overview_pick")
    mkt_code = {"A股": "A", "港股": "HK", "美股": "US"}[mkt_pick]

    try:
        idx_list = get_multi_index_snapshot(mkt_code)
    except Exception:
        idx_list = []

    if idx_list:
        idx_cols = st.columns(len(idx_list))
        for col, idx in zip(idx_cols, idx_list):
            color = "#e02020" if idx["涨跌"] >= 0 else "#22a06b"
            with col:
                st.markdown(
                    f"<div style='background:#f8f8f8;border-radius:8px;padding:12px;text-align:center'>"
                    f"<div style='font-size:0.8rem;color:#666'>{idx['名称']}</div>"
                    f"<div style='font-size:1.3rem;font-weight:700;color:{color}'>{idx['最新']:,.2f}</div>"
                    f"<div style='font-size:0.85rem;color:{color}'>{idx['涨跌']:+.2f} ({idx['涨跌幅']:+.2f}%)</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
    else:
        st.caption("指数数据暂时获取不到。")

    st.divider()

    if mkt_code != "A":
        st.caption(f"{mkt_pick}没有涨跌停限制制度，这块统计只适用于A股，暂不显示。")
    else:
        try:
            breadth = get_market_breadth()
        except Exception:
            breadth = {}
        if breadth:
            bcols = st.columns(6)
            for col, key in zip(bcols, ["上涨", "下跌", "涨停", "跌停", "平盘", "活跃度"]):
                col.metric(key, breadth.get(key, "—"))
            st.caption(f"统计时间：{breadth.get('统计日期', '未知')}（数据来自乐咕乐股网）")

        st.divider()
        up_col, down_col = st.columns(2)
        show_n = 30 if st.session_state.get("_show_more_limit_pool") else 10
        with up_col:
            st.markdown("**涨停股池**")
            try:
                up_pool = get_limit_pool("up", show_n)
                st.dataframe(up_pool, use_container_width=True, hide_index=True)
            except Exception as e:
                st.caption(f"获取失败：{e}")
        with down_col:
            st.markdown("**跌停股池**")
            try:
                down_pool = get_limit_pool("down", show_n)
                st.dataframe(down_pool, use_container_width=True, hide_index=True)
            except Exception as e:
                st.caption(f"获取失败：{e}")
        if not st.session_state.get("_show_more_limit_pool"):
            if st.button("显示更多（前30）", key="_more_limit_pool"):
                st.session_state["_show_more_limit_pool"] = True
                st.rerun()

with tab_watchlist:
    _email = st.session_state["user_email"]
    watched = get_watchlist(_email)

    if not watched:
        st.write("")
        _, mid_empty, _ = st.columns([1, 2, 1])
        with mid_empty:
            st.markdown(
                "<div style='text-align:center;color:#888;padding:20px 0 10px'>还没有关注任何股票</div>",
                unsafe_allow_html=True,
            )
            if st.button("＋ 新增自选股", type="primary", use_container_width=True, key="wl_empty_add"):
                st.session_state["_show_wl_add"] = True

    if watched or st.session_state.get("_show_wl_add"):
        with st.expander("＋ 新增自选股", expanded=not watched and st.session_state.get("_show_wl_add", False)):
            addcol1, addcol2, addcol3 = st.columns([2, 1, 1])
            add_query = addcol1.text_input("代码（如 600519 / 00700 / AAPL）", key="_wl_add_query")
            add_market_label = addcol2.selectbox("市场", ["A股", "港股", "美股"], key="_wl_add_market")
            if addcol3.button("添加", key="_wl_add_btn", use_container_width=True) and add_query:
                add_market_code = {"A股": "A", "港股": "HK", "美股": "US"}[add_market_label]
                q = add_query.strip()
                add_symbol = q.zfill(5) if add_market_code == "HK" else (q.upper() if add_market_code == "US" else q)
                try:
                    add_spot = get_stock_realtime(add_symbol, market=add_market_code)
                except Exception:
                    add_spot = {}
                if not add_spot or not add_spot.get("最新价"):
                    st.error(f"没查到「{add_symbol}」的行情，检查一下代码对不对。")
                else:
                    add_to_watchlist(_email, add_symbol, add_spot.get("名称", add_symbol), market=add_market_code)
                    st.session_state["_show_wl_add"] = False
                    st.rerun()

    if watched:
        header = st.columns([2, 1.2, 1.2, 1.2, 0.6])
        for col, label in zip(header, ["名称/代码", "最新价", "涨跌额", "涨跌幅", ""]):
            col.markdown(f"**{label}**")

        for item in watched:
            item_market = item.get("market", "A")
            try:
                wspot = get_stock_realtime(item["symbol"], market=item_market)
            except Exception:
                wspot = {}

            wc1, wc2, wc3, wc4, wc5 = st.columns([2, 1.2, 1.2, 1.2, 0.6])
            if wc1.button(f"{item['name']}（{item['symbol']}）", key=f"wl_open_{item['symbol']}"):
                st.session_state["_detail_symbol"] = item["symbol"]
                st.session_state["_detail_market"] = item_market
                st.session_state["_detail_name"] = item["name"]
                st.rerun()
            if wspot and wspot.get("最新价"):
                wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
                wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
                color = "#e02020" if wchange >= 0 else "#22a06b"
                wc2.markdown(f"<span style='color:{color}'>{wspot['最新价']:.2f}</span>", unsafe_allow_html=True)
                wc3.markdown(f"<span style='color:{color}'>{wchange:+.2f}</span>", unsafe_allow_html=True)
                wc4.markdown(f"<span style='color:{color}'>{wchange_pct:+.2f}%</span>", unsafe_allow_html=True)
            else:
                wc2.write("—")
                wc3.write("—")
                wc4.write("—")
            if wc5.button("✕", key=f"wl_remove_{item['symbol']}"):
                _confirm_delete_dialog(_email, item["symbol"], item["name"])

with tab_analyze:
    market = st.radio("市场", ["A股", "港股", "美股"], horizontal=True, key="_market_select")
    market_code = {"A股": "A", "港股": "HK", "美股": "US"}[market]
    placeholder = {"A股": "600519 / 贵州茅台", "港股": "00700（腾讯控股）", "美股": "AAPL（苹果）"}[market]

    if market_code != "A":
        st.caption("港股/美股目前只支持直接输代码，暂不支持按名称搜索；K线周期切换也暂时只有日K。")

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
        if market_code == "HK":
            # phase 1 先只支持直接输代码，不做名称搜索。格式先本地校验一遍，
            # 不对就直接拦掉，不发请求——之前"小米"这种中文名直接传下去，
            # 新浪那边返回空表，rename 时因为列不存在崩了个看不懂的 KeyError。
            if not re.match(r"^\d{4,5}$", query):
                st.error("港股代码应为4-5位数字（如 00700），暂不支持按名称搜索。")
                st.stop()
            symbol = query.zfill(5)
        elif market_code == "US":
            if not re.match(r"^[A-Za-z.]{1,6}$", query):
                st.error("美股代码应为英文字母（如 AAPL），暂不支持按名称搜索。")
                st.stop()
            symbol = query.upper()
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

            with st.spinner("拉取财务数据..."):
                try:
                    fin = get_financial_abstract(symbol, market=active_market)
                except Exception as e:
                    st.warning(f"财务数据获取失败（不影响后续分析）：{e}")
                    fin = None

            with st.spinner("拉取相关新闻..."):
                try:
                    stock_name = get_stock_name(symbol) if active_market == "A" else spot.get("名称", symbol)
                    news = get_stock_news(stock_name, limit=8)
                except Exception as e:
                    st.warning(f"新闻获取失败（不影响后续分析）：{e}")
                    news = None

            with st.spinner("拉取基准指数..."):
                try:
                    benchmark = get_benchmark_history(start, end, market=active_market)
                except Exception:
                    benchmark = None

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
                        add_to_watchlist(
                            st.session_state["user_email"], symbol, spot.get("名称", symbol), market=active_market
                        )
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

            benchmark_name = {"A": "沪深300", "HK": "恒生指数", "US": "标普500"}[active_market]
            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.markdown("**每日涨跌幅分布**")
                st.plotly_chart(build_return_histogram(hist), use_container_width=True)
            with chart_col2:
                if benchmark is not None and not benchmark.empty:
                    st.markdown(f"**对比{benchmark_name}（起点=100）**")
                    st.plotly_chart(
                        build_benchmark_comparison(hist, benchmark, benchmark_name=benchmark_name),
                        use_container_width=True,
                    )
                else:
                    st.markdown(f"**对比{benchmark_name}**")
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
