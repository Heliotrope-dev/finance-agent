"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import html
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
import pandas as pd
import streamlit as st
import streamlit.components.v1 as _cv1
from datetime import datetime, timedelta, timezone

from data_sources import (
    cn_now,
    _MULTI_INDICES,
    get_stock_kline_futu,
    get_stock_intraday_futu,
    get_stock_intraday_a,
    get_index_history,
    get_index_intraday_futu,
    get_index_intraday_a,
    get_stock_history,
    get_stock_realtime,
    check_stock_valid,
    get_financial_abstract,
    get_stock_news,
    get_stock_notices,
    get_benchmark_history,
    get_stock_name,
    get_index_news,
    get_futu_news,
    get_market_news,
    get_hot_market_news,
    get_global_indices,
    search_stock_by_name,
    get_multi_index_snapshot,
    get_multi_index_snapshot_slow,
    load_home_map_cache,
    get_market_breadth,
    get_limit_pool,
    get_hk_famous_movers,
    get_index_top_movers,
    get_southbound_flow,
    get_us_famous_movers,
    get_hot_sectors,
    get_sector_constituents,
    get_hstech_constituents,
    resolve_symbol_by_name,
    detect_symbol_candidates,
    get_data_source_health,
    to_cny,
    _A_FUND_NAME_MAP,
)
from analysis import (
    cross_validate, summarize_financials, summarize_news, summarize_index_news, summarize_benchmark,
    extract_verdict, analyze_index, summarize_overall, extract_score,
)
from assistant import build_context as build_assistant_context, stream_reply as stream_assistant_reply
from tracker import (
    log_analysis, get_history, get_due_for_review, record_review, get_accuracy_stats, record_overall_score,
    get_accuracy_trend, get_daily_accuracy, add_watch_only, is_position_tracked,
    add_search_history, get_search_history, get_latest_leaderboard, get_advice_accuracy, get_score_band_backtest,
    get_position_advice, get_positions, upsert_position, reduce_position, delete_position,
    get_latest_portfolio_advice, get_max_capital, set_max_capital,
    get_sim_agent_enabled, set_sim_agent_enabled, get_simulated_orders, get_sim_agent_runs, get_sim_virtual_cash,
    get_equity_snapshots, get_period_pnl,
)
import sim_trader
import sim_agent
from charts import (
    build_candlestick, build_intraday_line, compute_stats, compute_technical_signal, compute_realtime_signal,
    build_benchmark_comparison, build_return_histogram, build_multi_comparison, build_position_donut,
    build_sim_equity_curve,
)
from auth import (
    _check_user, _register_user, _create_token, _validate_token,
    _invalidate_token, _hash_pw, _user_exists,
)
from theme import UP_COLOR, DOWN_COLOR, NEUTRAL_COLOR

for _k in ("SUPABASE_URL", "SUPABASE_KEY", "ADVISOR_EMAIL"):
    if _k not in os.environ:
        try:
            os.environ[_k] = st.secrets[_k]
        except Exception:
            pass

st.set_page_config(page_title="Invest Agent", layout="wide")

# ── 主题（移动端适配 + 涨跌红绿色统一）────────────────────────────────────────
# 这里原来还有一版深色模式，用户实测反馈"按了跟没按一样，很烦"——撤掉了，
# 只留CSS变量本身（数值固定为浅色，不再有深色分支），移动端适配和涨跌色号
# 统一这两块跟深色模式无关，继续保留。
_FA_BASE_CSS = """
<style>
:root {
    --fa-bg:      #F8F8FA;
    --fa-surface: #FFFFFF;
    --fa-border:  #E4E6EA;
    --fa-text:    #0F172A;
    --fa-muted:   #6E6E82;
}

html, body { background: var(--fa-bg) !important; }
.stApp, [data-testid="stAppViewContainer"],
[data-testid="stMain"], [data-testid="stMainBlockContainer"],
section.main, .main, .block-container,
[data-testid="stBottom"], .stBottom,
[data-testid="stBottomBlockContainer"],
footer {
    background: var(--fa-bg) !important;
}
header[data-testid="stHeader"] { background: var(--fa-bg) !important; box-shadow: none !important; }
[data-testid="stHorizontalBlock"] { background: transparent !important; }
[data-testid="stColumn"] { background: transparent !important; }
[data-testid="stElementContainer"] { background: transparent !important; }
[data-testid="stVerticalBlockBorderWrapper"] { background: var(--fa-surface) !important; border-color: var(--fa-border) !important; }
[data-testid="stExpander"] { background: var(--fa-surface) !important; border: 1px solid var(--fa-border) !important; }
hr { border-color: var(--fa-border) !important; }

/* 下面这几条只作用于 Streamlit 自己生成的文字节点（radio标签、caption、
   纯文本markdown的<p>），不会碰到我们自己写的带inline color的<div>/<span>
   （那些本来就没有匹配到<p>标签选择器）。*/
[data-testid="stMarkdownContainer"] p { color: var(--fa-text) !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * { color: var(--fa-muted) !important; }
[data-testid="stWidgetLabel"] p { color: var(--fa-text) !important; }

.stButton button {
    background: var(--fa-surface) !important; border: 1px solid var(--fa-border) !important;
    color: var(--fa-text) !important;
}
div[data-testid="stButtonGroup"] button,
div[data-testid="stButtonGroup"] [role="radio"] {
    background-color: var(--fa-surface) !important; border: 1px solid var(--fa-border) !important;
    color: var(--fa-muted) !important;
}
div[data-testid="stButtonGroup"] button[aria-checked="true"],
div[data-testid="stButtonGroup"] [aria-checked="true"] {
    background-color: #e02020 !important; border-color: #e02020 !important; color: #fff !important;
}
div[data-testid="stButtonGroup"] p, div[data-testid="stButtonGroup"] span { color: inherit !important; }

/* ── 移动端（窄屏）适配 ──────────────────────────────────────────────────────
   之前这个项目完全没有@media适配——大量信息密集的卡片（涨跌停池/核心股/
   成分股卡片、指数快照表头、持仓行、热门板块宫格）都是手写flex比例布局，
   不会跟着窄屏自动折行，手机打开容易挤出文字截断、数字错位。这里不重做
   信息架构，只让这几处关键卡片在窄屏下改成允许换行/压缩字号，同时保留
   数据本身的可读性。 */
@media (max-width: 768px) {
    .fa-flex-row { flex-wrap: wrap !important; }
    .fa-flex-row > div { flex: 1 1 auto !important; }
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
    .stButton button { font-size: 0.8rem !important; padding: 6px 10px !important; }
    div[data-testid="stButtonGroup"] button,
    div[data-testid="stButtonGroup"] [role="radio"] { font-size: 0.75rem !important; padding: 3px 8px !important; }
}
</style>
"""

# _FA_BASE_CSS 是个大的三引号CSS字符串，里面有大量`{}`（CSS规则本身），
# 不适合直接转成f-string（要逐个转义花括号，容易改错）。这里选用最小风险的
# 办法达到同样的目的——选中态用的品牌红是字面量"#e02020"，跟theme.py里
# UP_COLOR是同一个值，用.replace()把它换成UP_COLOR变量值，日后改
# theme.py时这里能跟着变，不用再在两个文件里各改一次。
st.markdown(_FA_BASE_CSS.replace("#e02020", UP_COLOR), unsafe_allow_html=True)

# ── 加载中遮罩 ────────────────────────────────────────────────────────────────
# 这个app所有页面跳转（列表点进详情页、返回列表、切换市场等）走的都是真实的
# <a href="?...">整页导航，浏览器会先展示上一个页面最后一帧，再等Streamlit
# 把新页面整个渲染完替换上去——中间这段空档用户看到的是"新旧内容短暂重叠
# 闪一下"（math-agent那边遇到过同一个问题，已经用一层遮罩盖住这段过渡状态，
# 这里原样搬过来）。遮罩在Streamlit的状态组件不再显示"Running"时淡出移除，
# 不依赖固定延迟时间去猜多久算加载完。
# 总是渲染（不放在登录判断后面），让这个组件iframe每次页面跳转都重新挂载
# 执行一遍——这本身就是遮罩能在新页面重新出现的关键。
#
# 光靠下面这个components.v1.html()注入的遮罩div还不够彻底——它本身是在一个
# 独立iframe里异步加载/执行的，跟页面主体内容的渲染是两条不同步的时间线，
# 实测iframe加载/执行有自己的延迟，主体内容有时候会抢先画出来，遮罩后到，
# 反而先看到一下没盖住的内容再被盖住，等于制造了另一次"闪烁"。这里先用
# st.markdown（原生渲染在Streamlit自己的DOM里，跟页面主体是同一条渲染
# 时间线，没有iframe那层异步延迟）立刻把主体内容透明度设成0，用!important
# 保证优先级最高；下面iframe里的JS判断真正加载完成后，直接把这个<style>
# 标签整个删掉（不是覆盖，删除比设置内联样式更可靠——!important的规则内联
# style优先级压不过它，必须真删除这条规则本身）。
st.markdown(
    "<style id='_fa_loader_css'>[data-testid=\"stAppViewContainer\"]{opacity:0!important}</style>",
    unsafe_allow_html=True,
)
# 之前这个遮罩只在"真实整页导航"（点<a href>链接）时生效，切"行情/持仓"
# 这种纯靠st.radio触发的内部rerun时完全不出现——用户反馈"持仓页面还是有
# 上一页残留"，排查发现st.components.v1.html()传入的HTML/JS内容如果两次
# rerun之间字节完全相同，Streamlit前端不会重新挂载这个iframe（判定为"没变化"
# 直接跳过），脚本也就不会重新执行，遮罩自然不会在内部rerun时重新出现。
# 之前每次整页导航时内容恰好每次都是这同一份固定字符串，只有"完整刷新文档"
# 这种场景会绕开这层去重（浏览器整个重新加载，不存在"和上次一样跳过"这回事），
# 这就是"整页导航生效、内部切换不生效"这个差异的真正原因。改成把当前时间戳
# 埋进一个不可见的HTML注释里，保证每次rerun这段内容字符串都不一样，
# Streamlit就没法把它当成"没变化"跳过，每次rerun都会重新挂载、重新执行。
_cv1.html("""
<!-- __FA_LOADER_NONCE__ -->
<script>
(function() {
try {
    var doc = window.parent.document;
    var ov = doc.getElementById('_fa_loader');
    if (!ov) {
        ov = doc.createElement('div');
        ov.id = '_fa_loader';
        ov.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:14px;transition:opacity 0.35s;background:#F8F8FA';
        ov.innerHTML = '<div style="width:36px;height:36px;border:3px solid #e0e0e8;border-top-color:#e02020;border-radius:50%;animation:_fa_spin 0.8s linear infinite"></div><div style="font-size:0.9rem;color:#aaa;font-family:Inter,sans-serif;letter-spacing:.03em;margin-top:4px">加载中…</div><style>@keyframes _fa_spin{to{transform:rotate(360deg)}}</style>';
        doc.body.appendChild(ov);
    }
    function _fa_removeHideCss() {
        var css = doc.getElementById('_fa_loader_css');
        if (css) css.remove();
    }
    var _ovTries = 0;
    var _ovIv = setInterval(function() {
        _ovTries++;
        if (_ovTries > 40) { clearInterval(_ovIv); _fa_removeHideCss(); if (ov) { ov.style.opacity='0'; setTimeout(function(){ if (ov) ov.remove(); },350); } return; }
        var status = doc.querySelector('[data-testid="stStatusWidget"]');
        var running = status && (status.textContent || '').indexOf('Running') !== -1;
        var app = doc.querySelector('[data-testid="stAppViewContainer"]');
        if (app && !running) {
            clearInterval(_ovIv);
            _fa_removeHideCss();
            setTimeout(function(){ if (ov) { ov.style.opacity='0'; setTimeout(function(){ if (ov) ov.remove(); },350); } }, 250);
        }
    }, 150);
} catch(e2) {}
})();
</script>
""".replace("__FA_LOADER_NONCE__", datetime.now().isoformat()).replace("#e02020", UP_COLOR), height=0)


def _show_login_page():
    st.markdown(
        "<div style='text-align:center;padding:60px 0 24px'>"
        "<div style='font-size:1.5rem;font-weight:600;margin:8px 0 4px'>Invest Agent</div>"
        "<div style='font-size:0.85rem;color:var(--fa-muted)'>行情 + 财务 + 新闻交叉验证</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        # 2026-08-25：游客模式入口按用户要求关掉了，安全优先——下面
        # guest_mode 相关的分支逻辑（第367行的登录墙判断、持仓/关注/AI分析
        # 历史那几处 if not logged_in 的降级展示）都还留着，只是没有任何
        # 入口能把 guest_mode 置成 True 了，全站恢复成"不登录什么都看不到"。
        # 如果以后要重新开放访客浏览，把下面这个按钮加回来就行，不用重写
        # 其它地方的判断逻辑。
        tab_l, tab_r = st.tabs(["登录", "注册"])
        with tab_l:
            _em = st.text_input("邮箱", key="li_email", placeholder="your@email.com")
            _pw = st.text_input("密码", type="password", key="li_pw")
            if st.button("登录", type="primary", use_container_width=True, key="do_login"):
                _ok, _msg = _check_user(_em, _pw)
                if _ok:
                    _tok = _create_token(_em)
                    # token 不写进 st.query_params——7天免登录凭证常驻在可见地址栏里
                    # 会被Nginx access log/浏览器历史明文留存。当次会话靠session_state
                    # 就够了，后续每个卡片链接会通过_auth_qs()从session_state重新
                    # 拼一份，不依赖当前地址栏残留的这一份。
                    st.session_state["logged_in"] = True
                    st.session_state["user_email"] = _em
                    st.session_state["_token"] = _tok
                    # localStorage继续写一份留作兜底/legacy；Cookie才是"下次
                    # 打开自动登录"真正依赖的那条路径——见下面"记住登录"那段
                    # 注释，st.context.cookies能在服务端直接读到它，不需要
                    # 再靠iframe强制刷新页面（那条路已经被浏览器拦掉了）。
                    _cv1.html(
                        f"""<script>try{{
                        window.parent.localStorage.setItem("fa_auth_tok","{_tok}");
                        var d = new Date(); d.setTime(d.getTime() + 7*24*60*60*1000);
                        window.parent.document.cookie = "fa_auth_tok={_tok}; expires=" + d.toUTCString() + "; path=/; SameSite=Lax; Secure";
                        }}catch(e){{}}</script>""",
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


# ── 记住登录（Cookie，7天）───────────────────────────────────────────────
# 之前这里是"读localStorage→塞进URL→800ms后用iframe强制刷新父页面"那一套，
# 2026-08-28实测在当前Chrome版本下彻底失效：iframe（components.v1.html创建
# 的沙盒iframe）读写window.parent.localStorage没问题，但
# window.parent.location.replace(...)会被浏览器当成"沙盒iframe试图导航
# 顶层页面"直接拦截，控制台报SecurityError——token其实一直是有效的（拿
# 同一个token单独调用_validate_token()验证过没问题），只是浏览器根本没
# 机会把它带着发一次新请求给服务器，页面永远卡在登录页。
#
# 改用Cookie：写入不需要导航权限，只需要access parent origin（跟
# localStorage是同一类同源访问权限，已经在用了，不受上面那条限制）；
# Cookie会由浏览器自动带在*每一次*请求里（包括第一次打开页面的那次
# HTTP请求），服务端用Streamlit自带的st.context.cookies直接读，完全
# 不需要JS参与"读取"这一步、也不需要强制刷新——从根上绕开了这个安全
# 限制，而且比原来的方案更快（省掉了800ms的人为延迟和一次额外的整页
# 重新加载）。旧的_auth query-param路径保留作为兜底（分享链接/禁用
# Cookie的场景）。
if not st.session_state.get("logged_in"):
    _cookie_token = st.context.cookies.get("fa_auth_tok")
    if _cookie_token:
        _auto_email = _validate_token(_cookie_token)
        if _auto_email:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = _auto_email
            st.session_state["_token"] = _cookie_token

_stored_token = st.query_params.get("_auth", "") or ""
if _stored_token and not st.session_state.get("logged_in"):
    _auto_email = _validate_token(_stored_token)
    if _auto_email:
        st.session_state["logged_in"] = True
        st.session_state["user_email"] = _auto_email
        st.session_state["_token"] = _stored_token
        _cv1.html(
            f"""<script>
            try {{
                var tok = {_stored_token!r};
                var d = new Date(); d.setTime(d.getTime() + 7*24*60*60*1000);
                window.parent.document.cookie = "fa_auth_tok=" + encodeURIComponent(tok) + "; expires=" + d.toUTCString() + "; path=/; SameSite=Lax; Secure";
                var url = new URL(window.parent.location.href);
                if (url.searchParams.get('_auth')) {{
                    url.searchParams.delete('_auth');
                    window.parent.history.replaceState(null, '', url.toString());
                }}
            }} catch(e) {{}}
            </script>""",
            height=1,
        )
    else:
        try:
            del st.query_params["_auth"]
        except Exception:
            pass
        _cv1.html(
            '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");window.parent.document.cookie="fa_auth_tok=; max-age=0; path=/";}catch(e){}</script>',
            height=1,
        )

if not st.session_state.get("logged_in") and not st.session_state.get("guest_mode"):
    _show_login_page()
    st.stop()

# 持仓列表整卡片可点——之前试过CSS覆盖层、JS找DOM绑事件两种方案，
# 在真实浏览器里都点不动（大概率是这两种方案都依赖对Streamlit内部渲染结构
# 的猜测，版本一变或者猜错了就失效）。改成最朴素可靠的办法：卡片内容整个
# 包在一个真正的<a href="?...">链接里，点击就是标准的浏览器导航行为，
# 不依赖任何JS/CSS去猜内部结构。这里在页面渲染最开始就检查URL参数，
# 有就直接跳转详情页并清掉参数。
if st.query_params.get("open_symbol"):
    st.session_state["_detail_symbol"] = st.query_params["open_symbol"]
    st.session_state["_detail_market"] = st.query_params.get("open_market", "A")
    st.session_state["_detail_name"] = st.query_params.get("open_name", st.query_params["open_symbol"])
    # 从持仓卡片点进来的，"返回"要能回到持仓分区，不是每次都弹回默认的
    # "行情"分区——整页导航会把session_state清空，"_active_section"记不住
    # 是从哪个分区点进来的，得靠这个参数显式带过来。
    if st.query_params.get("open_from") == "pos":
        st.session_state["_active_section"] = "持仓"
    st.query_params.clear()
    st.rerun()
if st.query_params.get("open_index_code"):
    st.session_state["_index_detail_code"] = st.query_params["open_index_code"]
    st.session_state["_index_detail_market"] = st.query_params.get("open_index_market", "A")
    st.session_state["_index_detail_name"] = st.query_params.get("open_index_name", "")
    st.query_params.clear()
    st.rerun()
if st.query_params.get("open_sector"):
    st.session_state["_sector_detail_name"] = st.query_params["open_sector"]
    st.session_state["_sector_detail_market"] = st.query_params.get("open_sector_market", "A")
    st.query_params.clear()
    st.rerun()


_BENCHMARK_NAMES = {"A": "沪深300", "HK": "恒生指数", "US": "标普500"}


def _fetch_news_items(keyword: str, symbol: str | None, market: str) -> tuple:
    """页面展示和AI分析要用同一份新闻源，不然会出现页面上一手资讯明明有
    （比如寒武纪的官方公告），AI资讯解读那栏却说"没有找到相关新闻"这种自相
    矛盾的情况。优先级：A股官方公告（get_stock_notices，监管强制披露，永远
    免费）> 富途资讯搜索（get_futu_news，真按关键词匹配，港股/美股/A股通吃，
    链接免费可读）> 财新关键词匹配（get_stock_news，兜底，有付费墙）。
    返回 (DataFrame, 来源标记："notices"/"futu"/"caixin")。
    """
    if market == "A" and symbol:
        try:
            notices = get_stock_notices(symbol)
        except Exception:
            notices = None
        if notices is not None and not notices.empty:
            return notices, "notices"

    try:
        futu_news = get_futu_news(keyword, max_count=8)
    except Exception:
        futu_news = None
    if futu_news is not None and not futu_news.empty:
        return futu_news, "futu"

    try:
        news = get_stock_news(keyword, limit=8)
    except Exception:
        news = None
    return news, "caixin"


def _build_sparkline_svg(values: list, color: str, width: int = 60, height: int = 26) -> str:
    """持仓行情列表里那种"一眼看趋势"的迷你走势图——不用plotly（每行一个太重，
    列表长了会很卡），纯手算折线点位吐一段内联SVG，跟长桥/同花顺那种列表里的
    小图一个意思。
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return "<span style='color:var(--fa-muted);font-size:0.7rem'>--</span>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or (abs(lo) * 0.01 or 1)
    n = len(vals)
    pts = [f"{(i / (n - 1) * width):.1f},{(height - (v - lo) / rng * height):.1f}" for i, v in enumerate(vals)]
    points_str = " ".join(pts)
    return (
        f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' style='display:block'>"
        f"<polyline points='{points_str}' fill='none' stroke='{color}' stroke-width='1.6' "
        f"stroke-linejoin='round' stroke-linecap='round'/></svg>"
    )


def _fetch_sparkline_closes(symbol: str, market: str, days: int = 20) -> list:
    """持仓迷你图用的近期收盘价——直接复用已有的历史行情接口（带缓存，5分钟
    过期），不新开专门的接口，多取一倍自然日天数换算成够用的交易日数量。
    """
    try:
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
        hist = get_stock_history(symbol, start, end, market=market)
        if hist is None or hist.empty:
            return []
        return hist["收盘"].astype(float).tail(days).tolist()
    except Exception:
        return []


def _fmt_turnover(v) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.0f}"


def _run_concurrent_with_deadline(items: list, fn, timeout: float, max_workers: int = 8) -> dict:
    """一批任务并发跑、给整批一个统一的deadline（从submit那一刻算起，不是从"轮到
    检查它"那一刻）——持仓列表/组合对比/回看补录三处原来各自手写一份几乎一样的
    "ThreadPoolExecutor + concurrent.futures.wait(timeout) + shutdown(wait=False)"，
    抽成这一个共享helper，不用再维护三份重复代码。

    统一deadline是刻意的：三处原来都各自踩过同一个坑——按"逐个future.result(timeout=N)"
    实现时，每个future的超时窗口是从"轮到检查它"那一刻才开始计时，排在后面检查的
    future会绕过预期上限（前面几个如果很快返回，轮到最后一个时它已经跑了好几秒、
    检查时又给它一次全新的N秒，总耗时照样能远超预期，没起到限制作用）。改用
    concurrent.futures.wait() 给整批futures一个从submit那一刻算起的统一截止时间，
    时间到了还没完成的直接跳过，才是真正把整批总耗时卡在timeout附近。

    cancel_futures=True：shutdown时把"提交了但线程池还没轮到开始跑"的future取消掉，
    减少浪费。已经在执行中、卡在网络请求里的线程没法从Python这层强制中断——deadline
    到了之后它们会在后台自己跑完/超时再自然退出，不阻塞这个函数返回。这不是"无限
    堆积"的线程泄漏：各处传进来的fn目前都是走内部已有重试/超时保护的数据源接口
    （get_stock_realtime/get_stock_history等），最终都会自己返回，只是可能比这里
    的deadline稍晚，不是真正无界悬挂；如果以后有调用点传入没有自带超时的fn，这条
    保证就不成立了，调用方要自己确认。

    返回：{items里的位置索引 -> fn(item)的返回值}，deadline内没完成/抛异常的位置
    不会出现在字典里，调用方按"i in results"判断这一项是不是被跳过了。用位置索引
    而不是item本身做key，是因为item可能是dict这类不可hash的类型。
    """
    results: dict[int, object] = {}
    if not items:
        return results
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(items)))
    futures = {ex.submit(fn, item): i for i, item in enumerate(items)}
    done, _not_done = _futures_wait(list(futures.keys()), timeout=timeout)
    for fut in done:
        try:
            results[futures[fut]] = fut.result()
        except Exception:
            pass
    ex.shutdown(wait=False, cancel_futures=True)
    return results


def _auth_qs() -> str:
    """卡片链接的<a href="?...">会触发真正的整页导航（不是Streamlit的软rerun），
    URL的query string会被整个替换掉——如果不把登录用的_auth token也带上，
    跳转后session_state被清空，会先闪一下登录页，等localStorage自动登录的
    JS再刷新一次才恢复，两次整页刷新叠加体验很差。这里统一把当前token拼进
    每个卡片链接，跳转就是一步到位，不会闪登录页。
    """
    token = st.session_state.get("_token", "")
    return f"&_auth={urllib.parse.quote(token)}" if token else ""


def _resolve_add_symbol(q: str, market_code: str) -> str | None:
    """"新增持仓"用的名称→代码解析，A股之前一直漏了——resolve_symbol_by_name
    只支持HK/US（内部的知名股名单和Futu模糊搜索都没有A股这块），A股market
    传进去必然返回None，退化成直接把"茅台"这种中文名当代码用，当然查不到。
    这里A股单独先走search_stock_by_name（BaoStock按名称模糊匹配，真支持A股），
    查不到再试_A_FUND_NAME_MAP——BaoStock按名称搜索只覆盖个股不含ETF/基金，
    "沪深300ETF"这类名字搜不到，用户反馈过这个问题。
    """
    q = q.strip()
    if market_code == "A":
        try:
            matches = search_stock_by_name(q)
        except Exception:
            matches = []
        if matches:
            return matches[0]["code"]
        fund_code = _A_FUND_NAME_MAP.get(q.lower())
        if fund_code:
            return fund_code
        return q if re.match(r"^\d{6}$", q) else None
    by_name = resolve_symbol_by_name(q, market_code)
    if by_name:
        return by_name
    return q.zfill(5) if market_code == "HK" else q.upper()


@st.cache_data(ttl=60, show_spinner=False)
def _get_hot_stock_names() -> set:
    """"新闻标红"用的交叉比对集合——不是新闻本身的热度分数（免费数据源里
    没有真实的新闻热度数据，实测过NewsAPI的totalResults当代理指标也不可靠：
    中文公司名基本查不到，英文名又被无关内容严重干扰，"Apple"两天内2000多条
    大多是无关内容，不是真的"苹果公司今天很火"），改用我们自己已有的、真实
    可靠的当日异动数据做交叉验证：新闻提到的公司如果今天正好在涨停/跌停
    股池、或者港股/美股核心股里涨跌幅超过一定幅度，就认为是"今天值得关注"，
    标题标红；不是新闻热度，是"新闻提到的标的今天股价表现是否异常"，如实
    是这个语义，不是编一个假热度。
    """
    names = set()
    try:
        up = get_limit_pool("up", limit=30)
        if up is not None and not up.empty:
            names.update(up["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        down = get_limit_pool("down", limit=30)
        if down is not None and not down.empty:
            names.update(down["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        hk = get_hk_famous_movers(15)
        if hk is not None and not hk.empty:
            names.update(hk[hk["涨跌幅"].abs() > 3]["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        us = get_us_famous_movers(15)
        if us is not None and not us.empty:
            names.update(us[us["涨跌幅"].abs() > 3]["名称"].dropna().tolist())
    except Exception:
        pass
    return names


def _news_title_color(title: str, hot_names: set) -> str:
    """标题里提到了今天有异动的公司名就标红，否则跟随默认文字色。"""
    if any(name and name in title for name in hot_names):
        return UP_COLOR
    return "var(--fa-text)"


def _esc(s) -> str:
    """新闻标题/URL 等外部抓取内容拼进 HTML 前统一转义，防 XSS。"""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


_CN_TZ = timezone(timedelta(hours=8))


def _to_cn_dt(iso_str: str) -> datetime | None:
    """tracker.py里run_at/created_at统一存的是datetime.now(timezone.utc).isoformat()
    ——数据库存UTC是对的(不该跟着服务器本地时区存，见data_sources.cn_now()
    同一个教训)，但界面上给用户看的必须是北京时间，2026-09-01用户反馈AI
    模拟盘的收益曲线/决策记录时间戳显示的是UTC（比如凌晨4点多），跟他自己
    的时区对不上，这里统一转换。返回datetime对象而不是格式化字符串——
    图表x轴需要真正的时间类型，不能传字符串给Plotly猜。
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CN_TZ)
    except (ValueError, TypeError):
        return None


def _to_cn_time_str(iso_str: str) -> str:
    dt = _to_cn_dt(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else (iso_str or "")[:19].replace("T", " ")


def _chat_bubble(role: str, text: str) -> str:
    """AI咨询浮窗用的聊天气泡——用户明确要求跟主流AI聊天产品一致的经典
    样式：用户消息靠右、蓝底白字；AI回复靠左、白底黑字，不带任何头像图标
    （st.chat_message自带的默认头像是卡通小图标，跟"不许有emoji/装饰图标"
    的要求冲突，改成纯HTML拼这个气泡，不用chat_message）。
    用户消息走_esc转义（用户输入不可信，防XSS）；AI回复不转义、原样走
    markdown渲染——AI输出的**加粗**这类格式化要保留，且是本模块自己生成的
    内容，不是外部抓取的不可信文本。
    """
    is_user = role == "user"
    align = "flex-end" if is_user else "flex-start"
    bg = "#2563eb" if is_user else "#f0f1f3"
    color = "#fff" if is_user else "#1a1a1a"
    body = _esc(text) if is_user else text
    return (
        f"<div style='display:flex;justify-content:{align};margin:6px 2px'>"
        f"<div style='max-width:82%;padding:8px 13px;border-radius:16px;background:{bg};"
        f"color:{color};font-size:0.88rem;line-height:1.5;white-space:pre-wrap;word-break:break-word'>"
        f"{body}</div></div>"
    )


def _typing_indicator_html() -> str:
    """AI气泡里的"正在想"三点跳动动画——2026-08-30新增。用户反馈发消息后
    在拿到第一个字之前整个界面"像死机一样"：这轮对话现在会先做一次不流式
    的探测请求判断要不要调用工具（见assistant.stream_reply），探测本身
    实测能花4-10秒，这段时间原来的代码从用户消息发出去到出现任何回应
    之间是真正意义上的空白，跟chat_input本身没包fragment导致的"发消息要
    整页重跑一遍"叠加在一起，体验上完全等同于卡死。这个函数只负责画气泡
    本身，样式跟_chat_bubble的assistant气泡对齐（同一个背景色/圆角），
    调用方在拿到第一个真实字符前用这个占位，拿到后立刻替换掉。
    """
    dots = "".join(
        f"<span style='display:inline-block;width:6px;height:6px;border-radius:50%;"
        f"background:#8a8a92;margin:0 2px;animation:_fa_typing 1.1s ease-in-out {i * 0.15}s infinite'></span>"
        for i in range(3)
    )
    return (
        "<style>@keyframes _fa_typing{0%,60%,100%{opacity:0.25;transform:translateY(0)}"
        "30%{opacity:1;transform:translateY(-3px)}}</style>"
        "<div style='display:flex;justify-content:flex-start;margin:6px 2px'>"
        "<div style='padding:11px 15px;border-radius:16px;background:#f0f1f3'>"
        f"{dots}</div></div>"
    )


def _safe_href(url) -> str:
    """新闻链接专用——_esc() 只做HTML实体转义，不管URL的scheme是什么，理论上
    如果新闻源（财新/富途资讯/东财公告）混进一条 url 字段是 "javascript:..."
    开头，_esc()不会拦下来，<a href='javascript:...'>依然会在用户点击时执行。
    这里只放行 http/https 两种scheme，其它一律换成 "#"（点了没反应，不会跳转
    也不会执行任何东西）——当前几个新闻源可信度都比较高，这只是防御性兜底，
    不是说已经发现过真实的恶意url。
    """
    s = _esc(url)
    if not re.match(r"^https?://", s, re.IGNORECASE):
        return "#"
    return s


def _news_to_summary(news) -> str:
    """喂给AI的新闻摘要——带上日期和分类，不只是光秃秃的标题，不然AI只能看着
    一行标题瞎总结，写不出具体内容，只能说"整体偏利好"这种空话。"""
    if news is None or news.empty:
        return "无相关新闻"
    return "\n".join(
        f"- [{r.get('日期', '') or '未知日期'}] ({r.get('分类', '') or '未分类'}) {r['新闻标题']}"
        for _, r in news.iterrows()
    )


def _render_overall_summary(raw_text: str):
    """总结性分析的展示——把AI输出末尾的[综合评分: 数字]标签解析出来，做成一条
    可视化打分条摆在文字前面，分数一眼看出偏多偏空，不用读完整段文字才知道结论；
    红涨绿跌是这个项目一贯的配色约定，这里偏多用红、偏空用绿，跟涨跌颜色语义保持一致。
    """
    import re
    score = extract_score(raw_text)
    display_text = re.sub(r"\[综合评分[：:]\s*\d{1,3}\]", "", raw_text).strip()

    if score is not None:
        if score >= 65:
            color, zone = UP_COLOR, "偏多"
        elif score <= 35:
            color, zone = DOWN_COLOR, "偏空"
        else:
            color, zone = "#888", "中性"
        st.markdown(
            f"<div style='margin-bottom:14px'>"
            + f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:6px'>"
            + f"<span style='font-size:1.6rem;font-weight:700;color:{color}'>{score}</span>"
            + f"<span style='font-size:0.85rem;color:var(--fa-muted)'>/ 100 "
            + f"<span style='color:{color};font-weight:600'>{zone}</span></span>"
            + "</div>"
            + f"<div style='position:relative;height:6px;border-radius:3px;background:linear-gradient(to right,{DOWN_COLOR},#d8d8d8,{UP_COLOR})'>"
            + f"<div style='position:absolute;left:{score}%;top:-4px;width:14px;height:14px;"
            + f"border-radius:50%;background:#fff;border:3px solid {color};transform:translateX(-50%)'></div>"
            + "</div>"
            + "<div style='display:flex;justify-content:space-between;font-size:0.7rem;color:#aaa;margin-top:3px'>"
            + "<span>偏空</span><span>中性</span><span>偏多</span>"
            + "</div>"
            + "</div>",
            unsafe_allow_html=True,
        )
    st.markdown(display_text)


def _display_name(symbol: str, market: str, spot: dict) -> str:
    """给个股详情页AI模块用的展示名（拿去做新闻搜索关键词）——A股优先用
    get_stock_name(symbol)（BaoStock查到的规范公司名），因为这个名字要
    拿去搜新闻，spot实时快照（Tencent/Futu）里的名称字段有时跟新闻源
    用的公司全称对不上，会影响新闻关键词命中率；港股/美股没有BaoStock
    覆盖，退回spot快照里的名称字段。

    注意：tracker.log_analysis 存的"name"字段跟这个不是同一个口径——那边
    只是历史记录里的展示标签，不需要为了新闻命中率特地查BaoStock规范名，
    直接用spot快照里的名称即可，两处刻意保留了不同的计算，不是遗漏。
    """
    return get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)


def _stream_ai_text(gen, raise_on_error: bool = True) -> str:
    """AI流式输出的统一处理——不直接把生成器丢给st.write_stream(gen)，实测
    生成过程中一旦出错（API瞬时抖动之类），st.write_stream()会把异常悄悄
    吞掉、返回空字符串，缓存进session_state后页面上就是标题下面空空如也，
    连报错都看不见（"总结性分析"最早踩过这个坑）。手动逐块迭代、显式捕获
    异常，绝不会把空字符串当成正常结果存住。

    这里原来是两份几乎一样的代码（_write_stream_safe 给普通AI模块用，
    _stream_overall_summary 给"总结性分析"用），唯一实质区别是失败时怎么
    处理，现在用 raise_on_error 这一个参数统一表达：
    - raise_on_error=True（默认，给资讯解读/财务摘要/交叉验证这些普通模块
      用）：异常/空结果直接往外抛，让调用方自己的try/except接住去展示
      "分析失败"；成功时占位区保留完整文本（调用方不会再另外渲染一遍）。
    - raise_on_error=False（给"总结性分析"用）：异常/空结果不抛出，转成一句
      "汇总失败：..."的文本原样返回；占位区在结束时清空——因为调用方
      _render_overall_summary 会把返回的文本重新渲染成带评分条的样式，
      占位区留着原始文字会跟最终版本重复显示。
    """
    placeholder = st.empty()
    full_text = ""
    try:
        for chunk in gen:
            full_text += chunk
            placeholder.markdown(full_text + "▌")
    except Exception as e:
        if raise_on_error:
            raise
        placeholder.empty()
        return f"汇总失败：{e}"

    if not full_text.strip():
        if raise_on_error:
            raise RuntimeError("AI 没有返回任何内容")
        placeholder.empty()
        return "汇总失败：AI 没有返回任何内容，请点「重新分析」再试一次。"

    if raise_on_error:
        placeholder.markdown(full_text)
    else:
        placeholder.empty()
    return full_text


def _render_news_section(keyword: str, symbol: str | None = None, market: str = "A", is_index: bool = False):
    """一手资讯单独成块，标题不截断——是AI解读的依据来源，放在AI解读前面让用户
    自己先看一手材料。A股优先用官方公告（监管强制披露，永远免费，比新闻评论
    更"一手"，点进去就是东财公告中心原文，不存在付费墙）；港股/美股没有对应的
    免费公告聚合源，退回财新新闻摘要（有付费墙，已经标注清楚）。

    指数（is_index=True）没有公司名可以精确匹配，用 get_index_news 单独处理——
    优先走富途资讯搜索（真按这个指数的名字搜，免费可读），连不上才退回财新
    严格关键词匹配，匹配不到就如实说没有，不再拿不相关的大盘资讯硬凑（详见
    get_index_news 的说明）。
    """
    st.subheader("最新资讯")

    if is_index:
        try:
            news, idx_source = get_index_news(keyword, limit=8)
        except Exception as e:
            st.caption(f"获取失败：{e}")
            return
        if news is None or news.empty:
            st.caption("暂时没有查到相关的资讯，可能只是这几个免费源都没收录。")
            return
        if idx_source == "futu":
            st.caption("来自富途资讯搜索，按这个指数的名字精确匹配，免费可读，点标题可跳转原文。")
        else:
            st.caption("来自财新的关键词匹配资讯，原文链接需要财新会员订阅才能打开全文，这里只展示摘要。")
        idx_clickable = idx_source == "futu"
        _hot_names = _get_hot_stock_names()
        for _, r in news.iterrows():
            _title = r["新闻标题"]
            _title_color = _news_title_color(_title, _hot_names)
            _title_html = (
                f"<a href='{_safe_href(r.get('url', ''))}' target='_blank' style='color:{_title_color};text-decoration:none'>{_esc(_title)}</a>"
                if idx_clickable else f"<span style='color:{_title_color}'>{_esc(_title)}</span>"
            )
            st.markdown(
                f"<div style='margin:6px 0;font-size:0.9rem'>"
                f"<span style='color:var(--fa-muted);font-size:0.78rem'>{_esc(r.get('日期', '') or '')}</span>　"
                f"{_title_html}　"
                f"<span style='color:var(--fa-muted);font-size:0.75rem'>{_esc(r.get('分类', ''))}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        return

    news, source = _fetch_news_items(keyword, symbol, market)
    if news is None or news.empty:
        st.caption("这只股票近期没有查到直接相关的新闻，不代表没有热度，可能只是这几个免费源都没收录。")
        return

    if source == "notices":
        st.caption("来自东财公告中心的官方公告，监管强制披露，永远免费，点标题可跳转原文。")
    elif source == "futu":
        st.caption("来自富途资讯搜索，按关键词精确匹配，免费可读，点标题可跳转原文。")
    else:
        st.caption("摘要来自财新，原文链接需要财新会员订阅才能打开全文，这里只展示摘要本身。")

    clickable = source in ("notices", "futu")
    _hot_names = _get_hot_stock_names()
    for _, r in news.iterrows():
        date = r.get("日期") or ""
        title = r["新闻标题"]
        tag = r.get("分类", "")
        _title_color = _news_title_color(title, _hot_names)
        title_html = (
            f"<a href='{_safe_href(r.get('url', ''))}' target='_blank' style='color:{_title_color};text-decoration:none'>{_esc(title)}</a>"
            if clickable else f"<span style='color:{_title_color}'>{_esc(title)}</span>"
        )
        st.markdown(
            f"<div style='margin:6px 0;font-size:0.9rem'>"
            f"<span style='color:var(--fa-muted);font-size:0.78rem'>{_esc(date)}</span>　"
            f"{title_html}　"
            f"<span style='color:var(--fa-muted);font-size:0.75rem'>{_esc(tag)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _render_module(module: str, symbol: str, market: str, hist, spot: dict):
    """AI 模块按需加载：每个模块独立缓存，点开哪个才跑哪个的 AI 调用，不会一次性全跑。

    非AI的部分（原始数据表格/图表/统计指标）每次都重新算一遍——这些本来就有
    @st.cache_data缓存，重算很便宜，不用塞进session_state。真正要缓存的只有
    AI生成的文字：第一次生成时用st.write_stream()流式显示（用户反馈"一下子
    蹦出来"不像实时生成，改成打字机效果），生成完的完整文本存进session_state；
    之后重新渲染这个模块时（比如切换K线周期触发的rerun）直接用session_state
    里存好的文本静态显示，不会又调一次AI、也不会重新流式播放一遍。
    """
    mod_key = f"_detail_mod_{symbol}_{market}_{module}"
    is_fresh = mod_key not in st.session_state

    if module == "news":
        stock_name = _display_name(symbol, market, spot)
        # 原始新闻列表已经在页面上方单独一块展示了（_render_news_section），
        # 这里不重复摆一次，只放AI解读，避免同一份数据在页面上出现两遍。
        if is_fresh:
            news, _ = _fetch_news_items(stock_name, symbol, market)
            news_summary = _news_to_summary(news)
            try:
                ai_text = _stream_ai_text(summarize_news(symbol, news_summary))
            except Exception as e:
                st.error(f"分析失败：{e}")
                return
            st.session_state[mod_key] = {"ai_text": ai_text}
        else:
            st.markdown(st.session_state[mod_key]["ai_text"])

    elif module == "financial":
        fin = get_financial_abstract(symbol, market=market)
        if fin is not None and not fin.empty:
            st.dataframe(fin, use_container_width=True, hide_index=True)
            if is_fresh:
                financial_summary = fin.head(10).to_string(index=False)
                st.caption("AI 解读")
                try:
                    ai_text = _stream_ai_text(summarize_financials(symbol, financial_summary))
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
                st.session_state[mod_key] = {"ai_text": ai_text}
            else:
                st.caption("AI 解读")
                st.markdown(st.session_state[mod_key]["ai_text"])
        else:
            st.caption("暂无财务数据。")

    elif module == "benchmark":
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=90)).strftime("%Y%m%d")
        benchmark = get_benchmark_history(start, end, market=market)
        bm_name = _BENCHMARK_NAMES[market]
        if benchmark is not None and not benchmark.empty:
            st.plotly_chart(
                build_benchmark_comparison(hist, benchmark, benchmark_name=bm_name), use_container_width=True,
            )
            if is_fresh:
                stock_pct = (float(hist.iloc[-1]["收盘"]) / float(hist.iloc[0]["收盘"]) - 1) * 100
                bm_pct = (float(benchmark.iloc[-1]["收盘"]) / float(benchmark.iloc[0]["收盘"]) - 1) * 100
                st.caption("AI 解读")
                try:
                    ai_text = _stream_ai_text(summarize_benchmark(symbol, stock_pct, bm_name, bm_pct))
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
                st.session_state[mod_key] = {"ai_text": ai_text}
            else:
                st.caption("AI 解读")
                st.markdown(st.session_state[mod_key]["ai_text"])
        else:
            st.caption("基准数据暂时获取不到。")

    else:  # "cross" —— 完整交叉验证
        stats = compute_stats(hist)
        if stats:
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", stats.get("区间收益率", "—"))
            scol2.metric("年化波动率", stats.get("年化波动率", "—"))
            scol3.metric("最大回撤", stats.get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", stats.get("夏普比率(简化)", "—"))

        try:
            _intraday_for_signal = (
                get_stock_intraday_a(symbol) if market == "A" else get_stock_intraday_futu(symbol, market)
            )
        except Exception:
            _intraday_for_signal = None
        realtime_signal = compute_realtime_signal(spot, _intraday_for_signal)
        technical_summary = compute_technical_signal(hist) + " 【盘中实时信号】" + realtime_signal
        st.markdown(f"**技术面信号**：{technical_summary}")

        if hist is not None and not hist.empty:
            st.plotly_chart(build_return_histogram(hist), use_container_width=True)

        st.caption("AI 解读（交叉验证消息面、财务、技术面是否一致）")
        if is_fresh:
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
            stock_name = _display_name(symbol, market, spot)
            news, _ = _fetch_news_items(stock_name, symbol, market)
            news_summary = _news_to_summary(news)

            try:
                ai_text = _stream_ai_text(
                    cross_validate(symbol, history_summary, financial_summary, news_summary, technical_summary)
                )
            except Exception as e:
                st.error(f"分析失败：{e}")
                return
            current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
            verdict = extract_verdict(ai_text)
            # log_analysis存的"name"只是历史记录里的展示标签，口径跟上面
            # _display_name（专门为了新闻搜索命中率查BaoStock规范名）刻意
            # 不同——这里不需要那么讲究，直接用spot快照里的名称即可。
            log_name = spot.get("名称", symbol) if spot else symbol
            # 游客模式没有真实身份，不落库——"历史回看"/一致率追踪本来就是
            # 挂在账号下的个人功能，游客只看这次分析结果，不生成历史记录。
            if st.session_state.get("logged_in"):
                log_analysis(
                    st.session_state["user_email"], symbol, float(current_price), ai_text,
                    verdict=verdict, market=market, name=log_name,
                )
            st.session_state[mod_key] = {"ai_text": ai_text}
        else:
            st.markdown(st.session_state[mod_key]["ai_text"])


_PRICE_FLASH_CSS = (
    "<style>"
    "@keyframes priceFlashUp { 0% { background: rgba(224,32,32,0.28); } 100% { background: transparent; } }"
    "@keyframes priceFlashDown { 0% { background: rgba(34,160,107,0.28); } 100% { background: transparent; } }"
    ".price-flash-up { animation: priceFlashUp 1.4s ease-out; }"
    ".price-flash-down { animation: priceFlashDown 1.4s ease-out; }"
    "</style>"
)


@st.fragment(run_every=3)
def _render_price_header(symbol: str, market: str):
    """价格区块单独做成 fragment，每3秒自己刷新，不带动AI模块、新闻这些重的部分
    一起重跑——之前全页面每30秒整体rerun一次，观感上像"每隔一阵闪一下"，跟
    同花顺那种数字持续跳动的实时感完全不一样。数字真变了就闪一下背景色，
    让"活着"这件事肉眼可见，不是纯靠脑补更新时间戳。
    """
    try:
        spot = get_stock_realtime(symbol, market=market)
    except Exception:
        spot = {}
    if not (spot and spot.get("最新价")):
        st.caption("实时价格暂时取不到。")
        return

    change = spot["最新价"] - spot.get("昨收", spot["最新价"])
    change_pct = change / spot["昨收"] * 100 if spot.get("昨收") else 0
    color = UP_COLOR if change >= 0 else DOWN_COLOR

    flash_key = f"_last_price_{symbol}_{market}"
    prev = st.session_state.get(flash_key)
    st.session_state[flash_key] = spot["最新价"]
    flash_class = ""
    if prev is not None and prev != spot["最新价"]:
        flash_class = "price-flash-up" if spot["最新价"] > prev else "price-flash-down"

    st.markdown(
        _PRICE_FLASH_CSS
        + f"<div class='{flash_class}' style='margin:12px 0;padding:4px 8px;border-radius:6px'>"
        + f"<span style='font-size:2rem;font-weight:700;color:{color}'>{spot['最新价']:.2f}</span>&nbsp;&nbsp;"
        + f"<span style='font-size:1.1rem;color:{color}'>{change:+.2f} ({change_pct:+.2f}%)</span>"
        + "</div>",
        unsafe_allow_html=True,
    )
    _src = "Futu 实时" if spot.get("数据源") == "Futu实时" else "延迟行情"
    st.caption(f"{_src} · {spot.get('更新时间', '-')} · 每 3 秒自动刷新")
    hcol1, hcol2, hcol3 = st.columns(3)
    hcol1.metric("最高", f"{spot.get('最高', 0):.2f}")
    hcol2.metric("最低", f"{spot.get('最低', 0):.2f}")
    hcol3.metric("今开", f"{spot.get('今开', 0):.2f}")

    if not st.session_state.get("logged_in"):
        st.caption("登录后可关注/管理个人持仓")
    else:
        _tracked_now = is_position_tracked(st.session_state["user_email"], symbol)
        if _tracked_now:
            if st.button("取消关注", key="pos_toggle"):
                delete_position(st.session_state["user_email"], symbol)
                st.rerun()
        else:
            if st.button("关注", key="pos_toggle"):
                add_watch_only(st.session_state["user_email"], symbol, spot.get("名称", symbol), market=market)
                st.rerun()


@st.fragment(run_every=3)
def _render_index_price_header(name: str, market: str):
    """指数版的实时价格区块，逻辑跟_render_price_header一样，独立的 fragment。"""
    try:
        idx_snap = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
    except Exception:
        idx_snap = None
    if not idx_snap:
        st.caption("实时行情暂时取不到。")
        return

    color = UP_COLOR if idx_snap["涨跌"] >= 0 else DOWN_COLOR
    flash_key = f"_last_price_idx_{name}_{market}"
    prev = st.session_state.get(flash_key)
    st.session_state[flash_key] = idx_snap["最新"]
    flash_class = ""
    if prev is not None and prev != idx_snap["最新"]:
        flash_class = "price-flash-up" if idx_snap["最新"] > prev else "price-flash-down"

    st.markdown(
        _PRICE_FLASH_CSS
        + f"<div class='{flash_class}' style='margin:12px 0;padding:4px 8px;border-radius:6px'>"
        + f"<span style='font-size:2rem;font-weight:700;color:{color}'>{idx_snap['最新']:,.2f}</span>&nbsp;&nbsp;"
        + f"<span style='font-size:1.1rem;color:{color}'>{idx_snap['涨跌']:+.2f} ({idx_snap['涨跌幅']:+.2f}%)</span>"
        + "</div>",
        unsafe_allow_html=True,
    )
    st.caption("每 3 秒自动刷新")


def _inject_pos_card_css():
    """pos-card-link 这个class的样式——多个板块（持仓/成分股/涨跌停池/核心股
    榜）共用同一个class做卡片点击跳转，样式只需要注入一次，但每个板块渲染时
    不一定确定其它板块的注入代码有没有跑过，重复调用这个函数是幂等的，
    不会有副作用。
    """
    st.markdown(
        "<style>"
        "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.pos-card-link:hover { opacity: 0.85; }"
        "</style>",
        unsafe_allow_html=True,
    )


def _render_stock_movers_cards(df, market: str):
    """把一份"代码/名称/最新价/涨跌幅"的行情表渲成一叠可点击卡片（红涨绿跌，
    点击跳去那只股票详情页）——涨跌停池、港股/美股核心股榜、指数成分股都是
    这个形态，抽成公共函数不用每处各写一遍。df为空时调用方自己处理提示语，
    这里不管。

    价格变了背景闪一下红/绿：跟持仓列表(_render_position_rows)、详情页
    价格区块(_render_price_header)同一套_PRICE_FLASH_CSS机制，用户反馈"行情
    里的股票也要有这个效果"——调用方（涨跌停池/港股核心股/美股核心股这几个
    fragment）都已经是run_every=3自动刷新，数据变了这里自然就能跟着闪。
    """
    _inject_pos_card_css()
    st.markdown(_PRICE_FLASH_CSS, unsafe_allow_html=True)
    for _, row in df.iterrows():
        mv_symbol = str(row["代码"])
        mv_color = UP_COLOR if row["涨跌幅"] >= 0 else DOWN_COLOR
        href = (
            f"?open_symbol={urllib.parse.quote(mv_symbol)}"
            f"&open_market={urllib.parse.quote(market)}"
            f"&open_name={urllib.parse.quote(str(row['名称']))}"
            f"{_auth_qs()}"
        )
        flash_key = f"_mv_last_price_{mv_symbol}_{market}"
        prev = st.session_state.get(flash_key)
        st.session_state[flash_key] = row["最新价"]
        flash_class = ""
        if prev is not None and prev != row["最新价"]:
            flash_class = "price-flash-up" if row["最新价"] > prev else "price-flash-down"
        with st.container(border=True):
            st.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row {flash_class}' style='display:flex;align-items:center;border-radius:4px'>"
                f"<div style='flex:2;font-weight:600;color:var(--fa-text);text-decoration:none'>"
                f"{_esc(row['名称'])}（{_esc(mv_symbol)}）</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{mv_color}'>{row['最新价']:.2f}</div>"
                f"<div style='flex:1;text-align:right;color:{mv_color}'>{row['涨跌幅']:+.2f}%</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


_HSTECH_ASOF = "2026-06-08"  # _HSTECH_CONSTITUENTS名单的生效日期，手动维护，见data_sources.py里的说明


def _render_index_top_movers(market: str, index_name: str = ""):
    """指数详情页的"成分股"板块。宽基指数（上证/深证/创业板/恒生/国企/标普/
    纳指/道琼斯）用get_index_top_movers那套"这个市场涨幅最大的股票"代理
    指标——不是严格的官方成分股清单，但对宽基指数勉强说得过去。

    "恒生科技"这种行业主题指数走单独的真实成分股路径（get_hstech_
    constituents）：之前也用这套代理指标，结果混进了优然牧业（乳业）、
    布鲁可（玩具）这些跟科技毫不相关的公司；改成完全不展示后用户又反馈
    "之前虽然错但至少有内容，现在啥都没有"——两头都不是想要的效果，最后
    查了真实的恒生科技成分股名单手动维护起来（见data_sources.py），比
    "编数据"和"不展示"都更贴近用户实际想要的东西。默认显示前10，点
    "展开"再显示到前30，卡片点击直接跳去那只股票的详情页。
    """
    if index_name == "恒生科技":
        try:
            movers = get_hstech_constituents(limit=30)
        except Exception:
            movers = None
        if movers is None or movers.empty:
            st.caption("恒生科技成分股数据需要本地/服务器跑 Futu OpenD 网关，当前没有检测到连接，暂不可用。")
            return
        st.caption(
            f"恒生科技指数真实成分股（名单截至 {_HSTECH_ASOF} 生效，手动维护——"
            "指数公司按季度调整，这份名单可能跟最新官方名单有出入），按当日涨跌幅排序。"
        )
        expand_key = f"_movers_expand_{market}_hstech"
        show_n = 30 if st.session_state.get(expand_key) else 10
        _render_stock_movers_cards(movers.head(show_n), market)
        if len(movers) > 10:
            if not st.session_state.get(expand_key):
                if st.button("展开（前30）", key=f"_movers_expand_btn_{market}_hstech"):
                    st.session_state[expand_key] = True
                    st.rerun()
            else:
                if st.button("收起", key=f"_movers_collapse_btn_{market}_hstech"):
                    st.session_state[expand_key] = False
                    st.rerun()
        return

    try:
        movers = get_index_top_movers(market, limit=30, index_name=index_name)
    except Exception:
        movers = None
    if movers is None or movers.empty:
        st.caption("暂时获取不到数据。")
        return

    if market == "A":
        st.caption("按当前A股全市场涨跌幅排序，不是这个指数的官方成分股名单。")
    elif market == "HK":
        st.caption("按港股热门个股的涨跌幅排序，不是这个指数的官方成分股名单。")
    else:
        st.caption("覆盖美股主要板块龙头股，按涨跌幅排序，不是这个指数的官方成分股名单。")

    # 之前是 f"_movers_expand_{market}"，只带市场不带指数名——同一市场下
    # 切换不同指数（比如A股的上证指数/深证成指/创业板指）会共享同一个展开
    # 状态，在一个指数里点过"展开"，切到同市场另一个指数也变成展开状态。
    # 恒生科技分支（上面）已经正确带了 "_hstech" 后缀，这里补上指数名区分。
    _movers_qualifier = index_name or "default"
    expand_key = f"_movers_expand_{market}_{_movers_qualifier}"
    show_n = 30 if st.session_state.get(expand_key) else 10
    _render_stock_movers_cards(movers.head(show_n), market)

    if len(movers) > 10:
        if not st.session_state.get(expand_key):
            if st.button("展开（前30）", key=f"_movers_expand_btn_{market}_{_movers_qualifier}"):
                st.session_state[expand_key] = True
                st.rerun()
        else:
            if st.button("收起", key=f"_movers_collapse_btn_{market}_{_movers_qualifier}"):
                st.session_state[expand_key] = False
                st.rerun()


@st.fragment
def _render_index_snapshot(mkt_code: str):
    """"行情"tab顶部的指数快照卡片。做成fragment的原因见_render_a_share_overview
    开头的注释——本质是同一个问题：这几个区块以前全挤在同一段代码里，点其中
    任何一个的交互按钮都会连带其余区块一起重新拉一遍数据。

    之前试过给这个fragment加run_every=3做涨跌闪烁，结果导致"从行情切到
    持仓"出现页面残留——猜测是这个fragment自己的自动刷新定时器切换页面
    后仍在后台继续触发，把已经不该存在的旧内容重新塞回DOM里。残留问题
    优先级更高，撤回run_every，闪烁效果的代码保留（不会触发，也无副作用），
    等找到不会导致残留的方案再说。

    2026-08-30修复第一版：这个页面本身就是要给"较新"的行情数据，get_multi_
    index_snapshot偶尔慢（港股/美股实测出现过接近10秒），点市场切换时
    Streamlit整页rerun，在数据回来之前页面停在上一个市场的旧卡片上、没有
    任何提示，用户会以为看到的还是新选市场的数据，其实是没刷新的旧数字——
    比单纯等待更容易造成误导。加个spinner，至少明确告诉用户"正在查，这
    不是最终结果"。

    2026-08-30修复第二版：跟用户明确确认过，接受首页地图那份预热缓存
    （warm_home_cache.py每分钟更新，最多1分钟旧）的新鲜度用来换稳定秒开，
    这里改成优先读同一份缓存（load_home_map_cache）——三处（首页地图/
    这里/AI咨询窗）现在共用同一份数据，不用各自另开一条查询路径。缓存
    没有或太旧（预热脚本没跑上）才退回上面第一版那套spinner+实时查询。
    """
    cached = load_home_map_cache(max_age_sec=90)
    if cached:
        idx_list = cached["snaps"].get(mkt_code, [])
    else:
        with st.spinner(f"加载{mkt_code}行情..."):
            try:
                idx_list = get_multi_index_snapshot(mkt_code)
            except Exception:
                idx_list = []

    _idx_code_by_name = dict(_MULTI_INDICES.get(mkt_code, []))

    if not idx_list:
        st.caption("指数数据暂时获取不到。")
        return

    st.markdown(
        _PRICE_FLASH_CSS
        + "<style>"
        "a.idx-card-link, a.idx-card-link:link, a.idx-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.idx-card-link:hover { opacity: 0.85; }"
        "</style>"
        "<div class='fa-flex-row' style='display:flex;padding:4px 8px;font-size:0.78rem;color:var(--fa-muted)'>"
        "<div style='flex:2.4'>指数</div>"
        "<div style='flex:1;text-align:right'>最新</div>"
        "<div style='flex:1;text-align:right'>涨幅</div>"
        "<div style='flex:1;text-align:right'>涨跌</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    for idx in idx_list:
        color = UP_COLOR if idx["涨跌"] >= 0 else DOWN_COLOR
        idx_code = _idx_code_by_name.get(idx["名称"], "")
        href = (
            f"?open_index_code={urllib.parse.quote(idx_code)}"
            f"&open_index_market={urllib.parse.quote(mkt_code)}"
            f"&open_index_name={urllib.parse.quote(idx['名称'])}"
            f"{_auth_qs()}"
        )
        flash_key = f"_idx_snap_last_{idx['名称']}_{mkt_code}"
        prev = st.session_state.get(flash_key)
        st.session_state[flash_key] = idx["最新"]
        flash_class = ""
        if prev is not None and prev != idx["最新"]:
            flash_class = "price-flash-up" if idx["最新"] > prev else "price-flash-down"
        with st.container(border=True):
            st.markdown(
                f"<a class='idx-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row {flash_class}' style='display:flex;align-items:center;border-radius:4px'>"
                f"<div style='flex:2.4;font-weight:600;color:var(--fa-text);text-decoration:none'>{_esc(idx['名称'])}</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{color}'>{idx['最新']:,.2f}</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌']:+.2f}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


@st.fragment
def _render_a_share_overview():
    """A股大盘统计+涨停/跌停股池。之前这几块和指数快照、热门板块全部挤在
    "行情"tab同一段代码里——点"显示更多（前30）"这一个按钮，会触发整个
    tab重新rerun，连带指数快照、热门板块这些跟这次点击完全无关的区块也要
    重新拉一遍数据（其中指数快照缓存只有25秒，涨停跌停池等未必命中缓存），
    这是页面交互感觉卡顿的主要原因。拆成独立fragment后，点这个按钮只会
    重新跑这一个区块。

    之前试过加run_every=3做涨跌闪烁，结果导致"从行情切到持仓"出现页面
    残留（这几个fragment自己的定时器猜测在切页后仍在后台触发，把旧内容
    重新塞回DOM）。残留问题优先级更高，撤回run_every，闪烁效果的代码
    保留（不会触发，也无副作用）。
    """
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
            if up_pool is not None and not up_pool.empty:
                _render_stock_movers_cards(up_pool, "A")
            else:
                st.caption("暂时没有数据。")
        except Exception as e:
            st.caption(f"获取失败：{e}")
    with down_col:
        st.markdown("**跌停股池**")
        try:
            down_pool = get_limit_pool("down", show_n)
            if down_pool is not None and not down_pool.empty:
                _render_stock_movers_cards(down_pool, "A")
            else:
                st.caption("暂时没有数据。")
        except Exception as e:
            st.caption(f"获取失败：{e}")
    if not st.session_state.get("_show_more_limit_pool"):
        if st.button("显示更多（前30）", key="_more_limit_pool"):
            st.session_state["_show_more_limit_pool"] = True
            st.rerun()


@st.fragment
def _render_hk_overview():
    """港股南向资金+核心股，独立fragment，原因同_render_a_share_overview
    （包括撤回run_every=3的原因——切到持仓页面残留）。"""
    try:
        south = get_southbound_flow()
    except Exception:
        south = None
    if south:
        _s_color = UP_COLOR if south["净买额"] >= 0 else DOWN_COLOR
        st.markdown(
            f"<div style='margin:4px 0 12px'>南向资金净买额　"
            f"<span style='color:{_s_color};font-weight:700;font-size:1.2rem'>"
            f"{south['净买额']:+.2f}亿</span></div>",
            unsafe_allow_html=True,
        )
    st.markdown("**港股核心股（按涨跌幅排）**")
    try:
        hk_movers = get_hk_famous_movers(15)
        if hk_movers is not None and not hk_movers.empty:
            _render_stock_movers_cards(hk_movers, "HK")
        else:
            st.caption("暂时获取不到数据。")
    except Exception as e:
        st.caption(f"获取失败：{e}")


@st.fragment
def _render_us_overview():
    """美股核心股，独立fragment，原因同_render_a_share_overview
    （包括撤回run_every=3的原因——切到持仓页面残留）。"""
    st.markdown("**美股核心股**")
    try:
        us_movers = get_us_famous_movers(15)
        if us_movers is not None and not us_movers.empty:
            _render_stock_movers_cards(us_movers, "US")
        else:
            st.caption("暂时获取不到数据。")
    except Exception as e:
        st.caption(f"获取失败：{e}")


@st.fragment
def _render_hot_sectors(market: str):
    """"热门板块"——按热度（成交额代理）排序的行业板块，3×3宫格展示前9名，
    "更多板块"展开到前30。点击板块卡片会跳到该板块的成分股列表（复用
    get_sector_constituents + _render_stock_movers_cards），成分股本身
    再点进去就是已有的个股详情页（走势+AI分析）——板块这一层不需要单独
    造一套K线/AI分析，成分股列表是已有能力的自然延伸。

    做成fragment：点"更多板块/收起"之前会带动整个"行情"tab（指数快照、
    涨停跌停池等）一起重新拉一遍数据，明明只是想展开这一个板块列表。
    """
    try:
        sectors = get_hot_sectors(market, limit=30)
    except Exception:
        sectors = None
    if sectors is None or sectors.empty:
        if market == "A":
            st.caption("暂时获取不到板块数据。")
        else:
            st.caption("港股/美股板块数据需要本地或服务器跑 Futu OpenD 网关，当前没有检测到连接，暂不可用。")
        return

    if market == "A":
        st.caption("按板块总成交额排序（成交额代理热度，不是官方板块人气榜）。")
    else:
        st.caption("按板块成交额排序（成交额代理热度，需本地 Futu 网关支持，未连接时暂不可用）。")

    expand_key = f"_sectors_expand_{market}"
    show_n = 30 if st.session_state.get(expand_key) else 9
    shown = sectors.head(show_n).reset_index(drop=True)

    for row_start in range(0, len(shown), 3):
        cols = st.columns(3)
        for i, col in enumerate(cols):
            idx = row_start + i
            if idx >= len(shown):
                continue
            row = shown.iloc[idx]
            s_color = UP_COLOR if row["涨跌幅"] >= 0 else DOWN_COLOR
            inner = (
                f"<div style='font-weight:600;color:var(--fa-text)'>{_esc(str(row['板块']))}</div>"
                f"<div style='color:{s_color};font-weight:700;font-size:1.1rem'>{row['涨跌幅']:+.2f}%</div>"
                f"<div style='color:var(--fa-muted);font-size:0.78rem'>热度第{idx + 1}名</div>"
            )
            with col:
                with st.container(border=True):
                    if market == "A":
                        # A股板块成分股走东财接口，实测连接经常失败（东财板块类
                        # 接口的老问题），点进去大概率只看到"获取不到"，体验比
                        # 不能点还差——干脆A股这边先不做成可点击，跟原来一样纯展示。
                        # 港股/美股走Futu，可靠，保留可点击。
                        st.markdown(inner, unsafe_allow_html=True)
                    else:
                        href = (
                            f"?open_sector={urllib.parse.quote(str(row['板块']))}"
                            f"&open_sector_market={urllib.parse.quote(market)}"
                            f"{_auth_qs()}"
                        )
                        st.markdown(
                            "<style>a.sector-card-link, a.sector-card-link:link, a.sector-card-link:visited {"
                            "text-decoration:none !important; color:inherit !important; display:block; cursor:pointer;"
                            "}</style>"
                            f"<a class='sector-card-link' href='{href}' target='_self'>{inner}</a>",
                            unsafe_allow_html=True,
                        )

    if len(sectors) > 9:
        if not st.session_state.get(expand_key):
            if st.button("更多板块", key=f"_sectors_more_btn_{market}"):
                st.session_state[expand_key] = True
                st.rerun()
        else:
            if st.button("收起", key=f"_sectors_collapse_btn_{market}"):
                st.session_state[expand_key] = False
                st.rerun()


def _render_sector_detail(name: str, market: str):
    """板块详情页——只展示成分股列表，复用_render_stock_movers_cards，每只
    成分股点进去就是已有的个股详情页（走势+AI分析）。板块本身不需要单独的
    K线/AI分析，这里不重新造轮子。
    """
    st.markdown(
        "<style>[class*='st-key-sector_back_'] button p { font-size: 1.5rem !important; font-weight: 700; }</style>",
        unsafe_allow_html=True,
    )
    if st.button("←", key=f"sector_back_{name}_{market}", type="tertiary", help="返回行情"):
        for k in ("_sector_detail_name", "_sector_detail_market"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "行情"
        st.rerun()

    st.markdown(
        f"""
        <div style='background:{UP_COLOR};margin:-1rem -1rem 0 -1rem;padding:14px 24px'>
            <div style='color:#fff;font-size:1.2rem;font-weight:700'>{_esc(name)}</div>
            <div style='color:#fff;font-size:0.85rem;opacity:0.85'>{market}股 · 行业板块</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()
    st.caption("成分股按涨跌幅排序，点击可查看该股票的走势图和 AI 分析。")

    try:
        cons = get_sector_constituents(market, name, limit=30)
    except Exception:
        cons = pd.DataFrame()
    if cons.empty:
        if market == "A":
            st.caption("暂时获取不到这个板块的成分股——东财的板块成分股接口偶尔不稳定，或者这个板块名称跟东财自己的分类对不上，稍后再试。")
        else:
            st.caption("暂时获取不到这个板块的成分股，可能是 Futu 连接暂时不可用，稍后再试。")
        return
    _render_stock_movers_cards(cons, market)


_HOME_MAP_MARKERS = [
    # (指数名, 所在市场, 纬度, 经度) —— 用户反馈"上证/恒生离得太近重叠"，
    # 明确说"地理位置可以适当牺牲，但两两都不要重合"；第一版把上证/韩国/
    # 新加坡挪得太夸张（上证跑到蒙古附近、韩国跑到赤道、新加坡跑到印度洋），
    # 用户又反馈"位置比较离谱"——现在配合下面缩小的标签尺寸，把这几个挪回
    # 更接近真实地理位置、只做小幅度错开，不再是大幅乱跳。
    # 市场是"GLOBAL"的走get_global_indices()那条单独的数据源(Yahoo Finance)，
    # 不是get_multi_index_snapshot。
    ("恒生指数", "HK", 22.0, 114.0),
    ("上证指数", "A", 38.0, 110.0),          # 真实上海在(31,121)，往西北挪一点跟恒生/日经拉开
    ("标普500", "US", 40.0, -95.0),
    ("纳斯达克100", "US", 48.0, -122.0),
    ("日经225", "GLOBAL", 36.0, 142.0),
    ("富时100", "GLOBAL", 54.0, -3.0),
    ("德国DAX", "GLOBAL", 40.0, 15.0),        # 真实德国大约在(47-55,6-15)；上一版(51,10)纬度跟富时100(54,-3)太接近，标签框在地图上挤在一起重叠，往南挪到意大利/亚得里亚海附近拉开纬度差(14)，跟富时100不再重叠
    ("印度SENSEX", "GLOBAL", 19.0, 73.0),
    ("巴西IBOVESPA", "GLOBAL", -15.0, -55.0),
    ("澳大利亚ASX200", "GLOBAL", -30.0, 145.0),
    ("新加坡STI", "GLOBAL", -5.0, 105.0),     # 真实新加坡在(1,104)，基本没挪，本来就够空
]

# 恒生指数/上证指数/标普500/纳斯达克100这4个能查到腾讯行情接口
# (qt.gtimg.cn) 对应的代码——实测这个接口带 access-control-allow-origin: *
# 响应头，浏览器JS可以直接跨域fetch，不用经过我们自己的Streamlit后端。
# 东财/Yahoo那7个国际指数没有这个响应头，浏览器直接fetch会被CORS拦下来，
# 只能停留在"页面加载时的服务端快照"，做不到这几个的秒级实时刷新——
# 这是浏览器安全机制的硬限制，不是不想做。（道琼斯按用户要求去掉了，
# 换成保留纳斯达克100，美股只留标普+纳斯达克两个。）
_HOME_MAP_TENCENT_CODE = {
    "恒生指数": "r_hkHSI", "上证指数": "r_sh000001",
    "标普500": "usINX", "纳斯达克100": "usNDX",
}

def _render_home_map():
    """首页世界地图——Leaflet.js + OpenStreetMap 免费瓦片（不需要API key/信用卡），
    在几个指数所在交易所城市的真实经纬度上放小图标，图标里显示指数名+当前点数+
    涨跌幅（红涨绿跌）。

    恒生指数/上证指数/标普500/纳斯达克100这4个走浏览器JS直接每3秒
    fetch腾讯行情接口（见_HOME_MAP_TENCENT_CODE的说明），原地更新图标，
    不牵扯Streamlit的rerun——这样既有实时跳动效果，又不会重蹈"行情"tab
    那几个卡片加run_every=3导致切页残留的覆辙（那次是Python侧的fragment
    定时器在背后继续触发；这次刷新完全在iframe内部的JS里自己完成，跟
    Streamlit的脚本重跑机制没有任何关系，理论上不会有同类残留风险）。
    其余7个国际指数（Yahoo Finance源，CORS不开放）保持页面加载时的
    服务端快照，不会跳动。道琼斯没有单独放在地图标记里（见
    _HOME_MAP_MARKERS 的说明，美股只保留标普+纳斯达克两个）。

    2026-08-30修复第一版：原来这里直接调get_multi_index_snapshot（3秒缓存，
    内部又是刻意串行拉，见该函数docstring），三个市场顺序叠加，实测
    经常20秒以上打不开首页——而且这段等待期间是一片空白，连个转圈动画
    都没有，用户反馈"很多地方长时间显示加载中，卡死在那边很丑"。这正是
    assistant.py早先复用错函数踩过的同一个坑（见get_multi_index_
    snapshot_slow的docstring）——首页地图跟AI助手一样，都是"打开时
    查一次快照，不需要3秒级跳动"的场景（4个核心指数本来就有独立的
    浏览器JS每3秒直接跳动，不依赖这次服务端快照），换成60秒缓存的
    _slow版本+并发（用已有的_run_concurrent_with_deadline，8秒截止），
    一个市场慢/卡不连累另外两个。这一版把20秒压到3秒左右，但"3秒"仍然是
    每个访问者都要付的成本。

    2026-08-30修复第二版：加了warm_home_cache.py，每分钟由系统crontab
    独立跑一次（不是Streamlit进程自己的定时任务），把三个市场+国际指数
    提前查好写进data/home_map_cache.json。这里改成优先读这个文件——
    读到90秒以内的新鲜数据就直接用，完全不用等网络，只有文件缺失/太旧
    （预热脚本没跑上）时才退回上面第一版那套并发查+8秒截止的兜底路径，
    不会比修复前更差。详见warm_home_cache.py开头的说明（包括这么做的
    代价：预热脚本会不看有没有真人访问都固定按分钟去查免费接口）。
    """
    snaps: dict[str, list[dict]] = {}
    global_idx: dict = {}
    cached = load_home_map_cache(max_age_sec=90)
    if cached:
        snaps, global_idx = cached["snaps"], cached["global_idx"]
    else:
        with st.spinner("加载全球指数..."):
            snap_results = _run_concurrent_with_deadline(
                ["A", "HK", "US"], get_multi_index_snapshot_slow, timeout=8, max_workers=3
            )
        snaps = dict(zip(["A", "HK", "US"], [snap_results.get(i, []) for i in range(3)]))
        try:
            global_idx = get_global_indices()
        except Exception:
            global_idx = {}

    # 地图图标点进对应指数详情页——只有恒生指数/上证指数/标普500/纳斯达克100这4个
    # 有真正的详情页数据支撑（_MULTI_INDICES里的A/HK/US市场，K线/成分股/AI分析全套都有）。
    # 其余7个国际指数走的是Yahoo Finance(get_global_indices)，现有详情页架构
    # (_render_index_detail/get_multi_index_snapshot)只认A/HK/US这三个市场，
    # 没有对应的K线/成分股数据源，硬点进去打不开一个能用的详情页，所以先只给
    # 这4个能查到code的指数加跳转，其余7个先保持不可点击。
    href_by_name: dict[str, str] = {}
    for name, mkt, _, _ in _HOME_MAP_MARKERS:
        if mkt == "GLOBAL":
            continue
        code = dict(_MULTI_INDICES.get(mkt, [])).get(name)
        if code:
            href_by_name[name] = (
                f"?open_index_code={urllib.parse.quote(code)}"
                f"&open_index_market={urllib.parse.quote(mkt)}"
                f"&open_index_name={urllib.parse.quote(name)}"
                f"{_auth_qs()}"
            )

    markers_js = []
    for name, mkt, lat, lon in _HOME_MAP_MARKERS:
        if mkt == "GLOBAL":
            idx = global_idx.get(name)
        else:
            idx = next((i for i in snaps.get(mkt, []) if i["名称"] == name), None)
        if not idx:
            continue
        color = UP_COLOR if idx["涨跌"] >= 0 else DOWN_COLOR
        # 标签尺寸缩小过一版——用户反馈"标签能小点的话就不会挤一起了"，
        # 从 padding 4px 8px/font-size 0.75rem 缩到 2px 5px/0.6rem，
        # iconSize 从 [90,50] 缩到 [68,38]，但没有缩到看不清的程度
        # （名称+点数+涨跌幅三行还是各自独占一行，只是整体更紧凑）。
        inner = (
            f"<div style='background:#fff;border:1px solid #ddd;border-radius:5px;"
            f"padding:2px 5px;font-size:0.6rem;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,0.15)'>"
            f"<div style='font-weight:600;color:#0f172a'>{name}</div>"
            f"<div style='color:{color};font-weight:700'>{idx['最新']:,.2f}</div>"
            f"<div style='color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
            f"</div>"
        )
        href = href_by_name.get(name)
        if href:
            # target='_top'：这个地图本身渲染在st.components.v1.html的iframe里，
            # 普通<a>点击只会在iframe内部跳转、看不到效果，_top让浏览器在最外层
            # 文档导航，才能真正带动Streamlit主页面的query params跳转到详情页。
            label = f"<a href='{href}' target='_top' style='cursor:pointer;text-decoration:none'>{inner}</a>"
        else:
            label = inner
        if name in _HOME_MAP_TENCENT_CODE:
            # 存进tcMarkers，供后面的JS轮询按名字找到这个marker原地更新图标。
            markers_js.append(
                "tcMarkers[%s] = L.marker([%s, %s], {icon: L.divIcon({html: %s, className: '', iconSize: [68, 38], iconAnchor: [34, 38]})}).addTo(map);"
                % (json.dumps(name), lat, lon, json.dumps(label))
            )
        else:
            markers_js.append(
                "L.marker([%s, %s], {icon: L.divIcon({html: %s, className: '', iconSize: [68, 38], iconAnchor: [34, 38]})}).addTo(map);"
                % (lat, lon, json.dumps(label))
            )

    if not markers_js:
        st.caption("指数数据暂时获取不到，地图先不展示。")
        return

    tencent_codes = list(_HOME_MAP_TENCENT_CODE.values())
    code_to_name = {v: k for k, v in _HOME_MAP_TENCENT_CODE.items()}

    map_html = f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <div id="home-map" style="height:420px;border-radius:8px;overflow:hidden"></div>
    <script>
    // 用户反馈缩放功能容易误触，干脆整个禁掉——不止滚轮缩放，双击/触摸
    // 双指缩放/框选缩放/键盘+-缩放、缩放按钮全部关掉，固定在一个能看到
    // 所有图标的世界视角，不会被不小心手滑放大/缩小。保留拖拽平移，
    // 纯粹"缩放"这个动作不再存在。
    var map = L.map('home-map', {{
        scrollWheelZoom: false, doubleClickZoom: false, touchZoom: false,
        boxZoom: false, keyboard: false, zoomControl: false, dragging: true,
    }}).setView([12, 25], 2);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap contributors', maxZoom: 8
    }}).addTo(map);
    var tcMarkers = {{}};
    {' '.join(markers_js)}

    var codeToName = {json.dumps(code_to_name)};
    var hrefByName = {json.dumps(href_by_name)};
    function fmtNum(n) {{ return n.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}}); }}
    function updateTcMarkers() {{
        fetch('https://qt.gtimg.cn/q={",".join(tencent_codes)}')
            .then(function(r) {{ return r.text(); }})
            .then(function(text) {{
                text.split(';').forEach(function(line) {{
                    line = line.trim();
                    if (!line) return;
                    var eq = line.indexOf('=');
                    if (eq < 0) return;
                    var code = line.substring(0, eq).replace('v_', '');
                    var name = codeToName[code];
                    if (!name || !tcMarkers[name]) return;
                    var val = line.substring(eq + 1).replace(/^"|"$/g, '');
                    var fields = val.split('~');
                    if (fields.length < 35) return;
                    var last = parseFloat(fields[3]);
                    var changeAmt = parseFloat(fields[31]);
                    var changePct = parseFloat(fields[32]);
                    if (isNaN(last) || isNaN(changePct)) return;
                    var color = changeAmt >= 0 ? '{UP_COLOR}' : '{DOWN_COLOR}';
                    var inner = "<div style='background:#fff;border:1px solid #ddd;border-radius:5px;"
                        + "padding:2px 5px;font-size:0.6rem;white-space:nowrap;box-shadow:0 1px 4px rgba(0,0,0,0.15)'>"
                        + "<div style='font-weight:600;color:#0f172a'>" + name + "</div>"
                        + "<div style='color:" + color + ";font-weight:700'>" + fmtNum(last) + "</div>"
                        + "<div style='color:" + color + "'>" + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + "%</div>"
                        + "</div>";
                    // 3秒轮询刷新图标时也要重新套上跳转链接，不然刷新一次链接就消失了
                    var href = hrefByName[name];
                    var html = href
                        ? "<a href='" + href + "' target='_top' style='cursor:pointer;text-decoration:none'>" + inner + "</a>"
                        : inner;
                    tcMarkers[name].setIcon(L.divIcon({{html: html, className: '', iconSize: [68, 38], iconAnchor: [34, 38]}}));
                }});
            }})
            .catch(function(e) {{}});
    }}
    setInterval(updateTcMarkers, 3000);
    </script>
    """
    _cv1.html(map_html, height=440)


_ADVICE_EMAIL = os.environ.get("ADVISOR_EMAIL", "")  # advisor.py 私人脚本写advice表时用的固定账号，跟当前登录访客无关
_ADVICE_ACTION_COLOR = {"买入": UP_COLOR, "卖出": DOWN_COLOR, "持有": NEUTRAL_COLOR, "观望": NEUTRAL_COLOR}
_ADVICE_SECTIONS = ("结论", "置信度", "基本面", "技术面", "价格位置", "理由")


def _parse_advice_text(text: str) -> dict:
    """advisor.py 里 judge_stock() 的输出是固定格式的多段文本（结论/置信度/
    基本面/技术面/价格位置/理由几段），这里按段名切开，首页卡片只挑"理由"
    直接展示（信息量最高、篇幅可控），"基本面/技术面/价格位置"放进展开区，
    不是把整段AI原文糊在首页上——那样篇幅太长，跟首页其它卡片（单行新闻）
    的信息密度不一致。"""
    parts: dict[str, str] = {}
    text = text or ""
    positions = []
    for name in _ADVICE_SECTIONS:
        idx = text.find(f"{name}：")
        if idx == -1:
            idx = text.find(f"{name}:")
        if idx != -1:
            positions.append((idx, name))
    positions.sort()
    for i, (idx, name) in enumerate(positions):
        start = idx + len(name) + 1
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        parts[name] = text[start:end].strip()
    return parts


def _render_advice_section():
    """首页"AI投研候选"——跟其它模块（世界地图/今日资讯）唯一的本质区别：
    这里明确给买入/卖出/持有/观望结论，其它模块刻意"只摆事实不下结论"。
    这个差异必须对访客说清楚，不能让人以为整个网站的调性突然变了。

    只读advisor.py（私人cron脚本，工作日17:30跑一次）写进advice表的最近一次
    结果，首页访问不现场重新跑——重新跑一次要几分钟、几十次AI调用，公开页面
    每次访问都触发一遍完全不现实，也没必要（这类基本面判断一天一次足够新）。
    """
    st.markdown("**推荐股排行榜**")
    # 卡片可点击跳转详情页——复用持仓列表卡片验证过的方案（见
    # _render_position_rows 的踩坑记录：JS/CSS猜DOM结构点不动，最后用最朴素
    # 的<a href="?open_symbol=...">整页导航才可靠）。那段CSS只在持仓tab渲染
    # 时才注入，首页不一定会经过那个函数，这里独立注入一份，不依赖执行顺序。
    st.markdown(
        "<style>"
        "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.pos-card-link:hover { opacity: 0.85; }"
        "</style>",
        unsafe_allow_html=True,
    )
    try:
        # source="watchlist"：2026-08-28改用固定规模但每天用真实热度/涨跌幅
        # 榜重新取一遍的观察池（美股20/港股20/A股10，见advisor.py的
        # _build_watchlist），取代原来"全市场量化初筛"的screen——用户明确
        # 要求首页这块要能天天复现、事后可核对，不是每天看着完全不一样的
        # 候选。原来的screen_candidates()仍然在跑，只是不再喂首页，继续
        # 只进私人微信简报。
        data = get_latest_leaderboard(limit=5, source="watchlist")
    except Exception:
        st.caption("候选数据暂时读取失败。")
        return

    if not data.get("run_date"):
        st.caption("还没有生成过推荐股排行榜（每个工作日17:30自动更新一次）。")
        return

    # 说明文字/更新时间/历史一致率折进一个默认收起的expander——不再在模块
    # 顶部常驻一整段小字。用户明确要求"小字不要"，但这块信息（尤其"不构成
    # 投资建议"+可验证的历史一致率）是Fable 5合规审查时的结论，不能整段删掉，
    # 收进一次点击可见的位置，两边都照顾到。
    with st.expander("说明", expanded=False):
        st.caption(
            "跟本页其它模块不同：这里 AI 会给出买入/卖出/持有/观望的明确结论（其它模块只摆事实、"
            "不下结论）。观察池是美股/港股/A股里当天最热门的约50支股票（按真实热度/涨跌幅榜取，"
            "不是固定名单，每天会变），AI 基于真实财务数据+技术面+52周价格位置逐支判断，次日核对"
            "一次涨跌方向对不对，仅供参考，不构成投资建议，请自行判断。"
        )
        st.caption(f"更新于 {data['run_date']}（每个工作日17:30自动更新）")

        # 历史方向一致率——比语气强硬更有说服力：Fable 5独立审查这个模块时指出，
        # get_advice_accuracy 这个函数早就写好了但从没接到UI上过，是这个模块目前
        # 唯一真正"有理有据"的可验证证据，比调整AI措辞的成本低得多、也不涉及
        # 弱化风险提示。_ADVICE_EMAIL是advisor.py那个私人脚本写数据时用的固定
        # 账号（这个模块的数据来源是私人cron脚本，不是当前登录访客本人的判断
        # 记录，所以这里查的是那个固定账号，不是st.session_state里的当前用户）。
        try:
            acc_all = get_advice_accuracy(_ADVICE_EMAIL)
            # 只看watchlist来源——position/screen还在用7天回填窗口，跟这里
            # "次日核对"的口径混在一起算会把数字算错，见get_advice_accuracy
            # 的"按来源"拆分（tracker.py）。
            acc = acc_all.get("按来源", {}).get("watchlist", {"总数": 0})
        except Exception:
            acc = {"总数": 0}
        if acc.get("总数"):
            rate = acc["一致率"]
            st.caption(
                f"历史追踪：已回看 {acc['总数']} 次判断（次日按事后价格核对方向），一致率 {rate:.0f}%"
                "——这是历史记录的客观统计，不代表未来表现。"
            )
        else:
            st.caption("历史追踪：判断满1天后会自动回填实际价格算方向一致率，现在还没有满足条件的历史记录。")

        # 2026-08-26新增：按综合得分分档的事后收益复盘——参考TradingAgents项目
        # "结果驱动复盘日志"思路，见tracker.get_score_band_backtest的docstring。
        # 跟上面的方向一致率是两个不同维度：一致率只看买入/卖出方向对不对，
        # 这里看"分数越高是不是真的表现越好"，是排行榜打分体系本身可信度的
        # 直接检验，不能被上面那条一致率替代。
        try:
            bt = get_score_band_backtest(source="watchlist")
        except Exception:
            bt = {"total_reviewed": 0, "bands": []}
        if bt.get("total_reviewed"):
            band_lines = []
            for b in bt["bands"]:
                if b["count"] == 0:
                    continue
                if b["avg_return_pct"] is None:
                    band_lines.append(f"{b['band']}分：{b['count']}条（样本不足{bt['min_sample']}条，暂不计算）")
                else:
                    band_lines.append(f"{b['band']}分：{b['count']}条，平均涨跌{b['avg_return_pct']:+.1f}%，上涨占比{b['win_rate_pct']:.0f}%")
            st.caption(
                f"打分体系复盘：已回填 {bt['total_reviewed']} 条候选的事后价格，按综合得分分档统计——"
                + "；".join(band_lines)
                + "。这是检验\"分数是否真的有预测力\"的客观数据，不代表未来表现，样本量还小时数字会有波动。"
            )
        else:
            st.caption("打分体系复盘：综合得分是2026-08-26新加的字段，还没有满7天回填的历史记录，需要积累一段时间才有数据。")

    # 2026-08-25从"每个市场固定Top3"改成三市场混排的综合得分排行榜——用户
    # 明确要求数量不用锁死、好的自然上榜、某个市场这次没有靠谱标的就不必
    # 硬凑。单列纵向排布（不是并排的列），排行榜这种"有先后名次"的内容
    # 天然适合从上到下读，并排列反而弱化了排名信息。
    _market_label = {"US": "美股", "HK": "港股", "A": "A股"}
    board = data.get("leaderboard") or []
    if not board:
        st.caption("这一批还没有可排名的结果。")
        return
    for rank, row in enumerate(board, 1):
        market_key = row.get("market", "A")
        parts = _parse_advice_text(row.get("fundamental_verdict", ""))
        action = row.get("action", "观望")
        color = _ADVICE_ACTION_COLOR.get(action, NEUTRAL_COLOR)
        price = row.get("price_at_advice")
        price_text = f"{price:.2f}" if price else "—"
        score = row.get("score")
        href = (
            f"?open_symbol={urllib.parse.quote(row.get('symbol',''))}"
            f"&open_market={urllib.parse.quote(market_key)}"
            f"&open_name={urllib.parse.quote(row.get('name',''))}"
            f"{_auth_qs()}"
        )
        with st.container(border=True):
            st.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<span style='font-weight:700'>#{rank} {_esc(row.get('name',''))}"
                f"<span style='font-weight:400;color:var(--fa-muted);font-size:0.8rem'> · {_market_label.get(market_key, market_key)}</span></span>"
                f"<span style='display:flex;align-items:center;gap:6px'>"
                + (f"<span style='font-size:0.85rem;color:var(--fa-muted)'>{score}分</span>" if score is not None else "")
                + f"<span style='background:{color};color:#fff;border-radius:4px;padding:1px 8px;"
                f"font-size:0.8rem;font-weight:700'>{_esc(action)}</span></span></div>"
                f"<div style='font-size:0.75rem;color:var(--fa-muted)'>{_esc(row.get('symbol',''))} · 现价{price_text}"
                f"（置信度：{_esc(parts.get('置信度','—'))}）</div>"
                f"<div style='margin-top:6px'>{_esc(parts.get('理由', ''))}</div>"
                f"</a>",
                unsafe_allow_html=True,
            )
            with st.expander("基本面 / 技术面 / 价格位置"):
                for sec in ("基本面", "技术面", "价格位置"):
                    if parts.get(sec):
                        st.markdown(f"**{sec}**：{_esc(parts[sec])}")


@st.fragment
def _render_ai_assistant():
    """右下角"AI 咨询"悬浮按钮——不管在哪个分区都常驻显示，点开是个能聊天的
    浮窗，能看到用户自己的持仓/回看历史（登录后）+ 首页推荐股排行榜这类
    全站公开数据（游客也能看到），回答"这个网站怎么用/这个数字什么意思"
    这类问题不用用户自己去翻。

    实现思路：用st.popover而不是自己拿HTML/JS搭一个浮层——popover是
    Streamlit原生组件，不用操心sandboxed iframe/跨frame通信这些坑（这个
    项目在登录态那块已经踩过components.v1.html iframe的坑，见_BRIDGE_JS
    相关历史记录）。触发按钮本身用CSS钉在右下角固定位置——Streamlit给
    带key的组件容器自动加`st-key-<key>`这个class（持仓卡片搜索/添加按钮
    已经在用同一个技巧，见_render_position_rows附近的CSS），不用去猜
    Streamlit内部生成的DOM结构。

    2026-08-30补@st.fragment：之前这个函数没有独立fragment，发一条消息
    触发的st.chat_input提交会导致整个app.py从头重跑一遍——不管用户当时
    停在首页/行情/持仓哪个分区，那个分区自己的渲染（哪怕已经有缓存）也
    要重新跑一次DOM生成，再加上千问那次真实请求，用户反馈"消息发出去
    要好久才显示在网页上"，这是真正的原因，不是网络慢。包一个fragment，
    提交消息只重跑这个小组件自己，不牵连整个页面。跟_render_index_
    snapshot那次踩过的坑不是同一类——那次问题是run_every自动定时器
    切页后残留，这里只是交互触发重跑，不加run_every，不会有同类风险。
    """
    st.markdown(
        "<style>"
        ".st-key-ai_assistant_popover{position:fixed;bottom:24px;right:24px;z-index:9999;}"
        f".st-key-ai_assistant_popover button{{"
        f"border-radius:50%!important;width:68px;height:68px;padding:0!important;"
        f"background:{UP_COLOR}!important;border-color:{UP_COLOR}!important;"
        f"box-shadow:0 3px 16px rgba(0,0,0,0.35);font-weight:800;font-size:1.05rem;"
        f"}}"
        f".st-key-ai_assistant_popover button p{{color:#fff!important;font-size:1.05rem!important;font-weight:800!important;}}"
        "</style>",
        unsafe_allow_html=True,
    )
    with st.popover("AI", key="ai_assistant_popover"):
        st.markdown("**AI 咨询**")

        email = st.session_state.get("user_email") if st.session_state.get("logged_in") else None
        if "_assistant_messages" not in st.session_state:
            st.session_state["_assistant_messages"] = []
        # 上下文只在浮窗打开后第一次算，同一个会话里复用——持仓/历史记录
        # 这类数据变化不快，没必要每发一条消息都重新查一遍数据库；
        # 用户明确要求"重新开一次对话"才需要最新数据可以接受这个折衷。
        if "_assistant_context" not in st.session_state:
            # build_assistant_context内部已经给最慢的那几个数据源分别加了
            # 超时保护，但这里再兜一层调用方总超时——不指望"内部每处都想
            # 到了"，只要有任何一个环节（现在的或者以后新加的）意外没被
            # 内部保护到，这层total_timeout保证浮窗最多等15秒，不会无限
            # 卡住整个弹窗打不开。超时/失败都走同一个降级文案。
            try:
                _ctx_ex = ThreadPoolExecutor(max_workers=1)
                _ctx_fut = _ctx_ex.submit(build_assistant_context, email)
                st.session_state["_assistant_context"] = _ctx_fut.result(timeout=15)
                _ctx_ex.shutdown(wait=False)
            except Exception:
                st.session_state["_assistant_context"] = "（数据加载失败，先聊网站怎么用的问题）"

        # 不用st.chat_message——它自带的默认头像是个卡通小图标，用户明确要求
        # 整个网站不许出现任何emoji/装饰性图标，且要求跟GPT一致的"用户蓝气泡/
        # AI白气泡"经典聊天条样式，改成自己拼HTML气泡，不带任何头像。
        bubble_box = st.container(height=320)
        with bubble_box:
            if not st.session_state["_assistant_messages"]:
                # 空对话只是个输入框，用户不知道能问啥——加一句纯展示的
                # 引导语，不写进_assistant_messages（不进模型上下文，也不会
                # 被当成一轮真实对话历史发出去）。
                st.markdown(
                    _chat_bubble(
                        "assistant",
                        "可以问我：这支股票为什么打这个分、我的持仓现在要不要动、"
                        "推荐股排行榜准不准。",
                    ),
                    unsafe_allow_html=True,
                )
            for m in st.session_state["_assistant_messages"]:
                st.markdown(_chat_bubble(m["role"], m["content"]), unsafe_allow_html=True)

        prompt = st.chat_input("问点什么...")
        if prompt:
            st.session_state["_assistant_messages"].append({"role": "user", "content": prompt})
            with bubble_box:
                st.markdown(_chat_bubble("user", prompt), unsafe_allow_html=True)
                placeholder = st.empty()
                # 2026-08-30新增：先占位显示"正在想"的三点动画，再开始真正的
                # 请求——stream_reply现在会先做一次不流式的工具调用探测
                # （见assistant.py），实测这一步单独就要4-10秒，这段时间内
                # 原来占位符是空的，看起来跟卡死一样。这里让用户在等待的
                # 第一时间就看到"AI收到了、正在处理"，不是真的加速请求本身。
                placeholder.markdown(_typing_indicator_html(), unsafe_allow_html=True)
                reply = ""
                try:
                    for chunk in stream_assistant_reply(
                        st.session_state["_assistant_messages"], st.session_state["_assistant_context"],
                    ):
                        reply += chunk
                        placeholder.markdown(_chat_bubble("assistant", reply + " ▌"), unsafe_allow_html=True)
                    placeholder.markdown(_chat_bubble("assistant", reply), unsafe_allow_html=True)
                except Exception as e:
                    reply = f"回答失败：{e}"
                    placeholder.markdown(_chat_bubble("assistant", reply), unsafe_allow_html=True)
            st.session_state["_assistant_messages"].append({"role": "assistant", "content": reply})


def _render_home_page():
    """首页——世界地图（几个常见指数的实时点位）+ 今日重磅资讯。

    2026-08-30改用get_hot_market_news（富途新闻搜索，用当天真实异动股票
    当关键词种子）替代财新大盘资讯——用户反馈财新那条线"更新的好慢"，
    查过不是查询慢（接口本身1秒内返回），是财新这个源发布节奏本身偏慢
    （周刊/深度报道风格），不是分钟级快讯。富途这条线搜出来为空（比如
    今天没有股票明显异动、或者富途连不上）才退回财新兜底，不会完全没有
    内容可看。
    """
    st.markdown("**全球指数一览**")
    _render_home_map()

    st.divider()
    _render_advice_section()

    st.divider()
    st.markdown("**今日重磅消息**")
    try:
        news = get_hot_market_news()
    except Exception:
        news = None
    if news is None or news.empty:
        try:
            news = get_market_news()
        except Exception:
            news = None
    if news is None or news.empty:
        st.caption("暂时获取不到资讯。")
        return

    news = news.copy()
    # get_hot_market_news已经带了"日期"列（来自富途，跟财新的URL日期格式
    # 不一样），只有财新兜底那条路径（没有这一列）才需要从url正则提取。
    if "日期" not in news.columns:
        news["日期"] = news["url"].str.extract(r"/(\d{4}-\d{2}-\d{2})/")
    news = news.sort_values("日期", ascending=False, na_position="last")

    show_n = 30 if st.session_state.get("_home_news_expand") else 10
    _hot_names = _get_hot_stock_names()
    for _, row in news.head(show_n).iterrows():
        _title_color = _news_title_color(row["summary"], _hot_names)
        with st.container(border=True):
            st.markdown(
                f"<a href='{_safe_href(row['url'])}' target='_blank' style='color:{_title_color};text-decoration:none;font-weight:600'>{_esc(row['summary'])}</a>"
                f"<div style='font-size:0.75rem;color:var(--fa-muted);margin-top:2px'>{_esc(row.get('tag',''))} · {_esc(row.get('日期') or '-')}</div>",
                unsafe_allow_html=True,
            )
    if len(news) > 10:
        if not st.session_state.get("_home_news_expand"):
            if st.button("更多资讯", key="_home_news_more"):
                st.session_state["_home_news_expand"] = True
                st.rerun()
        else:
            if st.button("收起", key="_home_news_collapse"):
                st.session_state["_home_news_expand"] = False
                st.rerun()


def _render_stock_detail(symbol: str, market: str, name: str):
    # 之前这里还挂着 _inject_auto_refresh(30,...) 强制整页每30秒rerun一次——
    # 是_render_price_header改成@st.fragment(run_every=3)独立刷新之前的老
    # 机制，早就没被清理掉。K线数据(hist)本来就缓存在session_state[core_key]
    # 里、AI分析生成后也缓存，全页面rerun并不会让它们变得更"新"，只是白白把
    # 图表/AI文字这些开销大的部分每30秒重新渲染一次——这正是"网页卡卡的"的
    # 真实来源。价格的"活着的感觉"已经由下面的fragment用更轻量的方式做到了，
    # 删掉这个多余的整页定时rerun。
    st.markdown(
        "<style>"
        "[class*='st-key-detail_back_'] button p { font-size: 1.5rem !important; font-weight: 700; }"
        "</style>",
        unsafe_allow_html=True,
    )
    if st.button("←", key=f"detail_back_{symbol}_{market}", type="tertiary", help="返回持仓"):
        for k in ("_detail_symbol", "_detail_market", "_detail_name", "_detail_module"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "持仓"
        st.rerun()

    st.markdown(
        f"""
        <div style='background:{UP_COLOR};margin:-1rem -1rem 0 -1rem;padding:14px 24px'>
            <div style='color:#fff;font-size:1.2rem;font-weight:700'>{_esc(name)}</div>
            <div style='color:#fff;font-size:0.85rem;opacity:0.85'>{_esc(symbol)} · {_esc(market)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 快路径：只拉行情+实时价，不碰AI，先把图画出来
    core_key = f"_detail_core_{symbol}_{market}"
    if core_key not in st.session_state:
        with st.spinner("加载行情..."):
            try:
                end = cn_now().strftime("%Y%m%d")
                start = (cn_now() - timedelta(days=90)).strftime("%Y%m%d")
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

    _render_price_header(symbol, market)

    st.divider()
    period_labels = ["分时K（今日）", "日K", "周K", "月K"]
    period_label = st.radio("K线周期", period_labels, index=0, horizontal=True, key="_detail_kline_period")

    if market == "A" and period_label == "分时K（今日）":
        intraday = get_stock_intraday_a(symbol)
        if intraday.empty:
            st.caption("今天的分时数据暂时取不到，展示日K替代。")
            if hist is not None and not hist.empty:
                st.plotly_chart(build_candlestick(hist), use_container_width=True)
        else:
            st.plotly_chart(
                build_intraday_line(intraday, spot.get("昨收") if spot else None, market), use_container_width=True,
            )
    elif market == "A":
        period_options = {"日K": ("d", 90), "周K": ("w", 730), "月K": ("m", 1825)}
        freq, days_back = period_options[period_label]
        c_end = cn_now().strftime("%Y%m%d")
        c_start = (cn_now() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            chart_hist = get_stock_history(symbol, c_start, c_end, frequency=freq, market=market)
        except Exception:
            chart_hist = hist
        if chart_hist is not None and not chart_hist.empty:
            st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)
    elif period_label == "分时K（今日）":
        intraday = get_stock_intraday_futu(symbol, market)
        if intraday.empty:
            st.caption("分时数据需要本地 Futu OpenD 连接、且当前有实时推送，暂时展示日K替代。")
            if hist is not None and not hist.empty:
                st.plotly_chart(build_candlestick(hist), use_container_width=True)
        else:
            st.plotly_chart(
                build_intraday_line(intraday, spot.get("昨收") if spot else None, market), use_container_width=True,
            )
    else:
        chart_hist = get_stock_kline_futu(symbol, market, period_label)
        if chart_hist.empty:
            chart_hist = hist
            st.caption("该周期需要本地 Futu OpenD 连接，当前展示日K替代。")
        if chart_hist is not None and not chart_hist.empty:
            st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)

    # 手机上进详情页第一屏默认只有图表这一块——之前新闻/AI分析这些小组件
    # 全部一起加载，手机上往下滑浏览的时候很容易手滑碰到中间那些按钮
    # （K线周期切换、重新分析这些）造成误触。改成默认收起，只留一个
    # "浏览更多"按钮，点了才展开新闻和AI分析部分，第一屏能滑动的可交互
    # 元素少很多，不容易碰错。
    expand_key = f"_detail_expand_{symbol}_{market}"
    st.divider()
    if not st.session_state.get(expand_key):
        if st.button("浏览更多（新闻 · AI 分析）", key=f"_detail_expand_btn_{symbol}_{market}", use_container_width=True):
            st.session_state[expand_key] = True
            st.rerun()
        return
    if st.button("收起", key=f"_detail_collapse_btn_{symbol}_{market}"):
        st.session_state[expand_key] = False
        st.rerun()

    _stock_name_for_news = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
    _render_news_section(_stock_name_for_news, symbol=symbol, market=market)

    st.divider()
    _head_col, _refresh_col = st.columns([5, 1])
    _head_col.subheader("AI 深度分析")
    st.caption(
        "打开详情页自动生成，多个独立 AI 调用分别交叉验证新闻、财务、大盘对比、"
        "技术面与消息面是否一致——只呈现数据和依据，不给买卖建议，请自行判断。"
        "价格是每 3 秒跳动的实时数据，但AI文字分析生成一次就缓存住，不会跟着"
        "价格自动重新生成（每次都调用AI要花钱），盘中变化大的话可以点右上角"
        "「重新分析」手动刷新。"
    )
    module_defs = (
        ("news", "资讯解读"), ("financial", "财务摘要"), ("benchmark", "对比大盘"), ("cross", "综合数据分析（交叉验证）"),
    )
    summary_key = f"_detail_summary_{symbol}_{market}"
    if _refresh_col.button("重新分析", key=f"_reanalyze_{symbol}_{market}", use_container_width=True):
        for mod_key, _ in module_defs:
            st.session_state.pop(f"_detail_mod_{symbol}_{market}_{mod_key}", None)
        st.session_state.pop(summary_key, None)
        st.rerun()

    for mod_key, mod_label in module_defs:
        with st.container(border=True):
            st.markdown(f"**{mod_label}**")
            _render_module(mod_key, symbol, market, hist, spot)
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if summary_key not in st.session_state:
            try:
                section_texts = {
                    mod_label: st.session_state.get(f"_detail_mod_{symbol}_{market}_{mod_key}", {}).get("ai_text", "")
                    for mod_key, mod_label in module_defs
                }
                st.session_state[summary_key] = _stream_ai_text(
                    summarize_overall(symbol, section_texts), raise_on_error=False,
                )
                # 2026-08-26新增：这个分数原来现算现扔，从来没被存过，没法
                # 回溯验证准不准——补记到cross模块那次log_analysis刚插入的
                # 那条记录上（游客模式不落库，跟cross模块的log_analysis同一个
                # 判断条件）。
                if st.session_state.get("logged_in"):
                    _overall_score = extract_score(st.session_state[summary_key])
                    if _overall_score is not None:
                        record_overall_score(st.session_state["user_email"], symbol, market, _overall_score)
            except Exception as e:
                st.session_state[summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[summary_key])


def _render_index_detail(name: str, code: str, market: str):
    # 同样的原因删掉了_inject_auto_refresh，见_render_stock_detail开头的注释。
    st.markdown(
        "<style>"
        "[class*='st-key-idx_back_'] button p { font-size: 1.5rem !important; font-weight: 700; }"
        "</style>",
        unsafe_allow_html=True,
    )
    if st.button("←", key=f"idx_back_{code}_{market}", type="tertiary", help="返回行情"):
        for k in ("_index_detail_code", "_index_detail_market", "_index_detail_name"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "行情"
        st.rerun()

    st.markdown(
        f"""
        <div style='background:{UP_COLOR};margin:-1rem -1rem 0 -1rem;padding:14px 24px'>
            <div style='color:#fff;font-size:1.2rem;font-weight:700'>{_esc(name)}</div>
            <div style='color:#fff;font-size:0.85rem;opacity:0.85'>{_esc(code)} · {_esc(market)}指数</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        idx_snap = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
    except Exception:
        idx_snap = None

    _render_index_price_header(name, market)

    st.divider()
    period_label = st.radio(
        "K线周期", ["分时K（今日）", "日K", "周K", "月K"], index=0, horizontal=True, key="_idx_kline_period",
    )

    base_price = idx_snap.get("最新") - idx_snap.get("涨跌") if idx_snap else None

    if period_label == "分时K（今日）":
        intraday = get_index_intraday_a(code) if market == "A" else get_index_intraday_futu(name, market, base_price)
        if intraday.empty:
            st.caption("今天的分时数据暂时取不到，展示日K替代。")
            try:
                chart_hist = get_index_history(code, market, "日K")
            except Exception:
                chart_hist = None
            if chart_hist is not None and not chart_hist.empty:
                st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)
        else:
            st.plotly_chart(
                build_intraday_line(intraday, base_price, market), use_container_width=True,
            )
    else:
        try:
            chart_hist = get_index_history(code, market, period_label)
        except Exception as e:
            chart_hist = None
            st.error(f"K线加载失败：{e}")
        if chart_hist is not None and not chart_hist.empty:
            st.plotly_chart(build_candlestick(chart_hist), use_container_width=True)

    idx_expand_key = f"_idx_expand_{code}_{market}"
    st.divider()
    if not st.session_state.get(idx_expand_key):
        if st.button("浏览更多（成分股 · 资讯 · AI 分析）", key=f"_idx_expand_btn_{code}_{market}", use_container_width=True):
            st.session_state[idx_expand_key] = True
            st.rerun()
        return
    if st.button("收起", key=f"_idx_collapse_btn_{code}_{market}"):
        st.session_state[idx_expand_key] = False
        st.rerun()

    st.subheader("成分股")
    _render_index_top_movers(market, index_name=name)

    st.divider()
    _render_news_section(name, is_index=True)

    st.divider()
    _idx_head_col, _idx_refresh_col = st.columns([5, 1])
    _idx_head_col.subheader("AI 深度分析")
    st.caption(
        "打开详情页自动生成，结合技术面信号和相关资讯做交叉验证——只呈现依据，不给操作建议。"
        "价格是实时跳动的，AI文字分析生成一次就缓存住，需要的话点右上角「重新分析」手动刷新。"
    )

    idx_ai_key = f"_idx_analysis_{code}_{market}"
    if _idx_refresh_col.button("重新分析", key=f"_idx_reanalyze_{code}_{market}", use_container_width=True):
        for _suffix in ("_news", "_cross", "_summary"):
            st.session_state.pop(f"{idx_ai_key}{_suffix}", None)
        st.rerun()
    _idx_news_fresh = f"{idx_ai_key}_news" not in st.session_state
    with st.container(border=True):
        st.markdown("**资讯解读**")
        if _idx_news_fresh:
            try:
                news, _ = get_index_news(name, limit=8)
                news_summary = _news_to_summary(news)
                ai_text = _stream_ai_text(summarize_index_news(name, news_summary))
                st.session_state[f"{idx_ai_key}_news"] = {"ai_text": ai_text, "summary": news_summary}
            except Exception as e:
                st.session_state[f"{idx_ai_key}_news"] = {"ai_text": f"获取失败：{e}", "summary": "无相关新闻"}
        else:
            st.markdown(st.session_state[f"{idx_ai_key}_news"]["ai_text"])

    _idx_cross_fresh = f"{idx_ai_key}_cross" not in st.session_state
    with st.container(border=True):
        st.markdown("**综合数据分析**")
        daily_hist = get_index_history(code, market, "日K")
        has_hist = daily_hist is not None and not daily_hist.empty
        technical_summary = compute_technical_signal(daily_hist) if has_hist else "暂无技术面数据"
        stats = compute_stats(daily_hist) if has_hist and len(daily_hist) > 5 else {}

        try:
            _idx_snap_now = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
            if _idx_snap_now:
                _idx_spot = {
                    "最新价": _idx_snap_now["最新"],
                    "昨收": _idx_snap_now["最新"] - _idx_snap_now["涨跌"],
                }
                _idx_intraday = (
                    get_index_intraday_a(code) if market == "A"
                    else get_index_intraday_futu(name, market, _idx_spot["昨收"])
                )
            else:
                _idx_spot, _idx_intraday = {}, None
        except Exception:
            _idx_spot, _idx_intraday = {}, None
        realtime_signal = compute_realtime_signal(_idx_spot, _idx_intraday)
        technical_summary += " 【盘中实时信号】" + realtime_signal

        if stats:
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", stats.get("区间收益率", "—"))
            scol2.metric("年化波动率", stats.get("年化波动率", "—"))
            scol3.metric("最大回撤", stats.get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", stats.get("夏普比率(简化)", "—"))
        st.markdown(f"**技术面信号**：{technical_summary}")
        if has_hist:
            st.plotly_chart(build_return_histogram(daily_hist), use_container_width=True)

        st.caption("AI 解读")
        if _idx_cross_fresh:
            news_summary = st.session_state.get(f"{idx_ai_key}_news", {}).get("summary", "无相关新闻")
            # 这里之前失败时会 `return`——是函数级别的return，会直接跳出整个
            # _render_index_detail，导致下面的"总结性分析"区块整个不渲染
            # （跟个股详情页 _render_module 不一样：那边每个模块是独立函数
            # 调用，一个模块内部return只影响它自己，不影响后续模块）。这里
            # 改成跟上面"资讯解读"模块一样的写法——异常时把错误信息当成
            # 这次的展示文本存进缓存，不再 return，让后面的区块正常渲染。
            try:
                ai_text = _stream_ai_text(analyze_index(name, technical_summary, news_summary))
                st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": ai_text}
            except Exception as e:
                ai_text = f"分析失败：{e}"
                st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": ai_text}
                st.error(ai_text)
        else:
            st.markdown(st.session_state[f"{idx_ai_key}_cross"]["ai_text"])

    idx_summary_key = f"{idx_ai_key}_summary"
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if idx_summary_key not in st.session_state:
            try:
                section_texts = {
                    "资讯解读": st.session_state.get(f"{idx_ai_key}_news", {}).get("ai_text", ""),
                    "综合数据分析": st.session_state.get(f"{idx_ai_key}_cross", {}).get("ai_text", ""),
                }
                st.session_state[idx_summary_key] = _stream_ai_text(
                    summarize_overall(name, section_texts), raise_on_error=False,
                )
            except Exception as e:
                st.session_state[idx_summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[idx_summary_key])


@st.fragment(run_every=3)
def _render_positions_today_pnl(positions: list):
    """今日收益，要求实时同步——跟_render_position_rows一样每3秒刷新。
    get_stock_realtime本身@st.cache_data(ttl=3)，跟这个fragment的刷新节奏
    对齐，同一只股票3秒内被两个fragment各查一次实际只打一次真实请求，
    第二次直接命中缓存，不算"额外请求"。只用最新价/昨收这两个已有字段
    算涨跌额，不需要新接口。汇率/行情任一失败就跳过那一支，跳过的部分
    在展示里如实说明，不拿旧数字硬凑。
    """
    holding_items = [w for w in positions if (w.get("shares") or 0) > 0]
    if not holding_items:
        st.caption("今日收益：暂无真实持仓。")
        return

    def _fetch_today(item):
        symbol, market = item["symbol"], item.get("market", "A")
        try:
            spot = get_stock_realtime(symbol, market=market)
        except Exception:
            spot = {}
        price, prev_close = spot.get("最新价"), spot.get("昨收")
        if not price or not prev_close:
            return None
        pnl_native = (price - prev_close) * item["shares"]
        pnl_cny, _note = to_cny(pnl_native, item.get("currency", "CNY"))
        value_cny, _note2 = to_cny(price * item["shares"], item.get("currency", "CNY"))
        if pnl_cny is None or value_cny is None:
            return None
        # 场外基金/上金所现货走的是T-1净值兜底(见get_stock_realtime里的
        # _fetch_otc_fund_quote/_fetch_sge_spot_quote)，"最新价/昨收"字段
        # 形状跟真实实时行情一样，但含义是"上一个披露日 vs 再前一日"，不是
        # "今天 vs 昨天"——直接汇总进这个每3秒刷新的"今日收益"会让隔夜没
        # 更新的净值变化看起来像是实时跳动的盈亏，是真实的误导，不是无害的
        # 显示细节，必须单独标记出来。
        is_stale = str(spot.get("数据源", "")).endswith("(T-1)")
        return pnl_cny, value_cny, is_stale

    results = _run_concurrent_with_deadline(holding_items, _fetch_today, timeout=6)
    total_pnl, total_value, skipped, stale_count = 0.0, 0.0, 0, 0
    for i in range(len(holding_items)):
        r = results.get(i)
        if r is None:
            skipped += 1
            continue
        total_pnl += r[0]
        total_value += r[1]
        if r[2]:
            stale_count += 1

    if total_value <= 0:
        st.caption("今日收益：行情/汇率暂时都获取不到，稍后重试。")
        return

    pnl_pct = total_pnl / (total_value - total_pnl) * 100 if (total_value - total_pnl) else 0
    pnl_color = UP_COLOR if total_pnl >= 0 else DOWN_COLOR
    st.markdown(
        f"<div style='font-size:0.8rem;color:var(--fa-muted)'>今日收益</div>"
        f"<div style='font-size:1.6rem;font-weight:700;color:{pnl_color}'>"
        f"{total_pnl:+,.0f} <span style='font-size:1rem'>（{pnl_pct:+.2f}%）</span></div>",
        unsafe_allow_html=True,
    )
    if skipped:
        st.caption(f"有 {skipped} 支持仓因行情/汇率暂时获取不到，未计入。")
    if stale_count:
        st.caption(f"其中 {stale_count} 支场外基金/贵金属现货用的是上一披露日净值（非实时），已计入合计。")


def _render_max_capital_input(email: str):
    """最大资金投入量（折人民币，手动设定）——用户明确要求"AI要知道我们
    总共有多少钱，不能盲目加仓"，advise_portfolio只看得到已经买了多少，
    看不到用户自己心里的资金上限，这里让用户手动设一下，写进
    user_settings表，advise_portfolio读取后能算"还剩多少额度"。不设是
    合法状态(None)，不强制填。"""
    current = get_max_capital(email)
    new_value = st.number_input(
        "最大资金投入量（¥，AI组合分析会参考，不填则不限制）",
        min_value=0.0, value=float(current) if current else 0.0, step=1000.0,
        key=f"_max_capital_input_{email}",
    )
    if st.button("保存", key=f"_max_capital_save_{email}", use_container_width=True):
        set_max_capital(email, new_value if new_value > 0 else None)
        st.success("已保存。")


@st.fragment(run_every=15)
def _render_ai_sim_live_snapshot(email: str, equity_points: list):
    """AI模拟盘的现金/持仓市值/浮盈浮亏这部分单独抽出来自动刷新——用户
    反馈"持仓收益好久没更新"，查证后发现_render_ai_sim_dashboard本身没有
    任何自动刷新机制，只有用户手动触发rerun（切tab/点按钮）才会重新查询
    Futu账户，之前看到的数字不是错的（跟当时的实时行情核对过是对的），
    只是页面不会自己动。这里用run_every=15秒刷新——比持仓页真实持仓那个
    3秒刷新（_render_position_rows）更保守，因为这里每次刷新要建HK+US
    两个市场的Futu交易连接，比单纯查行情更重，没必要跟那边一样逐秒刷新。
    历史决策记录/下单记录/图表不放在这个fragment里——那些只有AI每15分钟
    跑一次才会变，没必要跟着这里一起抖。
    """
    with st.spinner("读取模拟盘状态..."):
        try:
            snapshot = sim_trader.get_agent_snapshot()
        except Exception as e:
            st.error(f"模拟盘状态读取失败：{e}")
            snapshot = None

    if not snapshot:
        return

    # 虚拟现金和持仓市值分开展示，不只给一个合并数字——用户明确要求
    # 能看清"钱花出去多少变成了股票、还剩多少现金"，不是只有一个净值
    # 黑箱数字。总额(=两者之和)标注清楚"十万港币起始"这个基准，这个
    # 基准数字本身不会变，变的是净值相对它涨跌了多少。
    holdings_value = snapshot["holdings_value_hkd"]
    virtual_cash = get_sim_virtual_cash(email)
    if virtual_cash is None:
        virtual_cash = sim_agent._VIRTUAL_BUDGET_HKD
    net_value = holdings_value + virtual_cash

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("虚拟现金（剩余可用）", f"HK${virtual_cash:,.0f}")
    with col2:
        st.metric("持仓市值", f"HK${holdings_value:,.0f}")
    with col3:
        st.metric("总额（起始十万港币）", f"HK${net_value:,.0f}")

    if equity_points:
        first = min(equity_points, key=lambda p: p["run_at"])["assets_hkd"]
        if first:
            change_pct = (net_value - first) / first * 100
            st.metric("累计收益率（相对第一次记录）", f"{change_pct:+.2f}%")
    if snapshot["skipped_markets"]:
        st.caption(f"以下市场暂时没查到模拟账户：{'、'.join(snapshot['skipped_markets'])}")

    # 本日/昨日/本月收益——用户明确要求这三个分开的时间窗口，各自带一个
    # 收益百分比，负收益就是负数（不用红绿颜色掩盖，数字本身带符号最直接）。
    # 找不到某个窗口的数据（比如AI今天/本月还没运行过）时如实显示"暂无
    # 数据"，不拿0冒充"没有变化"。
    pnl = get_period_pnl(email, net_value)
    pnl_col1, pnl_col2, pnl_col3 = st.columns(3)
    for col, label, key in ((pnl_col1, "本日收益", "today"), (pnl_col2, "昨日收益", "yesterday"), (pnl_col3, "本月收益", "month")):
        block = pnl.get(key)
        with col:
            if block:
                st.metric(label, f"HK${block['change']:+,.0f}", f"{block['pct']:+.2f}%")
            else:
                st.metric(label, "暂无数据")

    st.caption("每15秒自动刷新")
    if snapshot["positions"]:
        st.markdown("**当前持仓**")
        for p in snapshot["positions"]:
            pl_color = UP_COLOR if (p["pl_val"] or 0) >= 0 else DOWN_COLOR
            pl_text = f"{p['pl_val']:+,.0f} {p['currency']}" if p["pl_val"] is not None else "—"
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;padding:4px 0'>"
                f"<span>{_esc(p['name'])}（{_esc(p['code'])}）· {p['qty']:g}股</span>"
                f"<span style='color:{pl_color}'>{pl_text}</span></div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("当前空仓。")


def _render_ai_sim_dashboard(email: str):
    """回看页——2026-09-01用户明确要求"回看页全部改成AI模拟炒股，我需要
    看到它的持仓、收益和相关的所有交易记录"，完全取代原来的AI判断准确率
    追踪（_render_accuracy_dashboard函数还留着，只是导航不再调用它——
    用户是要"完全替换"这个入口，不是要删掉底层历史数据，函数和数据表都
    保留，只是不在这个入口展示）。

    展示的是sim_agent.py那条每15分钟一次的自主决策链路（只交易港股/美股，
    A股不参与，起始本金约十万港币——由用户自己在富途App里设置，这里没法
    通过代码改），不是持仓页那个"跟着每天17:30组合分析走"的模拟盘（那个
    继续在持仓页自己的开关那块，两条链路各自独立）。
    """
    if not st.session_state.get("logged_in"):
        st.write("")
        _, mid_empty, _ = st.columns([1, 2, 1])
        with mid_empty:
            st.markdown(
                "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                "回看是个人功能，需要登录后使用<br>"
                "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                "</div>",
                unsafe_allow_html=True,
            )
            if st.button("登录 / 注册", use_container_width=True, key="_review_page_login_btn"):
                st.session_state["guest_mode"] = False
                st.rerun()
        return

    st.caption(
        "这是内置AI（千问）用虚拟资金自主管理的模拟盘——只交易港股/美股（A股不参与），"
        "起始本金约十万港币，在开盘时段每15分钟自主决定要不要买卖，不需要你手动操作。"
        "这里如实展示它的持仓、收益和完整交易记录，仅供观察AI决策能力，不构成投资建议。"
    )

    enabled = get_sim_agent_enabled(email)
    new_enabled = st.toggle("AI自主模拟交易", value=enabled, key=f"_ai_sim_dash_toggle_{email}")
    if new_enabled != enabled:
        set_sim_agent_enabled(email, new_enabled)
        st.rerun()
    if not enabled:
        st.caption("当前关闭——打开后AI会在下一个港股/美股开盘的15分钟节点开始自主交易。")
        return

    runs = get_sim_agent_runs(email, limit=30)

    # 走势图数据源用sim_equity_snapshots(每几分钟一次，跟AI决策频率解耦)，
    # 不再用sim_agent_runs的决策快照(15分钟一次)——用户反馈"遇到低波动
    # 持仓连续几次数字精确不变，图表看着像死了"，见sim_snapshot.py开头
    # 的说明。"AI每次决策记录"那个列表(下面)继续用runs，两者互不影响。
    snapshots = get_equity_snapshots(email, limit=500)
    equity_points = []
    for s in snapshots:
        _dt = _to_cn_dt(s.get("snapshot_at"))
        if _dt is None:
            continue
        equity_points.append({"run_at": _dt, "assets_hkd": s["net_value_hkd"]})

    _render_ai_sim_live_snapshot(email, equity_points)

    if len(equity_points) >= 2:
        # 用户明确要求走势图分天/周/月三种模式看——快照本身就只在开盘时段
        # 才记（sim_snapshot.py没开盘直接跳过不落库），非交易日/非开盘时段
        # 天然没有数据点，不用额外过滤"非交易日"。
        _view_key = f"_ai_sim_chart_view_{email}"
        view = st.radio(
            "走势图范围", ["天", "周", "月"], horizontal=True,
            key=_view_key, label_visibility="collapsed",
        )
        now_cn = datetime.now(timezone(timedelta(hours=8)))
        if view == "天":
            latest_date = max(p["run_at"].date() for p in equity_points)
            windowed = [p for p in equity_points if p["run_at"].date() == latest_date]
            granularity = "day"
        elif view == "周":
            cutoff = now_cn - timedelta(days=7)
            windowed = [p for p in equity_points if p["run_at"] >= cutoff]
            granularity = "week"
        else:
            cutoff = now_cn - timedelta(days=30)
            windowed = [p for p in equity_points if p["run_at"] >= cutoff]
            granularity = "month"
        if len(windowed) >= 2:
            st.plotly_chart(
                build_sim_equity_curve(windowed, baseline=sim_agent._VIRTUAL_BUDGET_HKD, granularity=granularity),
                use_container_width=True,
            )
        else:
            st.caption(f"「{view}」这个范围内数据点还不够画线——换个更大的范围看看，或者等AI多跑几轮。")
    else:
        st.caption("收益曲线数据还在积累——AI每次运行会记一个资产快照点，多跑几次（开盘时段每15分钟一次）后这里会出现走势图。")

    st.divider()
    st.markdown("**AI每次决策记录**")
    if not runs:
        st.caption("还没有运行记录——开盘时段每15分钟会自动跑一次。")
    else:
        _runs_key = f"_ai_sim_runs_show_all_{email}"
        show_all_runs = st.session_state.get(_runs_key, False)
        visible_runs = runs if show_all_runs else runs[:5]
        for r in visible_runs:
            when = _to_cn_time_str(r.get("run_at"))
            title = f"{when} · {r['status']}" + (f" · {r['note']}" if r.get("note") else "")
            with st.expander(title):
                if r.get("reasoning_text"):
                    st.markdown(_esc(r["reasoning_text"]).replace("\n", "<br>"), unsafe_allow_html=True)
                try:
                    sigs = json.loads(r.get("signals_json") or "[]")
                except Exception:
                    sigs = []
                actionable = [s for s in sigs if s.get("action") in ("买入", "卖出")]
                for s in actionable:
                    st.caption(f"{s['action']} {s['name']}（{s['symbol']}·{s['market']}）{s['shares']:g}股")
        if not show_all_runs and len(runs) > 5:
            if st.button(f"更多（最近{len(runs)}条）", key=f"_ai_sim_runs_more_{email}"):
                st.session_state[_runs_key] = True
                st.rerun()

    st.divider()
    st.markdown("**完整下单记录**")
    orders = get_simulated_orders(email, limit=50)
    if not orders:
        st.caption("还没有下单记录。")
    else:
        _orders_key = f"_ai_sim_orders_show_all_{email}"
        show_all_orders = st.session_state.get(_orders_key, False)
        visible_orders = orders if show_all_orders else orders[:10]
        for o in visible_orders:
            status_color = {"成功": UP_COLOR, "失败": DOWN_COLOR, "跳过": NEUTRAL_COLOR}.get(o["status"], NEUTRAL_COLOR)
            st.markdown(
                f"<div style='padding:4px 0'>{_to_cn_time_str(o['created_at'])} · "
                f"{_esc(o['name'] or o['symbol'])}（{_esc(o['symbol'])}·{_esc(o['market'])}）· {_esc(o['action'])} · "
                f"<span style='color:{status_color}'>{_esc(o['status'])}</span>"
                + (f" · {_esc(o['note'])}" if o["note"] else "") + "</div>",
                unsafe_allow_html=True,
            )
        if not show_all_orders and len(orders) > 10:
            if st.button(f"更多（最近{len(orders)}条）", key=f"_ai_sim_orders_more_{email}"):
                st.session_state[_orders_key] = True
                st.rerun()


def _render_positions_donut(positions: list):
    """持仓占比环形图。只统计真正持仓(shares>0)，纯关注(shares=0)不占份额。
    不用@st.fragment(run_every=3)——Plotly图3秒重绘会明显闪烁（见持仓分析
    方案里的踩坑记录），这块跟着页面正常rerun刷新就够了，不需要独立3秒轮询。
    汇率/实时价任何一项失败就跳过那一支（不拿0或旧数字硬凑），并在图下方
    如实提示"部分持仓因数据获取失败未计入"，不悄悄编一个不准的总资产出来。
    """
    holding_items = [w for w in positions if (w.get("shares") or 0) > 0]
    if not holding_items:
        if positions:
            st.caption("当前只有关注项、暂无真实持仓（关注项不占份额）——给关注项填股数会计入占比图。")
        else:
            st.caption("暂无真实持仓——添加持仓时填股数才会计入占比图。")
        return

    def _fetch_value(item):
        symbol, market = item["symbol"], item.get("market", "A")
        try:
            spot = get_stock_realtime(symbol, market=market)
            price = spot.get("最新价") if spot else None
        except Exception:
            price = None
        if not price:
            return None
        value_cny, _note = to_cny(item["shares"] * price, item.get("currency", "CNY"))
        return value_cny

    results = _run_concurrent_with_deadline(holding_items, _fetch_value, timeout=6)

    holdings, skipped = [], 0
    for i, item in enumerate(holding_items):
        value_cny = results.get(i)
        if value_cny is None:
            skipped += 1
            continue
        holdings.append({"label": f"{item['name']}（{item['symbol']}）", "value_cny": value_cny})

    if not holdings:
        st.caption("行情/汇率暂时都获取不到，稍后重试。")
        return

    holdings.sort(key=lambda h: h["value_cny"], reverse=True)
    total_value_cny = sum(h["value_cny"] for h in holdings)
    st.plotly_chart(build_position_donut(holdings, total_value_cny), use_container_width=True, key="_positions_donut")
    if skipped:
        st.caption(f"有 {skipped} 支持仓因行情/汇率暂时获取不到，未计入本图。")


_PORTFOLIO_REANALYZE_COOLDOWN = 300  # 5分钟节流——组合分析是1次真实AI调用，不是纯本地计算，不能让用户点着玩


def _render_bold_as_red(text: str) -> str:
    """把AI分析文本里的**加粗**改成红色高亮——用户明确要求"重点标红"。AI
    在_PORTFOLIO_SYSTEM里已经被要求用**加粗**标关键结论，复用这个已有的
    标记习惯改渲染方式，不用再发明新的自定义标记语法。非加粗部分照常转义
    （AI生成文本理论上不该有恶意内容，但统一走_esc()是这个项目一贯的
    习惯，不因为"来源可信"就破例）。"""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    html_parts = []
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            html_parts.append(f"<span style='color:{UP_COLOR};font-weight:700'>{_esc(part[2:-2])}</span>")
        else:
            html_parts.append(_esc(part).replace("\n", "<br>"))
    return "".join(html_parts)


def _render_trade_signals(signals_json: str):
    """结构化交易信号——用户明确要求"先只调研+搭好架子，不接真实下单"：
    这里只是把advisor.py解析好的信号(标的/方向/股数/金额)展示成一张能
    直接照着操作的表，不调用富途交易接口(OpenSecTradeContext/place_order)，
    下单动作还是用户自己去券商完成。红涨绿跌配色跟买卖方向复用同一套
    UP_COLOR/DOWN_COLOR（买入用涨色、卖出用跌色，跟这个项目一贯的红绿
    约定保持一致，不是另外发明一套买卖配色）。
    """
    try:
        signals = json.loads(signals_json) if signals_json else []
    except (json.JSONDecodeError, TypeError):
        signals = []
    if not signals:
        return
    action_signals = [s for s in signals if s.get("action") != "不动"]
    if not action_signals:
        st.caption("本次信号：全部维持不动，没有需要操作的标的。")
        return
    st.caption("交易信号（仅供参考，需要你自己去券商手动下单，不会自动执行）")
    for s in action_signals:
        color = UP_COLOR if s["action"] == "买入" else DOWN_COLOR
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"padding:6px 10px;margin:4px 0;border-radius:6px;background:var(--fa-card-bg,rgba(0,0,0,.03))'>"
            f"<span>{_esc(s['name'])}（{_esc(s['symbol'])}·{_esc(s['market'])}）</span>"
            f"<span style='color:{color};font-weight:700'>"
            f"{_esc(s['action'])} {s['shares']:g}股 · 约¥{s['amount_cny']:,.0f}</span></div>",
            unsafe_allow_html=True,
        )


def _render_portfolio_advice(email: str, positions: list):
    """AI组合分析卡片——跟左边"今日收益"不同，这块不是实时刷新的（AI调用
    有成本，不能每3秒跑一次），只读advisor.py（每工作日17:30跑）写进
    portfolio_advice表的最近一次结果，另外给一个"立即重新分析"按钮供用户
    现场触发（组合分析只有1次AI调用，跟单支判断动辄几十次不一样，现场跑
    得起），加5分钟节流防止连续点击刷爆DeepSeek账户。
    """
    holding_count = sum(1 for p in positions if (p.get("shares") or 0) > 0)
    advice = get_latest_portfolio_advice(email)

    if advice:
        created = advice["created_at"][:19].replace("T", " ")
        st.caption(f"更新于 {created}（UTC）")

        # 持仓变化检测——2026-08-26真实复现过的bug：用户已经清仓腾讯，但
        # 这张卡片还在展示几天前生成的分析，文字里具体写着"减仓腾讯40股"，
        # 用户如果不细看时间戳很容易误以为这是当前建议。这份分析里的股数/
        # 金额是基于生成那一刻的持仓快照(holdings_json)算的，持仓一旦变化
        # （加仓/减仓/清仓）这些具体数字就直接过期了——拿当前真实持仓的
        # symbol集合跟落库时的快照比对，不一致就强提醒，不能沉默展示。
        try:
            snapshot_symbols = {h["symbol"] for h in json.loads(advice.get("holdings_json") or "[]")}
        except Exception:
            snapshot_symbols = set()
        current_symbols = {p["symbol"] for p in positions if (p.get("shares") or 0) > 0}
        if snapshot_symbols and snapshot_symbols != current_symbols:
            st.warning(
                "你的持仓自这份分析生成后已经变化（加仓/减仓/清仓），"
                "下面提到的具体股数/金额操作建议很可能已经过期，不要直接照做——"
                "建议先看一眼下方持仓列表，再点击下方「立即重新分析」刷新。"
            )

        _render_trade_signals(advice.get("signals_json", ""))
        st.markdown(_render_bold_as_red(advice["analysis_text"]), unsafe_allow_html=True)
    else:
        st.caption("AI 组合分析还没生成过。")

    if holding_count < 2:
        st.caption("持仓不足2支时集中度分析意义不大，暂不生成（1支必然占比100%）。")
        return

    throttle_key = f"_portfolio_advice_last_{email}"
    elapsed = time.time() - st.session_state.get(throttle_key, 0)
    if elapsed < _PORTFOLIO_REANALYZE_COOLDOWN:
        st.button(f"请稍后再试（{int(_PORTFOLIO_REANALYZE_COOLDOWN - elapsed)}秒冷却）", disabled=True, use_container_width=True)
        return

    if st.button("立即重新分析", key=f"_portfolio_reanalyze_{email}", use_container_width=True):
        st.session_state[throttle_key] = time.time()
        with st.spinner("AI 正在分析组合……（要逐支查行情/新闻+推理+生成交易信号，大约1-3分钟）"):
            try:
                import advisor
                advisor._load_secrets_into_env()
                result = advisor.advise_portfolio(email)
            except Exception as e:
                st.error(f"分析失败：{e}")
                return
        if result is None:
            st.warning("持仓不足2支，暂不生成组合分析。")
        else:
            st.rerun()


@st.fragment(run_every=3)
def _render_position_rows(position_items: list, _email: str):
    """持仓列表本体单独做成 fragment，价格/涨跌幅每3秒自己刷新，效仿长桥的
    紧凑列表样式：名称代码 + 迷你走势图 + 现价/成交额 + 涨跌幅色块 + 删除键。
    数字真变了背景闪一下（复用详情页那套red/green flash动画）。每行用
    st.container(border=True)包起来，整行都是一个卡片。

    卡片点击跳转：试过两版JS/CSS方案（覆盖层、DOM遍历绑事件）在真实浏览器里
    都点不动，大概率是猜的Streamlit内部结构不对。这版换成最朴素可靠的办法——
    整块卡片内容包在一个真正的<a href="?open_symbol=...">链接里，点击就是
    标准浏览器导航，不依赖任何猜测。URL参数在脚本最开头统一处理（见文件靠前
    的 st.query_params 检查）。删除键单独放在旁边一个真正的 st.button，
    跟这个<a>标签是两个独立的DOM元素，互不干扰。
    """
    if not position_items:
        st.caption("这个分类下暂时没有持仓。")
        return

    st.caption("每 3 秒自动刷新")

    st.markdown(
        _PRICE_FLASH_CSS
        + "<style>"
        # 浏览器默认的 a:link/a:visited 样式（蓝色+下划线）选择器带伪类，
        # 优先级比单纯的class选择器高，必须用!important才能真正覆盖掉。
        + "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        + "  text-decoration: none !important; color: inherit !important;"
        + "  display: block; cursor: pointer;"
        + "}"
        + "a.pos-card-link:hover { opacity: 0.85; }"
        # 删除键用type="tertiary"，图标本身默认偏小，用户反馈要大一点、位置要
        # 跟卡片内容对齐。垂直对齐交给st.columns自己的vertical_alignment="center"
        # 处理（原生机制，比猜CSS高度靠谱）。默认按钮是圆角矩形/胶囊形，用户
        # 反馈这个和"对比/搜索"图标按钮一样改成正圆——固定等宽高+50%圆角。
        + "[class*='st-key-pos_del_'] button p { font-size: 1.5rem !important; font-weight: 700; margin: 0; }"
        + "[class*='st-key-pos_del_'] button {"
        + "  height: 36px; min-height: 36px; width: 36px; min-width: 36px;"
        + "  padding: 0; border-radius: 50% !important;"
        + "  display: flex; align-items: center; justify-content: center;"
        + "}"
        + "</style>",
        unsafe_allow_html=True,
    )

    # 表头跟下面每行的列宽必须是同一套 st.columns 比例分出来的，不能自己
    # 另外拿flex div模仿列宽——之前拿固定36px去凑删除键那一列的宽度，
    # 在不同屏幕宽度下跟实际的 st.columns([9,1]) 比例对不上，表头和数据
    # 看着就没对齐。
    _head_static_col, _head_dynamic_col, _head_del_col = st.columns([5.24, 3.76, 1])
    _head_static_col.markdown(
        "<div class='fa-flex-row' style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:var(--fa-muted)'>"
        "<div style='flex:2.1'>名称/代码</div>"
        "<div style='flex:1.1;text-align:center'>走势</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _head_dynamic_col.markdown(
        "<div class='fa-flex-row' style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:var(--fa-muted)'>"
        "<div style='flex:1.3;text-align:right'>最新/成交额</div>"
        "<div style='flex:1;text-align:right'>涨跌幅</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # 先把所有行的数据一次性取完（不带任何渲染），再统一画出来——之前是
    # 边取数据边画一行，用户反馈"一个一个蹦出来很慢"。取数据本身的耗时省不掉
    # （网络请求），但至少不会让用户看着页面一行一行往外挤，而是等一下之后
    # 整批一起出现，观感上干脆很多。
    def _fetch_one(item):
        item_market = item.get("market", "A")
        symbol = item["symbol"]
        try:
            wspot = get_stock_realtime(symbol, market=item_market)
        except Exception:
            wspot = {}
        closes = _fetch_sparkline_closes(symbol, item_market)
        return (item, item_market, symbol, wspot, closes)

    def _collect_rows():
        # 之前是for循环一只一只顺序取（实时价+迷你图两个接口都要等网络返回），
        # 用户反馈"持仓加载好慢"——几只股票乘以两次网络请求累加起来确实慢。
        # A股走BaoStock/akshare，内部各自有全局锁保证线程安全，并发提交时这
        # 部分本来就会排队，不会因为并发就变快；但港股/美股走Futu，现在走的是
        # 单一常驻worker线程+队列（见data_sources.py的_futu_call），单次查询
        # 本身只要零点几秒，并发提交多只互不阻塞。用线程池把每只股票的取数
        # 并发起来，A股之间该排队还是排队，但A股和港股/美股之间、以及港股/
        # 美股彼此之间不用再互相等，混合市场的持仓整体加载时间能明显缩短。
        #
        # 统一截止时间/避免线程堆积的实现细节抽到了共享的
        # _run_concurrent_with_deadline（见它的docstring——那里记录了同一个
        # 教训：per-future timeout会被排队顺序绕过，必须用整批统一的deadline）。
        results = _run_concurrent_with_deadline(position_items, _fetch_one, timeout=4)
        rows = []
        for i, item in enumerate(position_items):
            if i in results:
                rows.append(results[i])
            else:
                rows.append((item, item.get("market", "A"), item["symbol"], {}, []))
        return rows

    # 这个fragment每3秒自动刷新一次——只有真正第一次加载（session里还没有
    # 任何一次成功渲染过）才显示"加载中"，之后的静默自动刷新不再包一层
    # spinner：之前每次刷新都会先弹一下spinner再画出列表，整个列表跟着
    # 抖一下，跟_render_price_header那套"数字变了背景轻轻一闪"的丝滑感
    # 完全相反。改成只有首次展示这一遭才等得起spinner，后续刷新静默取数，
    # 取完直接原地重画，观感上就是"数字自己跳动"而不是"列表重绘"。
    if not st.session_state.get("_pos_seen_once"):
        with st.spinner("加载中..."):
            _rows_data = _collect_rows()
        st.session_state["_pos_seen_once"] = True
    else:
        _rows_data = _collect_rows()

    # AI持仓判断——只是本地SQLite读一次(不是每行都查、也不触发AI调用)，
    # 这个fragment本身每3秒会重跑，一起刷新代价很小。数据来自advisor.py
    # 每个工作日跑一次的持仓判断(holding=True的judge_stock)，不是现场生成。
    try:
        _advice_map = get_position_advice(_email)
    except Exception:
        _advice_map = {}

    for item, item_market, symbol, wspot, closes in _rows_data:
        spark_color = "#999"
        if wspot and wspot.get("最新价") and wspot.get("昨收"):
            spark_color = UP_COLOR if wspot["最新价"] >= wspot["昨收"] else DOWN_COLOR

        # 名称+走势图这部分每3秒刷新时几乎不变（迷你图数据本身缓存了好几分钟，
        # 涨跌方向短期内也很少翻转），但之前跟价格/涨跌幅拼进同一个markdown
        # 字符串——价格每次都变，导致这一整块（含SVG）每3秒都要重新生成、
        # 重新发给前端重绘，是持仓列表"感觉卡顿"的一部分原因。缓存住SVG
        # 字符串，输入不变就直接复用，减少每次刷新真正要重绘的内容量。
        spark_key = f"_pos_spark_{symbol}_{item_market}"
        spark_cache = st.session_state.get(spark_key)
        closes_tuple = tuple(closes)
        if spark_cache is not None and spark_cache[0] == closes_tuple and spark_cache[1] == spark_color:
            spark_svg = spark_cache[2]
        else:
            spark_svg = _build_sparkline_svg(closes, spark_color)
            st.session_state[spark_key] = (closes_tuple, spark_color, spark_svg)

        shares = item.get("shares") or 0
        cost_total = item.get("cost_total") or 0
        pnl_html = ""

        if wspot and wspot.get("最新价"):
            wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
            wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
            color = UP_COLOR if wchange >= 0 else DOWN_COLOR

            # 场外基金/上金所现货是T-1净值(见get_stock_realtime里的兜底)，
            # 不是真实盘中报价——不能套用"3秒刷新+背景一闪"这套暗示"刚刚
            # 变化"的动画，那是在说谎；数字本身也要标个"T-1"，不然跟真实
            # 实时行情长得一模一样，用户没法分辨这支的涨跌是不是"今天"的。
            is_stale = str(wspot.get("数据源", "")).endswith("(T-1)")

            flash_key = f"_pos_last_price_{symbol}_{item_market}"
            prev = st.session_state.get(flash_key)
            st.session_state[flash_key] = wspot["最新价"]
            flash_class = ""
            if not is_stale and prev is not None and prev != wspot["最新价"]:
                flash_class = "price-flash-up" if wspot["最新价"] > prev else "price-flash-down"

            stale_tag = " <span style='font-size:0.65rem;color:var(--fa-muted)'>T-1</span>" if is_stale else ""
            price_html = (
                f"<div class='{flash_class}' style='text-align:right;border-radius:4px'>"
                f"<div style='font-weight:600;color:{color}'>{wspot['最新价']:.2f}{stale_tag}</div>"
                f"<div style='font-size:0.72rem;color:var(--fa-muted)'>{_fmt_turnover(wspot.get('成交额'))}</div>"
                f"</div>"
            )
            badge_html = (
                f"<div style='text-align:right'>"
                f"<span style='background:{color};color:#fff;font-size:0.78rem;font-weight:600;"
                f"padding:3px 7px;border-radius:5px;display:inline-block;min-width:58px;text-align:center'>"
                f"{wchange_pct:+.2f}%</span></div>"
            )

            # 真正持仓(shares>0)才算市值/浮盈——纯关注(shares=0)不显示这一行，
            # 跟原来持仓的观感保持一致，不会突然多出一堆"0股"的噪音信息。
            if shares > 0:
                market_value = shares * wspot["最新价"]
                pnl = market_value - cost_total
                pnl_pct = (pnl / cost_total * 100) if cost_total else 0
                pnl_color = UP_COLOR if pnl >= 0 else DOWN_COLOR
                pnl_html = (
                    f"<div style='text-align:right;font-size:0.72rem;margin-top:2px'>"
                    f"<span style='color:var(--fa-muted)'>{shares:g}股 · 市值{market_value:,.0f}</span> "
                    f"<span style='color:{pnl_color}'>{pnl:+,.0f}（{pnl_pct:+.1f}%）</span></div>"
                )
        else:
            price_html = "<div style='text-align:right;color:var(--fa-muted)'>—</div>"
            badge_html = ""

        with st.container(border=True):
            # 比例是把原来单列里 名称2.1:走势1.1:价格1.3:涨跌幅1 这四段按
            # "静态(名称+走势)/动态(价格+涨跌幅)"拆成两组，再按原比例
            # 换算回外层st.columns([9,1])的尺度（9*3.2/5.5≈5.24，9*2.3/5.5≈3.76），
            # 保证拆分前后每一段的实际宽度不变，不会因为拆列导致布局跳动。
            static_col, dynamic_col, del_col = st.columns([5.24, 3.76, 1], vertical_alignment="center")
            href = (
                f"?open_symbol={urllib.parse.quote(symbol)}"
                f"&open_market={urllib.parse.quote(item_market)}"
                f"&open_name={urllib.parse.quote(item['name'])}"
                f"&open_from=pos"
                f"{_auth_qs()}"
            )
            # 名称+走势图（静态部分）和价格+涨跌幅（动态部分）拆成两个独立的
            # st.markdown调用——同一个href两边都能点，视觉上还是整行可点，
            # 但静态部分的HTML字符串在数据没变时保持不变，Streamlit的diff能
            # 跳过它不用每3秒都重绘，只有动态部分真正需要每次刷新。
            static_col.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                # 颜色直接写在这个div自己身上，不靠继承父级<a>的color——之前靠
                # a.pos-card-link{{color:inherit!important}}死活压不过浏览器
                # 默认的a:link蓝色，元素自己的inline style优先级天然最高，不用
                # 再跟CSS特异性较劲。
                f"<div style='flex:2.1;font-weight:600;color:var(--fa-text);text-decoration:none'>{_esc(item['name'])}（{_esc(symbol)}）</div>"
                f"<div style='flex:1.1;display:flex;justify-content:center'>{spark_svg}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )
            dynamic_col.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                f"<div style='flex:1.3'>{price_html}</div>"
                f"<div style='flex:1'>{badge_html}</div>"
                f"</div>{pnl_html}</a>",
                unsafe_allow_html=True,
            )
            if del_col.button("×", key=f"pos_del_{symbol}", help="卖出/取消关注", type="tertiary"):
                # 不能在这里直接调_confirm_sell_dialog——这个函数(_render_position_rows)
                # 是@st.fragment(run_every=3)，弹窗打开后绑定的是当下这个fragment实例，
                # 但每3秒的自动刷新会让fragment在后台重新生成一份，弹窗还留在界面上、
                # 看着正常，可点"确认卖出"时服务端发现绑定的fragment id已经不存在了，
                # 点击被静默丢弃、什么反应都没有(2026-09-01真实复现：VPS日志能看到
                # "The fragment with id ... does not exist anymore"，前端完全没有报错
                # 提示，只是点了没用)。改成在这里只记一个"要打开哪个标的的卖出弹窗"的
                # session_state标记+st.rerun()（默认整页作用域，会跳出这个fragment），
                # 真正调用_confirm_sell_dialog的代码挪到本函数外层不会自动刷新的稳定
                # 作用域里（持仓/自选两个分区各自调用_render_position_rows之后），
                # 这样弹窗绑定的就是稳定作用域，不会被后台定时刷新顶掉。
                st.session_state["_confirm_sell_target"] = {
                    "symbol": symbol, "item": item, "market": item_market,
                    "cur_price": wspot.get("最新价") if wspot else None,
                }
                st.rerun()

            adv = _advice_map.get(symbol)
            if adv:
                adv_action = adv.get("action", "观望")
                adv_color = _ADVICE_ACTION_COLOR.get(adv_action, NEUTRAL_COLOR)
                adv_parts = _parse_advice_text(adv.get("fundamental_verdict", ""))
                with st.expander(f"AI持仓判断：{adv_action}（{adv.get('created_at','')[:10]}）"):
                    st.markdown(
                        f"<span style='background:{adv_color};color:#fff;border-radius:4px;padding:1px 8px;"
                        f"font-size:0.8rem;font-weight:700'>{_esc(adv_action)}</span> "
                        f"<span style='font-size:0.75rem;color:var(--fa-muted)'>置信度：{_esc(adv_parts.get('置信度','—'))}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(_esc(adv_parts.get("理由", "")))
                    for sec in ("基本面", "技术面", "价格位置"):
                        if adv_parts.get(sec):
                            st.markdown(f"**{sec}**：{_esc(adv_parts[sec])}")


def _backfill_due_reviews(email: str):
    """把到期（满7天）该补录回看价格的分析记录补上——从"历史回看"侧边栏
    expander里抽出来的独立函数，"回看"主页面和侧边栏摘要都要用同一套
    节流逻辑，不能各自维护一份容易跑偏。

    节流原因见调用方：get_due_for_review+并发get_stock_realtime这一整套
    实测过Futu需要重连时接近10秒，而"7天后补录"这个需求是天级颗粒度的，
    没必要每次切页面都重新触发一遍网络请求，同一个会话内每
    _REVIEW_RECHECK_INTERVAL 秒最多真正跑一次。

    2026-08-30修复：deadline原来是3秒，比上面这段自己docstring里写的
    "接近10秒"还短，等于这条回填路径实测经常连一个都补不上——既然
    _REVIEW_RECHECK_INTERVAL已经把真正发请求的频率控制在每60秒最多一次，
    这里不需要为了"页面快0.几秒"牺牲"功能根本跑不起来"，调到10秒对齐
    实测延迟。
    """
    _REVIEW_RECHECK_INTERVAL = 60
    _review_checked_at = st.session_state.get("_review_checked_at", 0.0)
    if time.time() - _review_checked_at <= _REVIEW_RECHECK_INTERVAL:
        return
    due = get_due_for_review(email, min_age_days=7)

    def _fetch_review_price(item):
        try:
            spot = get_stock_realtime(item["symbol"], market=item.get("market", "A"))
            return item, spot
        except Exception:
            return item, None

    if due:
        review_results = _run_concurrent_with_deadline(due, _fetch_review_price, timeout=10)
        for item, spot in review_results.values():
            # 价格跟入场价一模一样大概率是当天没开盘取回的还是同一个交易日
            # 收盘价（周末/节假日回填），不是真的"事后零涨跌"——留着不记，
            # 下次真实交易日再试，跟advisor.py的_backfill_due_advice同一个
            # 修复思路，不能当0%收益记进一致率统计。
            if spot and spot.get("最新价") and float(spot["最新价"]) != item.get("price_at_analysis"):
                try:
                    record_review(item["id"], float(spot["最新价"]))
                except Exception:
                    continue
    st.session_state["_review_checked_at"] = time.time()


def _render_accuracy_dashboard(email: str):
    """"回看"页——把原来塞在侧边栏折叠面板里的方向一致率统计，提升成
    主内容区的独立页面。数据和统计口径完全复用tracker.py已有的
    get_accuracy_stats/get_accuracy_trend（没有新造轮子），新增的是这个
    页面本身的呈现方式：把"一个孤零零的百分比"变成一个真正像"过往战绩
    公开可查"的仪表盘——这是finance-agent"不做黑箱荐股，拿数据说话"这个
    产品定位最该被看见的地方，不该被折叠面板埋起来。

    新增的日历热力图故意不用红涨绿跌那套配色（UP_COLOR/DOWN_COLOR在这个
    App里全局代表"价格涨/跌"，这里如果借用会让用户以为热力图在讲价格
    涨跌，而这里讲的是完全不同的"预测准不准"）。改用同一个UP_COLOR的
    单一色相、只调深浅（浅→深表示当天有判断且一致率从低到高），跟品牌色
    保持同源但语义不冲突。
    """
    if not st.session_state.get("logged_in"):
        st.write("")
        _, mid_empty, _ = st.columns([1, 2, 1])
        with mid_empty:
            st.markdown(
                "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                "回看是个人功能，需要登录后使用<br>"
                "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                "</div>",
                unsafe_allow_html=True,
            )
            if st.button("登录 / 注册", use_container_width=True, key="_review_page_login_btn"):
                st.session_state["guest_mode"] = False
                st.rerun()
        return

    st.caption(
        "你每次点开一支股票的「数据分析」，AI都会说它觉得接下来会涨还是会跌。"
        "等过了至少7天，我们回头看看它当时说得准不准——这个页面就是那本明细账。"
        "提醒一句：这只是过去的记录，不是投资建议，以前判断得准不代表以后也准。"
    )
    _backfill_due_reviews(email)

    stats = get_accuracy_stats(email)
    if stats["总数"] == 0:
        st.caption("还没有满7天可回看的记录，判断记录满一周后会自动出现在这里。")
        return

    # ── 头条卡片：先给一句人话结论，细节留到下面 ──────────────────────────
    # 用户反馈"一致率""滑动窗口"这些词看着费劲——不是不懂百分比，是这套
    # 统计学黑话本身就没在"讲人话"。改成"先说结论、再摆证据"的顺序：
    # 大字号的总体准确率 + 一句自动生成的人话点评（哪个方向/哪个市场判断
    # 更准），剩下细分数字降级成小字辅助信息，不再是一排并列的st.metric
    # 让人自己去比大小。
    _market_label = {"A": "A股", "HK": "港股", "US": "美股"}
    _dir_bull = stats.get("按方向", {}).get("偏多", {})
    _dir_bear = stats.get("按方向", {}).get("偏空", {})
    _mkt_stats = stats.get("按市场", {})

    _insight = ""
    if _dir_bull.get("总数", 0) >= 3 and _dir_bear.get("总数", 0) >= 3:
        _diff = _dir_bull["一致率"] - _dir_bear["一致率"]
        if abs(_diff) >= 10:
            _better = "看涨" if _diff > 0 else "看跌"
            _worse = "看跌" if _diff > 0 else "看涨"
            _insight = f"AI「{_better}」的判断比「{_worse}」更准一些。"
    if not _insight and len(_mkt_stats) >= 2:
        _qualified = {m: s for m, s in _mkt_stats.items() if s["总数"] >= 3}
        if len(_qualified) >= 2:
            _best_m = max(_qualified, key=lambda m: _qualified[m]["一致率"])
            _worst_m = min(_qualified, key=lambda m: _qualified[m]["一致率"])
            if _best_m != _worst_m and _qualified[_best_m]["一致率"] - _qualified[_worst_m]["一致率"] >= 10:
                _insight = (
                    f"在「{_market_label.get(_best_m, _best_m)}」判断得最准，"
                    f"「{_market_label.get(_worst_m, _worst_m)}」相对差一些。"
                )
    if not _insight:
        _insight = "各个方向/市场的准确率暂时看不出明显差别，样本还不算多。"

    with st.container(border=True):
        st.markdown(
            f"<div style='text-align:center;padding:8px 0'>"
            f"<div style='font-size:0.85rem;color:var(--fa-muted)'>AI说对的比例</div>"
            f"<div style='font-size:3rem;font-weight:800;color:{UP_COLOR};line-height:1.1'>{stats['一致率']:.0f}%</div>"
            f"<div style='font-size:0.85rem;color:var(--fa-muted)'>"
            f"过去 {stats['总数']} 次「涨/跌」判断里，对了 {stats['一致数']} 次</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='text-align:center;font-size:0.92rem;margin-top:4px'>{_insight}</div>",
            unsafe_allow_html=True,
        )

    st.write("")
    st.markdown("<div style='color:var(--fa-muted);font-size:0.85rem'>细分数据</div>", unsafe_allow_html=True)
    d1, d2, d3, d4, d5 = st.columns(5)
    _pairs = [
        (d1, "看涨判断", _dir_bull),
        (d2, "看跌判断", _dir_bear),
    ] + [
        (col, _market_label.get(m, m), _mkt_stats.get(m, {}))
        for col, m in zip([d3, d4, d5], ["A", "HK", "US"])
    ]
    for _col, _label, _s in _pairs:
        with _col:
            if _s.get("总数", 0) >= 3:
                st.metric(_label, f"{_s['一致率']:.0f}%", help=f"{_s['总数']} 次判断")
            else:
                st.metric(_label, "还太少", help=f"目前只有 {_s.get('总数', 0)} 次，攒够3次才统计")

    st.divider()

    _trend = get_accuracy_trend(email, window=5)
    if _trend:
        st.markdown("**最近是变准了还是变不准了**")
        st.caption("每个点是「往前数5次判断」里对的比例——曲线往上走说明最近判断得比以前准。")
        _trend_df = pd.DataFrame(_trend).set_index("日期")[["一致率"]]
        st.line_chart(_trend_df, height=200)

    st.markdown("**每天判断得准不准**")
    st.caption("颜色越深代表那天判断得越准，浅粉色代表那天判断基本没说对，灰色代表那天没有可以对照的记录。")
    _daily = get_daily_accuracy(email, days=91)
    if not _daily:
        st.caption("暂无足够的每日数据。")
    else:
        _by_date = {d["日期"]: d for d in _daily}
        _today = datetime.now(timezone.utc).date()
        _start = _today - timedelta(days=90)
        _start -= timedelta(days=_start.weekday())  # 对齐到那一周的周一，格子排布整齐

        _dates, _weeks_idx, _weekdays, _rates, _hover = [], [], [], [], []
        _cursor = _start
        _week_i = 0
        while _cursor <= _today:
            entry = _by_date.get(_cursor.isoformat())
            _dates.append(_cursor)
            _weeks_idx.append(_week_i)
            _weekdays.append(_cursor.weekday())
            _rates.append(entry["一致率"] if entry else None)
            _hover.append(
                f"{_cursor.isoformat()}<br>{entry['一致数']}/{entry['总数']} 一致（{entry['一致率']:.0f}%）"
                if entry else f"{_cursor.isoformat()}<br>无记录"
            )
            if _cursor.weekday() == 6:
                _week_i += 1
            _cursor += timedelta(days=1)

        import plotly.graph_objects as go

        _z = [[None] * (_week_i + 1) for _ in range(7)]
        _text = [[""] * (_week_i + 1) for _ in range(7)]
        for wi, wd, rate, hv in zip(_weeks_idx, _weekdays, _rates, _hover):
            _z[wd][wi] = rate if rate is not None else -1
            _text[wd][wi] = hv

        _fig = go.Figure(
            go.Heatmap(
                z=_z, text=_text, hoverinfo="text",
                colorscale=[
                    [0.0, "#eee"], [0.001, "#fbe1df"], [0.5, UP_COLOR], [1.0, "#7a0f0f"],
                ],
                zmin=-1, zmax=100, showscale=False,
                xgap=3, ygap=3,
            )
        )
        _fig.update_layout(
            height=170, margin=dict(l=30, r=10, t=10, b=10),
            yaxis=dict(
                tickmode="array", tickvals=[0, 2, 4, 6], ticktext=["一", "三", "五", "日"],
                autorange="reversed", showgrid=False,
            ),
            xaxis=dict(showgrid=False, showticklabels=False),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(_fig, use_container_width=True, key="_accuracy_calendar_heatmap")

    st.divider()
    st.markdown("**最近这些判断，一条条看**")
    # 之前这里只列"当时X → 现在Y"，要用户自己心算"这算涨了还是跌了、
    # 跟判断对不对得上"——现在直接算好、直接说结论，不用用户再动脑子。
    _verdict_label = {"偏多": "看涨", "偏空": "看跌", "中性": "没明确方向"}
    history = get_history(email, limit=20)
    for h in history:
        verdict_color = {"偏多": UP_COLOR, "偏空": DOWN_COLOR, "中性": NEUTRAL_COLOR}.get(h["verdict"], NEUTRAL_COLOR)
        verdict_text = _verdict_label.get(h["verdict"], h["verdict"])
        name_line = f"{_esc(h.get('name') or h['symbol'])}（{_esc(h['symbol'])}）"

        if h.get("review_price") and h["verdict"] != "中性":
            went_up = h["review_price"] > h["price_at_analysis"]
            correct = (h["verdict"] == "偏多" and went_up) or (h["verdict"] == "偏空" and not went_up)
            result_badge = (
                f"<span style='color:{UP_COLOR};font-weight:600'>✓ 说对了</span>" if correct
                else f"<span style='color:{DOWN_COLOR};font-weight:600'>✗ 说反了</span>"
            )
            detail = f"当时 {h['price_at_analysis']:.2f} → 一周后 {h['review_price']:.2f}"
        elif h.get("review_price"):
            result_badge = "<span style='color:var(--fa-muted)'>不算方向判断，不参与对错统计</span>"
            detail = f"当时 {h['price_at_analysis']:.2f} → 一周后 {h['review_price']:.2f}"
        else:
            result_badge = "<span style='color:var(--fa-muted)'>还没到一周，等着看结果</span>"
            detail = f"当时 {h['price_at_analysis']:.2f}"

        with st.container(border=True):
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;font-size:0.85rem'>"
                f"<span>{name_line}　"
                f"<span style='color:{verdict_color}'>AI说：{verdict_text}</span></span>"
                f"<span style='color:var(--fa-muted);font-size:0.78rem'>{h['created_at'][:10]}</span>"
                f"</div>"
                f"<div style='font-size:0.8rem;margin-top:4px;display:flex;justify-content:space-between'>"
                f"<span style='color:var(--fa-muted)'>{detail}</span>{result_badge}"
                f"</div>",
                unsafe_allow_html=True,
            )


@st.dialog("卖出确认")
def _confirm_sell_dialog(email: str, item: dict, market: str, cur_price: float | None):
    """shares=0（纯关注，没有真实持仓）走原来的简单确认删除；shares>0是真的
    在卖持仓，要展示股数/均价/现价/浮盈，让用户输入卖出股数+成交金额——
    默认卖出全部、金额按现价估算，用户可以改成真实成交价。
    """
    symbol, name = item["symbol"], item["name"]
    shares = item.get("shares") or 0
    cost_total = item.get("cost_total") or 0

    if shares <= 0:
        st.write(f"确定要取消关注「{name}」（{symbol}）吗？")
        dc1, dc2 = st.columns(2)
        if dc1.button("确认", type="primary", use_container_width=True):
            delete_position(email, symbol)
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
        if dc2.button("取消", use_container_width=True):
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
        return

    avg_cost = cost_total / shares
    st.write(f"**{name}**（{symbol}·{market}）")
    st.caption(
        f"持仓 {shares:g} 股 · 均价 {avg_cost:.2f} · 现价 "
        f"{f'{cur_price:.2f}' if cur_price else '—'}"
    )
    if cur_price:
        pnl = (cur_price - avg_cost) * shares
        pnl_color = UP_COLOR if pnl >= 0 else DOWN_COLOR
        st.markdown(f"浮动盈亏：<span style='color:{pnl_color}'>{pnl:+,.2f}</span>", unsafe_allow_html=True)

    sell_shares = st.number_input("卖出股数", min_value=0.0, max_value=float(shares), value=float(shares), step=1.0, key=f"_sell_shares_{symbol}")
    default_amount = sell_shares * cur_price if cur_price else sell_shares * avg_cost
    sell_amount = st.number_input(
        "成交金额（默认按现价估算，可改成真实成交价×股数）", min_value=0.0,
        value=float(default_amount), step=1.0, key=f"_sell_amount_{symbol}",
    )
    dc1, dc2 = st.columns(2)
    if dc1.button("确认卖出", type="primary", use_container_width=True):
        if sell_shares <= 0:
            st.error("卖出股数要大于0。")
        else:
            reduce_position(email, symbol, sell_shares, sell_amount)
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
    if dc2.button("取消", use_container_width=True):
        st.session_state.pop("_confirm_sell_target", None)
        st.rerun()


def _resolve_confirmed_symbol(email: str, q: str, market_code: str) -> dict | None:
    """两阶段添加持仓的第一阶段：只做"这个代码/名字真实存在且能拿到行情"的
    确认，不落库。返回值直接存进session_state供第二阶段（填股数/金额）用，
    避免像老版一步到位那样——多市场候选分支rerun一次就把已经填的股数/金额
    输入框冲掉。成功才记一笔搜索历史（失败的搜索没必要占历史记录的位置）。
    """
    q = q.strip()
    if not q:
        return None
    add_symbol = _resolve_add_symbol(q, market_code)
    if not add_symbol:
        st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return None
    # check_stock_valid 只覆盖 A 股（内部走 BaoStock，港美股没有等价数据源）——
    # 能提前区分"代码格式对但公司已退市"和"数据源临时故障"这两种情况，
    # 不再一律甩给用户一句含糊的"检查一下代码对不对"。
    if market_code == "A":
        valid, info = check_stock_valid(add_symbol)
        # "没有找到代码"不直接拒绝——BaoStock的基础信息表对场内基金覆盖不全
        # （比如510300沪深300ETF查不到基础信息，但真实行情是有的），这种情况
        # 交给下面的get_stock_realtime做终审：查得到真实价格就认，查不到才
        # 真的算失败。只有"已退市"/"不是个股也不是基金"这两种明确结论才在
        # 这里直接拒绝，不用再往下查。
        if not valid and "没有找到代码" not in info:
            st.error(info)
            return None
    try:
        add_spot = get_stock_realtime(add_symbol, market=market_code)
    except Exception:
        add_spot = {}
    if not add_spot or not add_spot.get("最新价"):
        if market_code == "A":
            # 代码本身已经过 check_stock_valid 确认真实存在且在市，这里查不到
            # 行情就真的是数据源临时故障，不是代码错误，提示要区分开。
            st.error(f"「{q}」的行情暂时获取不到（数据源可能临时抖动），请稍后重试。")
        else:
            st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return None
    add_search_history(email, q, market_code)
    return {
        "symbol": add_symbol, "market": market_code,
        "name": add_spot.get("名称", add_symbol), "price": add_spot["最新价"],
    }


@st.dialog("搜索")
def _show_stock_search_dialog(email: str):
    """纯搜索——找到标的直接跳详情页，不问股数/金额，跟"添加持仓"（+号）是
    两个独立入口：这个只看行情，添加持仓才是真正记一笔仓位。跟历史记录那条
    "点了直接跳详情页"是同一个模式。
    """
    query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_pos_search_query_dialog")
    candidates = st.session_state.get("_pos_search_candidates")

    if st.button("搜索", type="primary", use_container_width=True, key="_pos_search_btn_dialog") and query:
        cands = detect_symbol_candidates(query)
        if not cands:
            st.error(f"没查到「{query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(cands) == 1:
            sym = _resolve_add_symbol(query, cands[0]["market"])
            if sym:
                add_search_history(email, query, cands[0]["market"])
                st.session_state["_detail_symbol"] = sym
                st.session_state["_detail_market"] = cands[0]["market"]
                st.session_state["_detail_name"] = query
                st.session_state.pop("_pos_search_candidates", None)
                st.rerun()
            else:
                st.error(f"「{query}」查不到行情。")
        else:
            st.session_state["_pos_search_candidates"] = cands
            st.session_state["_pos_search_query"] = query
            st.rerun(scope="fragment")

    if candidates:
        cq = st.session_state.get("_pos_search_query", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(candidates))
        for ccol, c in zip(cand_cols, candidates):
            if ccol.button(
                f"{c['market_label']}（{c['symbol']}）", key=f"_pos_search_cand_{c['market']}_{c['symbol']}",
                use_container_width=True,
            ):
                sym = _resolve_add_symbol(cq, c["market"])
                if sym:
                    add_search_history(email, cq, c["market"])
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = c["market"]
                    st.session_state["_detail_name"] = cq
                    st.session_state.pop("_pos_search_candidates", None)
                    st.session_state.pop("_pos_search_query", None)
                    st.rerun()

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "A股", "HK": "港股", "US": "美股"}
        for h in history:
            row_label = f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）"
            if st.button(row_label, key=f"_pos_search_hist_{h['id']}", use_container_width=True):
                sym = _resolve_add_symbol(h["query"], h["market"])
                if sym:
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = h["market"]
                    st.session_state["_detail_name"] = h["query"]
                    st.rerun()
                else:
                    st.error(f"没查到「{h['query']}」的行情。")


@st.dialog("添加持仓")
def _show_add_position_dialog(email: str):
    confirmed = st.session_state.get("_pos_add_confirmed")

    # 第二阶段：标的已确认，填股数/金额（不填股数=只关注不持仓）
    if confirmed:
        st.write(f"**{confirmed['name']}**（{confirmed['symbol']}·{confirmed['market']}） 现价 {confirmed['price']:.2f}")
        # 金额的币种是标的所在市场的原始币种，不是人民币——HK股买入金额是
        # 港币、US股是美元，upsert_position存的cost_total也是原始币种（跟
        # positions表的currency字段一致）。之前这里无条件写"¥"，港美股用户
        # 照着标签心算成人民币填进去，会把成本按错误币种存进去，均价/浮盈
        # 全部跟着错且没法自动发现——这是真实的资金核算bug，不是措辞问题。
        _CURRENCY_LABEL = {"A": "¥", "HK": "HK$", "US": "US$"}
        cur_label = _CURRENCY_LABEL.get(confirmed["market"], "¥")

        # 真实故障记录（2026-08-25）：原来的实现是两个number_input在表单外，
        # 靠on_change互相同步（改股数自动算金额、反之亦然），"确认添加"是
        # 普通按钮。生产环境实测踩到过一次：用户填了股数、肉眼看到金额也
        # 跟着换算出来了，点确认后却按shares<=0落到了add_watch_only()分支，
        # 数据库里存进去的是shares=0/cost_total=0——按Streamlit的组件模型，
        # 普通按钮点击和旁边number_input失焦提交是两条独立的前端事件，输入
        # 又快又紧跟着点确认时，按钮这次rerun携带的可能还是上一次的旧值，
        # 界面上"看起来算对了"不代表这一次rerun里Python这边真的拿到了新值。
        # 改用st.form()：表单内部所有输入框的当前值，只在点提交按钮那一刻
        # 一次性打包发送，不再有"哪个先到"的时序竞争。代价是表单内部件不支持
        # on_change实时互算，所以放弃"边打字边看到另一个框跟着变"这个效果，
        # 换成提交后台由代码统一按"填了哪个就用哪个算另一个"来处理。
        with st.form("_pos_add_form", border=False):
            st.caption("只填股数或金额其中一个就行，另一个提交后会自动按现价换算。")
            amount = st.number_input(
                f"买入金额（{cur_label}）", min_value=0.0, value=0.0, step=100.0, key="_pos_add_amount",
            )
            shares = st.number_input(
                "股数", min_value=0.0, value=0.0, step=1.0, key="_pos_add_shares",
            )
            submitted = st.form_submit_button("确认添加", type="primary", use_container_width=True)

        if submitted:
            # 表单提交时shares/amount是这一刻真实、原子提交的值，不会再是
            # 竞态下的旧值——这里只需要处理"只填了一个，另一个要补算"。
            if shares <= 0 and amount > 0:
                shares = round(amount / confirmed["price"], 4)
            elif amount <= 0 and shares > 0:
                amount = round(shares * confirmed["price"], 2)

            if shares > 0:
                upsert_position(email, confirmed["symbol"], confirmed["name"], confirmed["market"], shares, amount)
                st.session_state.pop("_pos_add_confirmed", None)
                st.rerun()
            else:
                add_watch_only(email, confirmed["symbol"], confirmed["name"], market=confirmed["market"])
                st.session_state.pop("_pos_add_confirmed", None)
                st.rerun()
        if st.button("返回重新搜索", use_container_width=True):
            # 不调st.rerun()——dialog函数本身是@st.fragment，按钮点击已经会
            # 触发它自己重跑，弹窗留在原地。之前这里调了st.rerun()，会触发
            # 全脚本重跑，dialog判定为"这轮脚本没再调用打开它的那行代码"就
            # 直接关掉了，表现成"点了下一步后窗口突然消失，得重新点放大镜
            # 才看到填股数的第二步"——这是Streamlit官方文档明确写的行为
            # （st.dialog继承st.fragment的重跑范围，st.rerun()会跳出fragment
            # 范围触发整页重跑，从而关闭弹窗）。
            st.session_state.pop("_pos_add_confirmed", None)
            st.rerun(scope="fragment")
        return

    # 第一阶段：搜索定标的
    add_query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_pos_add_query_dialog")

    if st.button("下一步", type="primary", use_container_width=True, key="_pos_add_btn_dialog") and add_query:
        # 不再让用户先选市场——大多数公司名字只在一个市场上市，自动判断就够了
        # （比如"苹果"只有美股）。只有像"阿里巴巴"这种港股美股都有的名字，
        # 才需要用户自己选，见下面的候选按钮。
        candidates = detect_symbol_candidates(add_query)
        if not candidates:
            st.error(f"没查到「{add_query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(candidates) == 1:
            confirmed = _resolve_confirmed_symbol(email, add_query, candidates[0]["market"])
            if confirmed:
                st.session_state["_pos_add_confirmed"] = confirmed
                # scope="fragment"：只重跑这个dialog本身，弹窗不关，直接从
                # "搜索"无缝切到"填股数"——不能用默认的st.rerun()（=整页重跑），
                # 那会把弹窗关掉，见下面"返回重新搜索"按钮那条注释的详细说明。
                st.rerun(scope="fragment")
        else:
            st.session_state["_pos_add_candidates"] = candidates
            st.session_state["_pos_add_candidates_query"] = add_query

    if st.session_state.get("_pos_add_candidates"):
        cands = st.session_state["_pos_add_candidates"]
        cq = st.session_state.get("_pos_add_candidates_query", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(cands))
        for ccol, c in zip(cand_cols, cands):
            if ccol.button(
                f"{c['market_label']}（{c['symbol']}）", key=f"_pos_cand_{c['market']}_{c['symbol']}",
                use_container_width=True,
            ):
                confirmed = _resolve_confirmed_symbol(email, cq, c["market"])
                if confirmed:
                    st.session_state.pop("_pos_add_candidates", None)
                    st.session_state.pop("_pos_add_candidates_query", None)
                    st.session_state["_pos_add_confirmed"] = confirmed
                    st.rerun(scope="fragment")

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "A股", "HK": "港股", "US": "美股"}
        for h in history:
            row_label = f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）"
            # 点历史记录直接跳去那只股票的详情页，不是再加一遍持仓——
            # 用户反馈"再加"这个按钮没必要，点了就想直接看那只股票。
            if st.button(row_label, key=f"_pos_hist_open_{h['id']}", use_container_width=True):
                sym = _resolve_add_symbol(h["query"], h["market"])
                if sym:
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = h["market"]
                    st.session_state["_detail_name"] = h["query"]
                    st.rerun()
                else:
                    st.error(f"没查到「{h['query']}」的行情。")


@st.dialog("持仓对比")
def _show_compare_dialog(positions: list):
    """build_multi_comparison（charts.py）之前写好了但一直没接界面——这里补上
    唯一缺的入口：勾选几支持仓，起点归一化到100画在一张图上，直接看
    "这段时间谁涨得多"，跟单只详情页里的"对比大盘"是同一套归一化思路，
    只是不限定跟大盘比，持仓互相之间也能比。

    生成结果存进 session_state 并记下当时的选股+区间参数——多选框/区间单选
    在 st.dialog 里改动都会触发这个函数重新整个跑一遍，如果不记参数直接显示
    上次的图，选项已经变了图却没跟着变，会让人以为点了什么但没生效；比对
    参数不一致就不显示旧图，逼用户重新点一次"生成对比图"。
    """
    if len(positions) < 2:
        st.caption("持仓至少要有2只才能对比，先去加几只吧。")
        return

    st.caption("勾选2-6只持仓，起点统一归一化到100，直接对比这段时间谁涨得多。")
    options = {f"{w['name']}（{w['symbol']}）": w for w in positions}
    labels = list(options.keys())
    picked_labels = st.multiselect(
        "对比标的", labels, default=labels[: min(3, len(labels))], key="_pos_compare_pick",
    )
    period_label = st.radio(
        "区间", ["近1月", "近3月", "近6月", "近1年"], index=1, horizontal=True, key="_pos_compare_period",
    )
    period_days = {"近1月": 30, "近3月": 90, "近6月": 180, "近1年": 365}[period_label]

    if len(picked_labels) < 2:
        st.caption("至少选2只才能对比。")
        return
    if len(picked_labels) > 6:
        st.caption("最多选6只，太多线挤在一起反而看不清。")
        return

    if st.button("生成对比图", type="primary", use_container_width=True, key="_pos_compare_go"):
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=period_days + 10)).strftime("%Y%m%d")

        def _fetch(label):
            item = options[label]
            try:
                hist = get_stock_history(item["symbol"], start, end, market=item.get("market", "A"))
            except Exception:
                hist = None
            return label, hist

        with st.spinner("加载行情..."):
            # 每只标的都是一次独立的网络请求，并发起来跟持仓列表
            # (_render_position_rows) 是同一个道理，不用互相等。原来是
            # with ThreadPoolExecutor() as ex: ex.map(...)——退出with块时会
            # 等所有线程真正跑完才返回，没有截止时间，某一只标的的数据源
            # 卡得久，这次"生成对比图"点击就跟着卡多久。改用共享的
            # _run_concurrent_with_deadline给整批一个统一截止时间，超时的
            # 那几只直接跳过不算，不会拖累这次点击的响应时间。这是手动点击
            # 触发的一次性操作（不是自动刷新循环），给的deadline比持仓
            # 列表/回看补录那两处更宽松一些。
            fetch_map = _run_concurrent_with_deadline(picked_labels, _fetch, timeout=15)
            results = list(fetch_map.values())

        hist_by_name = {label: hist for label, hist in results if hist is not None and not hist.empty}
        if len(hist_by_name) < 2:
            st.error("至少要有2只成功取到行情才能对比，换一批试试，或者稍后重试。")
        else:
            st.session_state["_pos_compare_result"] = {
                "params": (tuple(picked_labels), period_label),
                "hist_by_name": hist_by_name,
            }

    cached = st.session_state.get("_pos_compare_result")
    if cached and cached["params"] == (tuple(picked_labels), period_label):
        st.plotly_chart(build_multi_comparison(cached["hist_by_name"]), use_container_width=True)


_page_slot = st.empty()

if st.session_state.get("_detail_symbol"):
    with _page_slot.container():
        _render_stock_detail(
            st.session_state["_detail_symbol"],
            st.session_state.get("_detail_market", "A"),
            st.session_state.get("_detail_name", st.session_state["_detail_symbol"]),
        )
elif st.session_state.get("_index_detail_code"):
    with _page_slot.container():
        _render_index_detail(
            st.session_state.get("_index_detail_name", ""),
            st.session_state["_index_detail_code"],
            st.session_state.get("_index_detail_market", "A"),
        )
elif st.session_state.get("_sector_detail_name"):
    with _page_slot.container():
        _render_sector_detail(
            st.session_state["_sector_detail_name"],
            st.session_state.get("_sector_detail_market", "A"),
        )
else:
    with _page_slot.container():
        with st.sidebar:
            if st.session_state.get("logged_in"):
                _uemail = st.session_state.get("user_email", "")
                _uemail_safe = _uemail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                st.markdown(f"<p style='font-size:0.8rem;color:var(--fa-muted)'>{_uemail_safe}</p>", unsafe_allow_html=True)
                if st.button("退出登录", use_container_width=True):
                    _tok = st.session_state.pop("_token", None)
                    if _tok:
                        _invalidate_token(_tok)
                    try:
                        del st.query_params["_auth"]
                    except Exception:
                        pass
                    _cv1.html(
                        '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");window.parent.document.cookie="fa_auth_tok=; max-age=0; path=/";}catch(e){}</script>',
                        height=1,
                    )
                    st.session_state["logged_in"] = False
                    st.session_state.pop("user_email", None)
                    st.rerun()
            else:
                st.markdown("<p style='font-size:0.8rem;color:var(--fa-muted)'>游客模式浏览中</p>", unsafe_allow_html=True)
                if st.button("登录 / 注册", use_container_width=True):
                    st.session_state["guest_mode"] = False
                    st.rerun()
            _uemail = st.session_state.get("user_email", "")

            st.divider()
            with st.expander("历史回看"):
                # 完整的统计/趋势/日历热力图原来在主内容区独立的"回看"分区
                # （_render_accuracy_dashboard），2026-09-01那个分区改名
                # "AI模拟炒股"、内容也换成了AI自主模拟盘展示，不再是准确率
                # 完整版——"查看完整回看→"这个跳转按钮去掉了，点过去会是
                # 完全不相关的内容，不能留着一个指向错地方的链接。
                # _render_accuracy_dashboard函数和它的数据都还在，只是现在
                # 没有入口能跳到它的完整版，只留这里的摘要数字。补录逻辑
                # 抽成了_backfill_due_reviews。
                if not st.session_state.get("logged_in"):
                    st.caption("登录后可以看AI过去说得准不准。")
                else:
                    _backfill_due_reviews(_uemail)
                    stats = get_accuracy_stats(_uemail)
                    if stats["总数"] > 0:
                        st.metric(
                            "AI说对的比例", f"{stats['一致率']:.0f}%",
                            help=f"过去 {stats['总数']} 次「涨/跌」判断里，对了 {stats['一致数']} 次",
                        )
                    else:
                        st.caption("还没有满7天可回看的记录。")

            with st.expander("应用指南"):
                st.markdown(
                    "**定位**\n\n"
                    "Invest Agent 是一个多市场（A股/港股/美股）行情查询和数据交叉验证工具，"
                    "把行情、财务、新闻这几类原始数据放在一起给你看。个股/指数详情页的 AI 分析"
                    "只做交叉核对和综合评分，不做黑箱荐股、不直接给买卖判断；"
                    "「持仓」页的组合分析是例外——它只针对你自己填的真实持仓和设定的资金上限，"
                    "按集中度、资金余量给出继续持有/加仓/减仓/定投/止盈/割肉这类具体操作建议"
                    "（附股数和金额），这是基于你自己数据算出来的仓位管理建议，不是选股推荐，"
                    "同样不构成投资建议，请自行判断风险。\n\n"
                    "**行情**\n\n"
                    "首页按市场切换查看核心指数（A股按涨跌幅列示，港股按东财人气榜排热度，"
                    "美股展示固定核心股名单），A股另有涨停/跌停池和南向资金；"
                    "价格每 3 秒自动刷新一次。\n\n"
                    "**个股/指数详情页**\n\n"
                    "点开任意标的先看K线或分时图，再看一手资讯（A股优先展示官方公告，"
                    "港股/美股优先富途资讯，都查不到才退回财新摘要），最后是 AI 深度分析——"
                    "包含资讯解读、财务摘要、对比大盘、技术面与消息面交叉验证，"
                    "以及一段综合评分（0-100，越高越偏多头证据、越低越偏空头证据，"
                    "评分依据是各条独立证据链是否互相印证，不是 AI 自己主观看好程度）。\n\n"
                    "**持仓**\n\n"
                    "右上角放大镜可以按代码或名称搜索添加，填股数/金额记为真实持仓"
                    "（不填只是关注），卡片显示迷你走势图、实时涨跌和持仓浮盈，"
                    "点卡片进详情页，点 × 卖出或取消关注。\n\n"
                    "**AI模拟炒股**\n\n"
                    "内置AI（千问）用虚拟资金自主管理一个模拟盘——只交易港股/美股（A股不参与），"
                    "起始本金约十万港币，在开盘时段每15分钟自主决定要不要买卖，不需要手动操作，"
                    "这里能看到它的持仓、收益曲线和完整交易记录，仅供观察AI决策能力，"
                    "不构成投资建议。（2026-09-01之前这个分区叫「历史回看」、展示的是下面这条"
                    "方向一致率统计，改版后挪到了侧边栏摘要，主分区换成了这个。）\n\n"
                    "**历史回看（侧边栏摘要）**\n\n"
                    "每次生成「综合数据分析」时会记录当时价格和 AI 判断的方向倾向，"
                    "满 7 天后自动补录当时的价格做对照，统计一个方向一致率——"
                    "这是历史记录的客观统计，不代表未来表现，不是胜率承诺。完整版目前只在"
                    "侧边栏「历史回看」折叠面板里看摘要数字，没有单独的主分区入口。\n\n"
                    "**重要说明**\n\n"
                    "本应用所有分析、评分、资讯摘要仅基于公开数据的整理和交叉核对，"
                    "不构成任何投资建议，不保证数据的完整性和及时性，据此操作的风险自负。"
                )

            with st.expander("数据源状态"):
                st.caption("只读当前进程已知的连接/熔断状态，不是实时探测——打开这个面板本身不会额外发请求。")
                _health = get_data_source_health()
                _futu = _health["futu"]
                if not _futu["已安装SDK"]:
                    st.markdown("**Futu OpenD**：未安装 SDK，港股/美股实时数据全部走兜底源（腾讯行情）。")
                elif _futu["已连接"]:
                    st.markdown(f"**Futu OpenD**：<span style='color:{UP_COLOR}'>已连接</span>", unsafe_allow_html=True)
                else:
                    _last_try = _futu["上次尝试连接"]
                    _last_try_txt = (
                        datetime.fromtimestamp(_last_try).strftime("%H:%M:%S") if _last_try else "尚未尝试"
                    )
                    _next_interval = _futu["下次重连间隔秒"]
                    _next_interval_txt = f"，下次重连间隔约{_next_interval:.0f}秒（连续失败会指数退避，最长5分钟）" if _next_interval else ""
                    st.markdown(
                        f"**Futu OpenD**：<span style='color:{DOWN_COLOR}'>未连接</span>"
                        f"（上次尝试 {_last_try_txt}{_next_interval_txt}；"
                        "港股/美股行情会自动退回腾讯行情兜底，不影响使用）",
                        unsafe_allow_html=True,
                    )

                _breakers = _health["熔断记录"]
                if _breakers:
                    st.caption("兜底数据源熔断记录（只记录触发过失败的，没列出不代表已验证成功，只是还没失败过）")
                    for _b in _breakers:
                        _ts = datetime.fromtimestamp(_b["最近一次失败"]).strftime("%m-%d %H:%M:%S")
                        _state, _color = ("冷却中", DOWN_COLOR) if _b["冷却中"] else ("已恢复", NEUTRAL_COLOR)
                        st.markdown(
                            f"<span style='font-size:0.8rem'>{_b['名称']}："
                            f"<span style='color:{_color}'>{_state}</span>（最近一次失败 {_ts}）</span>",
                            unsafe_allow_html=True,
                        )
                else:
                    st.caption("暂无兜底数据源失败记录。")

        st.markdown(
            f"""
            <div class='fa-flex-row' style='background:{UP_COLOR};margin:-1rem -1rem 0 -1rem;padding:14px 24px;
                        display:flex;align-items:center'>
                <span style='color:#fff;font-size:1.3rem;font-weight:700;letter-spacing:.02em'>Invest Agent</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


        # "行情"分区的快速搜索框去掉了——用户反馈是累赘（"持仓"分区里
        # "新增持仓"自己就有搜索框，两边都放显得重复）。指数/个股的浏览
        # 入口保留在下面的指数卡片列表和涨跌幅排行榜里。

        # 用 radio 手动实现 tab 切换，不用 st.tabs()——st.tabs() 选中哪个是纯前端状态，
        # 代码控制不了；从持仓点进详情页再返回时，需要能把选中项强制拨回"持仓"。
        # "首页"放在最前面且是默认分区——打开网站先看首页（世界地图+今日资讯），
        # 不是直接扔进"行情"这种数据密集页面。
        st.session_state.setdefault("_active_section", "首页")

        active_section = st.radio(
            "分区", ["首页", "行情", "持仓", "自选", "AI模拟炒股"], key="_active_section", horizontal=True, label_visibility="collapsed",
        )

        if active_section == "首页":
            _render_home_page()

        elif active_section == "行情":
            # 指数快照/大盘统计+涨停跌停池/南向资金+核心股/热门板块，这几块
            # 之前全挤在一段代码里顺序往下跑——点其中任何一个的交互按钮
            # （比如"显示更多"、"更多板块"）都会带动其余几块跟着重新拉一遍
            # 数据，是页面交互卡顿的主要原因。现在各自是独立的@st.fragment，
            # 点一个按钮只重新跑对应那一块。
            mkt_pick = st.radio("市场", ["A股", "港股", "美股"], horizontal=True, key="_market_overview_pick")
            mkt_code = {"A股": "A", "港股": "HK", "美股": "US"}[mkt_pick]

            _render_index_snapshot(mkt_code)
            st.divider()

            if mkt_code == "A":
                _render_a_share_overview()
            elif mkt_code == "HK":
                _render_hk_overview()
            else:
                _render_us_overview()

            st.divider()
            st.markdown("**热门板块**")
            _render_hot_sectors(mkt_code)

        elif active_section == "持仓":
            if not st.session_state.get("logged_in"):
                st.write("")
                _, mid_empty, _ = st.columns([1, 2, 1])
                with mid_empty:
                    st.markdown(
                        "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                        "持仓管理是个人功能，需要登录后使用<br>"
                        "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("登录 / 注册", use_container_width=True):
                        st.session_state["guest_mode"] = False
                        st.rerun()
            else:
                _email = st.session_state["user_email"]
                positions = get_positions(_email)
                # 自选（shares=0，只关注不持仓）挪到独立的"自选"分区显示，这里
                # 只处理真实持仓——之前两者混在同一个列表里，用户反馈新加的自选
                # 股票"怎么跑到持仓里去了"，跟真金白银的仓位堆在一起分不清。
                holding_items = [p for p in positions if (p.get("shares") or 0) > 0]

                st.markdown(
                    "<style>"
                    "[class*='st-key-pos_search_icon'] button, [class*='st-key-pos_compare_icon'] button,"
                    "[class*='st-key-pos_add_icon'] button {"
                    "  display: flex; align-items: center; justify-content: center;"
                    "  height: 44px; min-height: 44px; width: 44px; min-width: 44px;"
                    "  padding: 0; border-radius: 50% !important;"
                    "}"
                    "[class*='st-key-pos_search_icon'] span[data-testid='stIconMaterial'],"
                    "[class*='st-key-pos_compare_icon'] span[data-testid='stIconMaterial'],"
                    "[class*='st-key-pos_add_icon'] span[data-testid='stIconMaterial'] {"
                    "  font-size: 1.6rem !important;"
                    "}"
                    "</style>",
                    unsafe_allow_html=True,
                )
                # 搜索（纯查行情，跳详情页）和添加持仓（真正记一笔仓位）是两个
                # 独立入口，不要合并——之前合并成一个放大镜图标时，点开就是"添加
                # 持仓"弹窗，没有单纯查一下行情的入口。build_multi_comparison
                # （charts.py）之前写好了没接界面，这里补上入口：持仓至少2只时才
                # 显示"对比"图标，三个图标并排。
                if len(holding_items) >= 2:
                    title_col, compare_col, search_col, add_col = st.columns([9, 1, 1, 1], vertical_alignment="center")
                    if compare_col.button("", icon=":material/show_chart:", key="pos_compare_icon", type="tertiary", help="对比持仓走势"):
                        _show_compare_dialog(holding_items)
                else:
                    title_col, search_col, add_col = st.columns([10, 1, 1], vertical_alignment="center")
                if search_col.button("", icon=":material/search:", key="pos_search_icon", type="tertiary", help="搜索"):
                    _show_stock_search_dialog(_email)
                if add_col.button("", icon=":material/add:", key="pos_add_icon", type="tertiary", help="添加持仓"):
                    _show_add_position_dialog(_email)

                if not holding_items:
                    st.write("")
                    _, mid_empty, _ = st.columns([1, 2, 1])
                    with mid_empty:
                        st.markdown(
                            "<div style='text-align:center;color:var(--fa-muted);padding:20px 0 10px'>"
                            "还没有持仓<br>"
                            "<span style='font-size:0.82rem'>点右上角的 + 按钮添加，填了股数/金额才算持仓——"
                            "不填股数会加进「自选」分区</span>"
                            "</div>",
                            unsafe_allow_html=True,
                        )

                if holding_items:
                    # 环形图 | 持仓列表，左右各半——环形图不用@st.fragment(run_every=3)
                    # （见_render_positions_donut docstring：Plotly图3秒重绘会闪烁），
                    # 右边列表沿用原来的3秒自动刷新fragment，两边各自独立刷新节奏。
                    donut_col, list_col = st.columns([1, 1])
                    with donut_col:
                        _render_positions_donut(holding_items)
                    with list_col:
                        # 用户反馈持仓一般也就几只，市场筛选(全部/A股/港股/美股)没有实际
                        # 必要，反而多一层点击——去掉筛选，统一直接展示全部持仓。
                        _render_position_rows(holding_items, _email)

                    # 卖出确认弹窗调用挪到这个稳定作用域（不是_render_position_rows
                    # 那个run_every=3的fragment内部）——见_render_position_rows里
                    # pos_del_按钮那段注释，原因是fragment的定时自动刷新会让弹窗绑定
                    # 的fragment失效，点"确认卖出"没反应。按shares>0过滤，避免自选
                    # 分区点的卖出误在这个持仓分区弹出来。
                    _sell_target = st.session_state.get("_confirm_sell_target")
                    if _sell_target and (_sell_target["item"].get("shares") or 0) > 0:
                        _confirm_sell_dialog(_email, _sell_target["item"], _sell_target["market"], _sell_target["cur_price"])

                    st.divider()
                    pnl_col, ai_col = st.columns([1, 1])
                    with pnl_col:
                        _render_positions_today_pnl(holding_items)
                        st.divider()
                        _render_max_capital_input(_email)
                    with ai_col:
                        _render_portfolio_advice(_email, holding_items)

        elif active_section == "自选":
            if not st.session_state.get("logged_in"):
                st.write("")
                _, mid_empty, _ = st.columns([1, 2, 1])
                with mid_empty:
                    st.markdown(
                        "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                        "自选是个人功能，需要登录后使用<br>"
                        "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("登录 / 注册", use_container_width=True, key="_watch_login_btn"):
                        st.session_state["guest_mode"] = False
                        st.rerun()
            else:
                _email = st.session_state["user_email"]
                # 自选＝positions表里shares=0的行（"只关注不持仓"），跟持仓
                # 是同一张表，靠shares区分，不是独立的表——见add_watch_only。
                watch_items = [p for p in get_positions(_email) if (p.get("shares") or 0) == 0]

                # 复用持仓分区那套圆形图标按钮样式（见上面"持仓"分支同款CSS的
                # 注释）——两个分区各自独立渲染，键名前缀不同，样式要各放一份。
                st.markdown(
                    "<style>"
                    "[class*='st-key-watch_search_icon'] button, [class*='st-key-watch_add_icon'] button {"
                    "  display: flex; align-items: center; justify-content: center;"
                    "  height: 44px; min-height: 44px; width: 44px; min-width: 44px;"
                    "  padding: 0; border-radius: 50% !important;"
                    "}"
                    "[class*='st-key-watch_search_icon'] span[data-testid='stIconMaterial'],"
                    "[class*='st-key-watch_add_icon'] span[data-testid='stIconMaterial'] {"
                    "  font-size: 1.6rem !important;"
                    "}"
                    "</style>",
                    unsafe_allow_html=True,
                )
                _, search_col, add_col = st.columns([10, 1, 1], vertical_alignment="center")
                if search_col.button("", icon=":material/search:", key="watch_search_icon", type="tertiary", help="搜索"):
                    _show_stock_search_dialog(_email)
                if add_col.button("", icon=":material/add:", key="watch_add_icon", type="tertiary", help="添加自选"):
                    _show_add_position_dialog(_email)

                if not watch_items:
                    st.write("")
                    _, mid_empty, _ = st.columns([1, 2, 1])
                    with mid_empty:
                        st.markdown(
                            "<div style='text-align:center;color:var(--fa-muted);padding:20px 0 10px'>"
                            "还没有自选股票<br>"
                            "<span style='font-size:0.82rem'>点右上角的 + 按钮添加，弹窗里不填股数/金额即为自选</span>"
                            "</div>",
                            unsafe_allow_html=True,
                        )
                else:
                    _render_position_rows(watch_items, _email)

                # 同上——挪到稳定作用域，避开run_every fragment失效的问题；
                # shares<=0过滤只处理"自选"这边点的卖出/取消关注。
                _sell_target = st.session_state.get("_confirm_sell_target")
                if _sell_target and (_sell_target["item"].get("shares") or 0) <= 0:
                    _confirm_sell_dialog(_email, _sell_target["item"], _sell_target["market"], _sell_target["cur_price"])

        elif active_section == "AI模拟炒股":
            # 2026-09-01用户明确要求"回看那边全部改成AI模拟炒股"，分区
            # 名字本身也在同一天改成"AI模拟炒股"——完全替换掉原来的AI判断
            # 准确率追踪入口，_render_accuracy_dashboard函数本身和它依赖
            # 的历史数据都还在，只是不再从这个入口调用。
            _render_ai_sim_dashboard(_uemail)

        _render_ai_assistant()

