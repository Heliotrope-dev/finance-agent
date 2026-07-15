"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import os
import re
import streamlit as st
import streamlit.components.v1 as _cv1
from datetime import datetime, timedelta

from data_sources import (
    get_stock_history,
    get_stock_realtime,
    get_financial_abstract,
    get_stock_news,
    get_benchmark_history,
    get_stock_name,
    get_multi_index_snapshot,
    get_market_breadth,
    get_limit_pool,
    get_hk_famous_movers,
    get_us_famous_movers,
)
from analysis import cross_validate, summarize_financials, summarize_news, summarize_benchmark
from tracker import (
    log_analysis,
    add_to_watchlist, remove_from_watchlist, is_in_watchlist, get_watchlist,
)
from charts import build_candlestick, compute_stats, build_benchmark_comparison
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


_BENCHMARK_NAMES = {"A": "沪深300", "HK": "恒生指数", "US": "标普500"}


def _render_module(module: str, symbol: str, market: str, hist, spot: dict):
    """AI 模块按需加载：每个模块独立缓存，点开哪个才跑哪个的 AI 调用，不会一次性全跑。"""
    mod_key = f"_detail_mod_{symbol}_{market}_{module}"
    if mod_key not in st.session_state:
        with st.spinner("分析中..."):
            try:
                if module == "news":
                    stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
                    news = get_stock_news(stock_name, limit=8)
                    news_summary = (
                        "\n".join(f"- {r['新闻标题']}：{r['新闻内容'][:100]}" for _, r in news.iterrows())
                        if news is not None and not news.empty else "无相关新闻"
                    )
                    ai_text = summarize_news(symbol, news_summary)
                    st.session_state[mod_key] = {"news": news, "ai_text": ai_text}

                elif module == "financial":
                    fin = get_financial_abstract(symbol, market=market)
                    financial_summary = (
                        fin.head(10).to_string(index=False) if fin is not None and not fin.empty else "无可用数据"
                    )
                    ai_text = summarize_financials(symbol, financial_summary) if fin is not None and not fin.empty else ""
                    st.session_state[mod_key] = {"fin": fin, "ai_text": ai_text}

                elif module == "benchmark":
                    end = datetime.now().strftime("%Y%m%d")
                    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
                    benchmark = get_benchmark_history(start, end, market=market)
                    bm_name = _BENCHMARK_NAMES[market]
                    stock_pct = (float(hist.iloc[-1]["收盘"]) / float(hist.iloc[0]["收盘"]) - 1) * 100
                    bm_pct = (
                        (float(benchmark.iloc[-1]["收盘"]) / float(benchmark.iloc[0]["收盘"]) - 1) * 100
                        if benchmark is not None and not benchmark.empty else None
                    )
                    ai_text = summarize_benchmark(symbol, stock_pct, bm_name, bm_pct) if bm_pct is not None else ""
                    st.session_state[mod_key] = {"benchmark": benchmark, "bm_name": bm_name, "ai_text": ai_text}

                else:  # "cross" —— 完整交叉验证
                    end = datetime.now().strftime("%Y%m%d")
                    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
                    stats = compute_stats(hist)
                    history_summary = hist.tail(20).to_string(index=False)
                    if spot and spot.get("最新价"):
                        history_summary += (
                            f"\n\n实时行情快照：最新价{spot['最新价']}，今开{spot.get('今开')}，"
                            f"最高{spot.get('最高')}，最低{spot.get('最低')}，昨收{spot.get('昨收')}"
                        )
                    history_summary += "\n\n统计指标：" + "，".join(f"{k}={v}" for k, v in stats.items())

                    fin = get_financial_abstract(symbol, market=market)
                    financial_summary = (
                        fin.head(10).to_string(index=False) if fin is not None and not fin.empty else "无可用数据"
                    )
                    stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
                    news = get_stock_news(stock_name, limit=8)
                    news_summary = (
                        "\n".join(f"- {r['新闻标题']}：{r['新闻内容'][:100]}" for _, r in news.iterrows())
                        if news is not None and not news.empty else "无相关新闻"
                    )

                    ai_text = cross_validate(symbol, history_summary, financial_summary, news_summary)
                    current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
                    log_analysis(st.session_state["user_email"], symbol, float(current_price), ai_text)
                    st.session_state[mod_key] = {"ai_text": ai_text}
            except Exception as e:
                st.error(f"分析失败：{e}")
                return

    data = st.session_state[mod_key]

    if module == "news":
        st.markdown(data["ai_text"])
        if data["news"] is not None and not data["news"].empty:
            with st.expander("原始新闻列表"):
                st.dataframe(data["news"], use_container_width=True)
    elif module == "financial":
        if data["fin"] is not None and not data["fin"].empty:
            st.dataframe(data["fin"], use_container_width=True, hide_index=True)
            if data["ai_text"]:
                st.markdown(data["ai_text"])
        else:
            st.caption("暂无财务数据。")
    elif module == "benchmark":
        if data["benchmark"] is not None and not data["benchmark"].empty:
            st.plotly_chart(
                build_benchmark_comparison(hist, data["benchmark"], benchmark_name=data["bm_name"]),
                use_container_width=True,
            )
            st.markdown(data["ai_text"])
        else:
            st.caption("基准数据暂时获取不到。")
    else:
        st.markdown(data["ai_text"])


def _render_stock_detail(symbol: str, market: str, name: str):
    if st.button("返回自选股"):
        for k in ("_detail_symbol", "_detail_market", "_detail_name", "_detail_module"):
            st.session_state.pop(k, None)
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

    # 快路径：只拉行情+实时价，不碰AI，先把图画出来
    core_key = f"_detail_core_{symbol}_{market}"
    if core_key not in st.session_state:
        with st.spinner("加载行情..."):
            try:
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
                hist = get_stock_history(symbol, start, end, market=market)
                if hist is None or hist.empty:
                    st.error("没有获取到行情数据，检查一下股票代码是否正确。")
                    return
                try:
                    spot = get_stock_realtime(symbol, market=market)
                except Exception:
                    spot = {}
                st.session_state[core_key] = {"hist": hist, "spot": spot}
            except Exception as e:
                st.error(f"加载失败：{e}")
                return

    core = st.session_state[core_key]
    hist, spot = core["hist"], core["spot"]

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

        _watched_now = is_in_watchlist(st.session_state["user_email"], symbol)
        if _watched_now:
            if st.button("移除自选", key="wl_toggle"):
                remove_from_watchlist(st.session_state["user_email"], symbol)
                st.rerun()
        else:
            if st.button("加入自选", key="wl_toggle"):
                add_to_watchlist(st.session_state["user_email"], symbol, spot.get("名称", symbol), market=market)
                st.rerun()

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
    st.subheader("深入分析")
    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    module_labels = [("news", "最新资讯"), ("financial", "财务摘要"), ("benchmark", "对比大盘"), ("cross", "数据分析")]
    for col, (mod_key, mod_label) in zip((mcol1, mcol2, mcol3, mcol4), module_labels):
        if col.button(mod_label, key=f"mod_btn_{mod_key}", use_container_width=True):
            st.session_state["_detail_module"] = mod_key

    active_module = st.session_state.get("_detail_module")
    if active_module:
        with st.container(border=True):
            _render_module(active_module, symbol, market, hist, spot)


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
        <span style='color:#fff;font-size:1.3rem;font-weight:700;letter-spacing:.02em'>Invest Agent</span>
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
    "快速搜索代码，直接进详情页",
    value="", key="_quick_search", placeholder="600519 / 00700 / AAPL",
)
if bcol.button("搜索", key="_quick_search_btn", use_container_width=True) and quick_query:
    q = quick_query.strip()
    detected = _auto_detect_market(q)
    if detected is None:
        st.warning("这个格式看着不像代码——快速搜索目前只支持直接输代码。")
    else:
        sym = q.zfill(5) if detected == "HK" else (q.upper() if detected == "US" else q)
        st.session_state["_detail_symbol"] = sym
        st.session_state["_detail_market"] = detected
        st.session_state["_detail_name"] = sym
        st.rerun()

tab_market, tab_watchlist = st.tabs(["行情", "自选股"])

def _style_movers_table(df):
    """涨跌幅/涨跌额红涨绿跌上色，数字统一两位小数，涨跌幅带%号——表格别一片黑。"""
    if df is None or df.empty:
        return df

    def _color(v):
        try:
            v = float(v)
        except Exception:
            return ""
        return f"color: {'#e02020' if v >= 0 else '#22a06b'}"

    fmt = {}
    if "最新价" in df.columns:
        fmt["最新价"] = "{:.2f}"
    if "涨跌额" in df.columns:
        fmt["涨跌额"] = "{:+.2f}"
    if "涨跌幅" in df.columns:
        fmt["涨跌幅"] = "{:+.2f}%"
    if "换手率" in df.columns:
        fmt["换手率"] = "{:.2f}%"

    color_cols = [c for c in ("涨跌额", "涨跌幅") if c in df.columns]
    return df.style.format(fmt).map(_color, subset=color_cols)


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

    if mkt_code == "A":
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
                st.dataframe(_style_movers_table(up_pool), use_container_width=True, hide_index=True)
            except Exception as e:
                st.caption(f"获取失败：{e}")
        with down_col:
            st.markdown("**跌停股池**")
            try:
                down_pool = get_limit_pool("down", show_n)
                st.dataframe(_style_movers_table(down_pool), use_container_width=True, hide_index=True)
            except Exception as e:
                st.caption(f"获取失败：{e}")
        if not st.session_state.get("_show_more_limit_pool"):
            if st.button("显示更多（前30）", key="_more_limit_pool"):
                st.session_state["_show_more_limit_pool"] = True
                st.rerun()

    elif mkt_code == "HK":
        st.caption("港股没有涨跌停限制制度，这里改成知名股涨跌幅榜。")
        try:
            with st.spinner("加载中（第一次会慢一些）..."):
                hk_movers = get_hk_famous_movers(15)
            st.dataframe(_style_movers_table(hk_movers), use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"获取失败：{e}")

    else:
        st.caption("美股同样没有涨跌停制度，这里也是知名股涨跌幅榜。")
        try:
            us_movers = get_us_famous_movers(15)
            st.dataframe(_style_movers_table(us_movers), use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"获取失败：{e}")

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
            if st.button("新增自选股", type="primary", use_container_width=True, key="wl_empty_add"):
                st.session_state["_show_wl_add"] = True

    if watched or st.session_state.get("_show_wl_add"):
        with st.expander("新增自选股", expanded=not watched and st.session_state.get("_show_wl_add", False)):
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
        header = st.columns([4, 0.5])
        header[0].markdown("**名称/代码/最新价/涨跌**")

        for item in watched:
            item_market = item.get("market", "A")
            try:
                wspot = get_stock_realtime(item["symbol"], market=item_market)
            except Exception:
                wspot = {}

            row_col, del_col = st.columns([4, 0.5])
            if wspot and wspot.get("最新价"):
                wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
                wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
                arrow = "▲" if wchange >= 0 else "▼"
                row_label = (
                    f"{item['name']}（{item['symbol']}）　"
                    f"{wspot['最新价']:.2f}　{arrow} {wchange:+.2f} ({wchange_pct:+.2f}%)"
                )
            else:
                row_label = f"{item['name']}（{item['symbol']}）　—"

            if row_col.button(row_label, key=f"wl_open_{item['symbol']}", use_container_width=True):
                st.session_state["_detail_symbol"] = item["symbol"]
                st.session_state["_detail_market"] = item_market
                st.session_state["_detail_name"] = item["name"]
                st.rerun()
            if del_col.button("×", key=f"wl_remove_{item['symbol']}", use_container_width=True):
                _confirm_delete_dialog(_email, item["symbol"], item["name"])
