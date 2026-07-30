"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import streamlit as st
import streamlit.components.v1 as _cv1
from datetime import datetime, timedelta

from data_sources import (
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
    search_stock_by_name,
    get_multi_index_snapshot,
    get_market_breadth,
    get_limit_pool,
    get_hk_famous_movers,
    get_index_top_movers,
    get_southbound_flow,
    get_us_famous_movers,
    get_hot_sectors,
    get_hstech_constituents,
    resolve_symbol_by_name,
    detect_symbol_candidates,
)
from analysis import (
    cross_validate, summarize_financials, summarize_news, summarize_index_news, summarize_benchmark,
    extract_verdict, analyze_index, summarize_overall, extract_score,
)
from tracker import (
    log_analysis, get_history, get_due_for_review, record_review, get_accuracy_stats,
    add_to_watchlist, remove_from_watchlist, is_in_watchlist, get_watchlist,
    add_search_history, get_search_history,
)
from charts import (
    build_candlestick, build_intraday_line, compute_stats, compute_technical_signal, compute_realtime_signal,
    build_benchmark_comparison, build_return_histogram,
)
from auth import (
    _check_user, _register_user, _create_token, _validate_token,
    _invalidate_token, _hash_pw, _user_exists,
)
from theme import UP_COLOR, DOWN_COLOR, NEUTRAL_COLOR

for _k in ("SUPABASE_URL", "SUPABASE_KEY"):
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
   成分股卡片、指数快照表头、自选股行、热门板块宫格）都是手写flex比例布局，
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

st.markdown(_FA_BASE_CSS, unsafe_allow_html=True)

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
# 之前这个遮罩只在"真实整页导航"（点<a href>链接）时生效，切"行情/自选股"
# 这种纯靠st.radio触发的内部rerun时完全不出现——用户反馈"自选股页面还是有
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
""".replace("__FA_LOADER_NONCE__", datetime.now().isoformat()), height=0)


def _show_login_page():
    st.markdown(
        "<div style='text-align:center;padding:60px 0 24px'>"
        "<div style='font-size:1.5rem;font-weight:600;margin:8px 0 4px'>Invest Agent</div>"
        "<div style='font-size:0.85rem;color:var(--fa-muted)'>行情 + 财务 + 新闻交叉验证 · 登录后开始使用</div>"
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
                    # token 不写进 st.query_params——7天免登录凭证常驻在可见地址栏里
                    # 会被Nginx access log/浏览器历史明文留存。当次会话靠session_state
                    # 就够了，后续每个卡片链接会通过_auth_qs()从session_state重新
                    # 拼一份，不依赖当前地址栏残留的这一份。
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
# 只在还没登录时注入这个iframe——登录之后每次rerun（尤其分时图20秒自动刷新那种
# 高频rerun）都重建一次这个iframe纯属浪费，是页面变卡的一个来源。
if not st.session_state.get("logged_in"):
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
        # 这里之前有一行 del st.query_params["_auth"]（出发点：校验通过就从
        # 地址栏删掉token，避免常驻URL）——在math-agent上实测过一模一样的
        # 写法会出真实的生产问题：st.query_params的修改会触发Streamlit自动
        # 重跑脚本，而上面"localStorage自动登录"那段JS判断要不要注入的时机
        # （`if not st.session_state.get("logged_in")`，在脚本更靠前的位置）
        # 跟这里token校验真正把logged_in置位的时机之间有代码距离，这次由
        # del触发的额外重跑会让那段JS在某些时序窗口下又判断成"还没登录"，
        # 重新触发"从localStorage读token→塞进URL→800ms后强制刷新"，造成
        # 登录后网页陷入固定几秒一次的无限刷新循环。安全加固和"网站能正常
        # 打开"冲突时选后者，这里不删，token继续留在URL里（7天有效期本身
        # 不变）。
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

# 自选股列表整卡片可点——之前试过CSS覆盖层、JS找DOM绑事件两种方案，
# 在真实浏览器里都点不动（大概率是这两种方案都依赖对Streamlit内部渲染结构
# 的猜测，版本一变或者猜错了就失效）。改成最朴素可靠的办法：卡片内容整个
# 包在一个真正的<a href="?...">链接里，点击就是标准的浏览器导航行为，
# 不依赖任何JS/CSS去猜内部结构。这里在页面渲染最开始就检查URL参数，
# 有就直接跳转详情页并清掉参数。
if st.query_params.get("open_symbol"):
    st.session_state["_detail_symbol"] = st.query_params["open_symbol"]
    st.session_state["_detail_market"] = st.query_params.get("open_market", "A")
    st.session_state["_detail_name"] = st.query_params.get("open_name", st.query_params["open_symbol"])
    # 从自选股卡片点进来的，"返回"要能回到自选股分区，不是每次都弹回默认的
    # "行情"分区——整页导航会把session_state清空，"_active_section"记不住
    # 是从哪个分区点进来的，得靠这个参数显式带过来。
    if st.query_params.get("open_from") == "wl":
        st.session_state["_active_section"] = "自选股"
    st.query_params.clear()
    st.rerun()
if st.query_params.get("open_index_code"):
    st.session_state["_index_detail_code"] = st.query_params["open_index_code"]
    st.session_state["_index_detail_market"] = st.query_params.get("open_index_market", "A")
    st.session_state["_index_detail_name"] = st.query_params.get("open_index_name", "")
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
    """自选股行情列表里那种"一眼看趋势"的迷你走势图——不用plotly（每行一个太重，
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
    """自选股迷你图用的近期收盘价——直接复用已有的历史行情接口（带缓存，5分钟
    过期），不新开专门的接口，多取一倍自然日天数换算成够用的交易日数量。
    """
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
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
    """"新增自选股"用的名称→代码解析，A股之前一直漏了——resolve_symbol_by_name
    只支持HK/US（内部的知名股名单和Futu模糊搜索都没有A股这块），A股market
    传进去必然返回None，退化成直接把"茅台"这种中文名当代码用，当然查不到。
    这里A股单独先走search_stock_by_name（BaoStock按名称模糊匹配，真支持A股）。
    """
    q = q.strip()
    if market_code == "A":
        try:
            matches = search_stock_by_name(q)
        except Exception:
            matches = []
        if matches:
            return matches[0]["code"]
        return q if re.match(r"^\d{6}$", q) else None
    by_name = resolve_symbol_by_name(q, market_code)
    if by_name:
        return by_name
    return q.zfill(5) if market_code == "HK" else q.upper()


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
            + f"<div style='position:relative;height:6px;border-radius:3px;background:linear-gradient(to right,#22a06b,#d8d8d8,#e02020)'>"
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


def _stream_overall_summary(gen) -> str:
    """总结性分析首次生成时的流式处理——先在一个占位区域里打字机效果播放AI
    的原始输出（这时候末尾的[综合评分: N]标签会跟着文字一起可见地闪过去，
    这是流式效果本身带来的、可以接受的小瑕疵），生成完之后清空占位区域，
    换成_render_overall_summary画的最终版本（评分标签从正文里摘出来，
    做成上面的可视化打分条，不再在正文里裸露出现）。

    手动逐块迭代而不是直接把生成器丢给st.write_stream()——之前那样写，
    生成过程中一旦出错（比如API瞬时抖动），实测st.write_stream()会把异常
    悄悄吞掉、返回空字符串，页面上就变成"总结性分析"标题下面空空如也，
    连报错都看不到。手动迭代能兜住异常，出错时给一句明确的错误提示，
    不会把空字符串存进缓存。
    """
    placeholder = st.empty()
    full_text = ""
    try:
        for chunk in gen:
            full_text += chunk
            placeholder.markdown(full_text + "▌")
    except Exception as e:
        placeholder.empty()
        return f"汇总失败：{e}"
    placeholder.empty()
    if not full_text.strip():
        return "汇总失败：AI 没有返回任何内容，请点「重新分析」再试一次。"
    return full_text


def _write_stream_safe(gen) -> str:
    """给AI模块用的流式显示——不直接调st.write_stream(gen)，实测生成过程中
    一旦出错（API瞬时抖动之类），st.write_stream()会把异常悄悄吞掉、返回
    空字符串，缓存进session_state后页面上就是标题下面空空如也，连报错都
    看不见（"总结性分析"那块就踩过这个坑）。手动逐块迭代、显式捕获异常，
    出错时抛出去让调用方的try/except接住，绝不会把空字符串当成正常结果存住。
    """
    placeholder = st.empty()
    full_text = ""
    for chunk in gen:
        full_text += chunk
        placeholder.markdown(full_text + "▌")
    placeholder.markdown(full_text)
    if not full_text.strip():
        raise RuntimeError("AI 没有返回任何内容")
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
        for _, r in news.iterrows():
            _title = r["新闻标题"]
            _title_html = (
                f"<a href='{r.get('url', '')}' target='_blank' style='color:var(--fa-text);text-decoration:none'>{_title}</a>"
                if idx_clickable else f"<span style='color:var(--fa-text)'>{_title}</span>"
            )
            st.markdown(
                f"<div style='margin:6px 0;font-size:0.9rem'>"
                f"<span style='color:var(--fa-muted);font-size:0.78rem'>{r.get('日期', '') or ''}</span>　"
                f"{_title_html}　"
                f"<span style='color:var(--fa-muted);font-size:0.75rem'>{r.get('分类', '')}</span>"
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
    for _, r in news.iterrows():
        date = r.get("日期") or ""
        title = r["新闻标题"]
        tag = r.get("分类", "")
        title_html = (
            f"<a href='{r.get('url', '')}' target='_blank' style='color:var(--fa-text);text-decoration:none'>{title}</a>"
            if clickable else f"<span style='color:var(--fa-text)'>{title}</span>"
        )
        st.markdown(
            f"<div style='margin:6px 0;font-size:0.9rem'>"
            f"<span style='color:var(--fa-muted);font-size:0.78rem'>{date}</span>　"
            f"{title_html}　"
            f"<span style='color:var(--fa-muted);font-size:0.75rem'>{tag}</span>"
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
        stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
        # 原始新闻列表已经在页面上方单独一块展示了（_render_news_section），
        # 这里不重复摆一次，只放AI解读，避免同一份数据在页面上出现两遍。
        if is_fresh:
            news, _ = _fetch_news_items(stock_name, symbol, market)
            news_summary = _news_to_summary(news)
            try:
                ai_text = _write_stream_safe(summarize_news(symbol, news_summary))
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
                    ai_text = _write_stream_safe(summarize_financials(symbol, financial_summary))
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
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
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
                    ai_text = _write_stream_safe(summarize_benchmark(symbol, stock_pct, bm_name, bm_pct))
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
            stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
            news, _ = _fetch_news_items(stock_name, symbol, market)
            news_summary = _news_to_summary(news)

            try:
                ai_text = _write_stream_safe(
                    cross_validate(symbol, history_summary, financial_summary, news_summary, technical_summary)
                )
            except Exception as e:
                st.error(f"分析失败：{e}")
                return
            current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
            verdict = extract_verdict(ai_text)
            stock_name = spot.get("名称", symbol) if spot else symbol
            log_analysis(
                st.session_state["user_email"], symbol, float(current_price), ai_text,
                verdict=verdict, market=market, name=stock_name,
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

    _watched_now = is_in_watchlist(st.session_state["user_email"], symbol)
    if _watched_now:
        if st.button("移除自选", key="wl_toggle"):
            remove_from_watchlist(st.session_state["user_email"], symbol)
            st.rerun()
    else:
        if st.button("加入自选", key="wl_toggle"):
            add_to_watchlist(st.session_state["user_email"], symbol, spot.get("名称", symbol), market=market)
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


def _inject_wl_card_css():
    """wl-card-link 这个class的样式——多个板块（自选股/成分股/涨跌停池/核心股
    榜）共用同一个class做卡片点击跳转，样式只需要注入一次，但每个板块渲染时
    不一定确定其它板块的注入代码有没有跑过，重复调用这个函数是幂等的，
    不会有副作用。
    """
    st.markdown(
        "<style>"
        "a.wl-card-link, a.wl-card-link:link, a.wl-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.wl-card-link:hover { opacity: 0.85; }"
        "</style>",
        unsafe_allow_html=True,
    )


def _render_stock_movers_cards(df, market: str):
    """把一份"代码/名称/最新价/涨跌幅"的行情表渲成一叠可点击卡片（红涨绿跌，
    点击跳去那只股票详情页）——涨跌停池、港股/美股核心股榜、指数成分股都是
    这个形态，抽成公共函数不用每处各写一遍。df为空时调用方自己处理提示语，
    这里不管。

    价格变了背景闪一下红/绿：跟自选股列表(_render_watchlist_rows)、详情页
    价格区块(_render_price_header)同一套_PRICE_FLASH_CSS机制，用户反馈"行情
    里的股票也要有这个效果"——调用方（涨跌停池/港股核心股/美股核心股这几个
    fragment）都已经是run_every=3自动刷新，数据变了这里自然就能跟着闪。
    """
    _inject_wl_card_css()
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
                f"<a class='wl-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row {flash_class}' style='display:flex;align-items:center;border-radius:4px'>"
                f"<div style='flex:2;font-weight:600;color:var(--fa-text);text-decoration:none'>"
                f"{row['名称']}（{mv_symbol}）</div>"
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

    expand_key = f"_movers_expand_{market}"
    show_n = 30 if st.session_state.get(expand_key) else 10
    _render_stock_movers_cards(movers.head(show_n), market)

    if len(movers) > 10:
        if not st.session_state.get(expand_key):
            if st.button("展开（前30）", key=f"_movers_expand_btn_{market}"):
                st.session_state[expand_key] = True
                st.rerun()
        else:
            if st.button("收起", key=f"_movers_collapse_btn_{market}"):
                st.session_state[expand_key] = False
                st.rerun()


@st.fragment(run_every=3)
def _render_index_snapshot(mkt_code: str):
    """"行情"tab顶部的指数快照卡片。做成fragment的原因见_render_a_share_overview
    开头的注释——本质是同一个问题：这几个区块以前全挤在同一段代码里，点其中
    任何一个的交互按钮都会连带其余区块一起重新拉一遍数据。

    run_every=3 + 涨跌闪烁，跟涨跌停股池/核心股列表统一（get_multi_index_
    snapshot的缓存TTL已经是3秒，数据本身能跟上）。
    """
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
                f"<div style='flex:2.4;font-weight:600;color:var(--fa-text);text-decoration:none'>{idx['名称']}</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{color}'>{idx['最新']:,.2f}</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌']:+.2f}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


@st.fragment(run_every=3)
def _render_a_share_overview():
    """A股大盘统计+涨停/跌停股池。之前这几块和指数快照、热门板块全部挤在
    "行情"tab同一段代码里——点"显示更多（前30）"这一个按钮，会触发整个
    tab重新rerun，连带指数快照、热门板块这些跟这次点击完全无关的区块也要
    重新拉一遍数据（其中指数快照缓存只有25秒，涨停跌停池等未必命中缓存），
    这是页面交互感觉卡顿的主要原因。拆成独立fragment后，点这个按钮只会
    重新跑这一个区块。

    加run_every=3是为了让_render_stock_movers_cards里价格变化的红绿闪烁
    效果在这里也能跟自选股一样跳动起来——底下的get_limit_pool等都是批量
    接口带自己的缓存TTL，3秒轮询大部分时候直接命中缓存，不会因此加重
    请求负担，只有缓存真正过期、数据真的变了才会触发闪烁。
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


@st.fragment(run_every=3)
def _render_hk_overview():
    """港股南向资金+核心股，独立fragment，原因同_render_a_share_overview
    （含run_every=3的原因）。"""
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


@st.fragment(run_every=3)
def _render_us_overview():
    """美股核心股，独立fragment，原因同_render_a_share_overview
    （含run_every=3的原因）。"""
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
    "更多板块"展开到前30。板块在这个app里不是可跳转详情的实体，纯展示卡片，
    不带链接，跟涨跌停池/核心股那种可点击卡片是两回事。

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
            with col:
                with st.container(border=True):
                    st.markdown(
                        f"<div style='font-weight:600;color:var(--fa-text)'>{row['板块']}</div>"
                        f"<div style='color:{s_color};font-weight:700;font-size:1.1rem'>{row['涨跌幅']:+.2f}%</div>"
                        f"<div style='color:var(--fa-muted);font-size:0.78rem'>热度第{idx + 1}名</div>",
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
    if st.button("←", key=f"detail_back_{symbol}_{market}", type="tertiary", help="返回自选股"):
        for k in ("_detail_symbol", "_detail_market", "_detail_name", "_detail_module"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "自选股"
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
        c_end = datetime.now().strftime("%Y%m%d")
        c_start = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
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
        "价格是每 15 秒跳动的实时数据，但AI文字分析生成一次就缓存住，不会跟着"
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
                st.session_state[summary_key] = _stream_overall_summary(summarize_overall(symbol, section_texts))
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
        <div style='background:#e02020;margin:-1rem -1rem 0 -1rem;padding:14px 24px'>
            <div style='color:#fff;font-size:1.2rem;font-weight:700'>{name}</div>
            <div style='color:#fff;font-size:0.85rem;opacity:0.85'>{code} · {market}指数</div>
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
                ai_text = _write_stream_safe(summarize_index_news(name, news_summary))
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
            try:
                ai_text = _write_stream_safe(analyze_index(name, technical_summary, news_summary))
            except Exception as e:
                st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": f"分析失败：{e}"}
                st.error(f"分析失败：{e}")
                return
            st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": ai_text}
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
                st.session_state[idx_summary_key] = _stream_overall_summary(summarize_overall(name, section_texts))
            except Exception as e:
                st.session_state[idx_summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[idx_summary_key])


@st.fragment(run_every=3)
def _render_watchlist_rows(watched_filtered: list, _email: str):
    """自选股列表本体单独做成 fragment，价格/涨跌幅每3秒自己刷新，效仿长桥的
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
    if not watched_filtered:
        st.caption("这个分类下暂时没有自选股。")
        return

    st.caption("每 3 秒自动刷新")

    st.markdown(
        _PRICE_FLASH_CSS
        + "<style>"
        # 浏览器默认的 a:link/a:visited 样式（蓝色+下划线）选择器带伪类，
        # 优先级比单纯的class选择器高，必须用!important才能真正覆盖掉。
        + "a.wl-card-link, a.wl-card-link:link, a.wl-card-link:visited {"
        + "  text-decoration: none !important; color: inherit !important;"
        + "  display: block; cursor: pointer;"
        + "}"
        + "a.wl-card-link:hover { opacity: 0.85; }"
        # 删除键用type="tertiary"去掉了方框，但图标本身默认偏小，用户反馈要
        # 大一点、位置要跟卡片内容对齐。垂直对齐交给st.columns自己的
        # vertical_alignment="center"处理（原生机制，比猜CSS高度靠谱），
        # 这里只负责放大字号，用 key 生成的 st-key-* class精确定位。
        + "[class*='st-key-wl_del_'] button p { font-size: 1.5rem !important; font-weight: 700; margin: 0; }"
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
        # 用户反馈"自选股加载好慢"——几只股票乘以两次网络请求累加起来确实慢。
        # A股走BaoStock/akshare，内部各自有全局锁保证线程安全，并发提交时这
        # 部分本来就会排队，不会因为并发就变快；但港股/美股走Futu，现在走的是
        # 单一常驻worker线程+队列（见data_sources.py的_futu_call），单次查询
        # 本身只要零点几秒，并发提交多只互不阻塞。用线程池把每只股票的取数
        # 并发起来，A股之间该排队还是排队，但A股和港股/美股之间、以及港股/
        # 美股彼此之间不用再互相等，混合市场的自选股整体加载时间能明显缩短。
        # ex.map保序，不会打乱原来的展示顺序。
        with ThreadPoolExecutor(max_workers=min(8, len(watched_filtered))) as ex:
            return list(ex.map(_fetch_one, watched_filtered))

    # 这个fragment每3秒自动刷新一次——只有真正第一次加载（session里还没有
    # 任何一次成功渲染过）才显示"加载中"，之后的静默自动刷新不再包一层
    # spinner：之前每次刷新都会先弹一下spinner再画出列表，整个列表跟着
    # 抖一下，跟_render_price_header那套"数字变了背景轻轻一闪"的丝滑感
    # 完全相反。改成只有首次展示这一遭才等得起spinner，后续刷新静默取数，
    # 取完直接原地重画，观感上就是"数字自己跳动"而不是"列表重绘"。
    if not st.session_state.get("_wl_seen_once"):
        with st.spinner("加载中..."):
            _rows_data = _collect_rows()
        st.session_state["_wl_seen_once"] = True
    else:
        _rows_data = _collect_rows()

    for item, item_market, symbol, wspot, closes in _rows_data:
        spark_color = "#999"
        if wspot and wspot.get("最新价") and wspot.get("昨收"):
            spark_color = UP_COLOR if wspot["最新价"] >= wspot["昨收"] else DOWN_COLOR

        # 名称+走势图这部分每3秒刷新时几乎不变（迷你图数据本身缓存了好几分钟，
        # 涨跌方向短期内也很少翻转），但之前跟价格/涨跌幅拼进同一个markdown
        # 字符串——价格每次都变，导致这一整块（含SVG）每3秒都要重新生成、
        # 重新发给前端重绘，是watchlist"感觉卡顿"的一部分原因。缓存住SVG
        # 字符串，输入不变就直接复用，减少每次刷新真正要重绘的内容量。
        spark_key = f"_wl_spark_{symbol}_{item_market}"
        spark_cache = st.session_state.get(spark_key)
        closes_tuple = tuple(closes)
        if spark_cache is not None and spark_cache[0] == closes_tuple and spark_cache[1] == spark_color:
            spark_svg = spark_cache[2]
        else:
            spark_svg = _build_sparkline_svg(closes, spark_color)
            st.session_state[spark_key] = (closes_tuple, spark_color, spark_svg)

        if wspot and wspot.get("最新价"):
            wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
            wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
            color = UP_COLOR if wchange >= 0 else DOWN_COLOR

            flash_key = f"_wl_last_price_{symbol}_{item_market}"
            prev = st.session_state.get(flash_key)
            st.session_state[flash_key] = wspot["最新价"]
            flash_class = ""
            if prev is not None and prev != wspot["最新价"]:
                flash_class = "price-flash-up" if wspot["最新价"] > prev else "price-flash-down"

            price_html = (
                f"<div class='{flash_class}' style='text-align:right;border-radius:4px'>"
                f"<div style='font-weight:600;color:{color}'>{wspot['最新价']:.2f}</div>"
                f"<div style='font-size:0.72rem;color:var(--fa-muted)'>{_fmt_turnover(wspot.get('成交额'))}</div>"
                f"</div>"
            )
            badge_html = (
                f"<div style='text-align:right'>"
                f"<span style='background:{color};color:#fff;font-size:0.78rem;font-weight:600;"
                f"padding:3px 7px;border-radius:5px;display:inline-block;min-width:58px;text-align:center'>"
                f"{wchange_pct:+.2f}%</span></div>"
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
                f"&open_from=wl"
                f"{_auth_qs()}"
            )
            # 名称+走势图（静态部分）和价格+涨跌幅（动态部分）拆成两个独立的
            # st.markdown调用——同一个href两边都能点，视觉上还是整行可点，
            # 但静态部分的HTML字符串在数据没变时保持不变，Streamlit的diff能
            # 跳过它不用每3秒都重绘，只有动态部分真正需要每次刷新。
            static_col.markdown(
                f"<a class='wl-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                # 颜色直接写在这个div自己身上，不靠继承父级<a>的color——之前靠
                # a.wl-card-link{{color:inherit!important}}死活压不过浏览器
                # 默认的a:link蓝色，元素自己的inline style优先级天然最高，不用
                # 再跟CSS特异性较劲。
                f"<div style='flex:2.1;font-weight:600;color:var(--fa-text);text-decoration:none'>{item['name']}（{symbol}）</div>"
                f"<div style='flex:1.1;display:flex;justify-content:center'>{spark_svg}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )
            dynamic_col.markdown(
                f"<a class='wl-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                f"<div style='flex:1.3'>{price_html}</div>"
                f"<div style='flex:1'>{badge_html}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )
            if del_col.button("×", key=f"wl_del_{symbol}", help="删除自选", type="tertiary"):
                _confirm_delete_dialog(_email, symbol, item["name"])


@st.dialog("确认删除")
def _confirm_delete_dialog(email: str, symbol: str, name: str):
    st.write(f"确定要把「{name}」（{symbol}）从自选股里删除吗？")
    dc1, dc2 = st.columns(2)
    if dc1.button("确认删除", type="primary", use_container_width=True):
        remove_from_watchlist(email, symbol)
        st.rerun()
    if dc2.button("取消", use_container_width=True):
        st.rerun()


def _do_add_watchlist(email: str, q: str, market_code: str) -> bool:
    """真正执行添加的公共逻辑，搜索弹窗里"添加"按钮和历史记录"再加"按钮共用。
    成功才记一笔搜索历史（失败的搜索没必要占历史记录的位置）。
    """
    q = q.strip()
    if not q:
        return False
    add_symbol = _resolve_add_symbol(q, market_code)
    if not add_symbol:
        st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return False
    # check_stock_valid 只覆盖 A 股（内部走 BaoStock，港美股没有等价数据源）——
    # 能提前区分"代码格式对但公司已退市"和"数据源临时故障"这两种情况，
    # 不再一律甩给用户一句含糊的"检查一下代码对不对"。
    if market_code == "A":
        valid, info = check_stock_valid(add_symbol)
        if not valid:
            st.error(info)
            return False
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
        return False
    add_to_watchlist(email, add_symbol, add_spot.get("名称", add_symbol), market=market_code)
    add_search_history(email, q, market_code)
    return True


@st.dialog("添加自选股")
def _show_add_watchlist_dialog(email: str):
    add_query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_wl_add_query_dialog")

    if st.button("添加", type="primary", use_container_width=True, key="_wl_add_btn_dialog") and add_query:
        # 不再让用户先选市场——大多数公司名字只在一个市场上市，自动判断就够了
        # （比如"苹果"只有美股）。只有像"阿里巴巴"这种港股美股都有的名字，
        # 才需要用户自己选，见下面的候选按钮。
        candidates = detect_symbol_candidates(add_query)
        if not candidates:
            st.error(f"没查到「{add_query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(candidates) == 1:
            if _do_add_watchlist(email, add_query, candidates[0]["market"]):
                st.rerun()
        else:
            st.session_state["_wl_add_candidates"] = candidates
            st.session_state["_wl_add_candidates_query"] = add_query

    if st.session_state.get("_wl_add_candidates"):
        cands = st.session_state["_wl_add_candidates"]
        cq = st.session_state.get("_wl_add_candidates_query", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(cands))
        for ccol, c in zip(cand_cols, cands):
            if ccol.button(
                f"{c['market_label']}（{c['symbol']}）", key=f"_wl_cand_{c['market']}_{c['symbol']}",
                use_container_width=True,
            ):
                if _do_add_watchlist(email, cq, c["market"]):
                    st.session_state.pop("_wl_add_candidates", None)
                    st.session_state.pop("_wl_add_candidates_query", None)
                    st.rerun()

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "A股", "HK": "港股", "US": "美股"}
        for h in history:
            row_label = f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）"
            # 点历史记录直接跳去那只股票的详情页，不是再加一遍自选——
            # 用户反馈"再加"这个按钮没必要，点了就想直接看那只股票。
            if st.button(row_label, key=f"_wl_hist_open_{h['id']}", use_container_width=True):
                sym = _resolve_add_symbol(h["query"], h["market"])
                if sym:
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = h["market"]
                    st.session_state["_detail_name"] = h["query"]
                    st.rerun()
                else:
                    st.error(f"没查到「{h['query']}」的行情。")


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
else:
    with _page_slot.container():
        with st.sidebar:
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
                    '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");}catch(e){}</script>',
                    height=1,
                )
                st.session_state["logged_in"] = False
                st.session_state.pop("user_email", None)
                st.rerun()

            st.divider()
            with st.expander("历史回看"):
                st.caption("每次点开个股「数据分析」时会记录当时价格和方向倾向，"
                           "满7天后自动补上现在的价格做对照。仅供参考，不是投资建议，"
                           "过去的方向一致率不代表未来表现。")
                due = get_due_for_review(_uemail, min_age_days=7)
                for item in due:
                    try:
                        spot = get_stock_realtime(item["symbol"], market=item.get("market", "A"))
                        if spot and spot.get("最新价"):
                            record_review(item["id"], float(spot["最新价"]))
                    except Exception:
                        continue

                stats = get_accuracy_stats(_uemail)
                if stats["总数"] > 0:
                    st.metric(
                        "方向一致率", f"{stats['一致率']:.0f}%",
                        help=f"过去 {stats['总数']} 次有方向判断的分析里，{stats['一致数']} 次跟事后价格走势一致",
                    )
                    # 按方向/按市场拆开看——笼统一个数字看不出"偏多判断准还是
                    # 偏空判断准""在哪个市场准"，样本太少（<3条）的分组百分比
                    # 波动大、参考意义不大，只展示总数够的分组，不硬凑显示。
                    _breakdown_cols = st.columns(2)
                    with _breakdown_cols[0]:
                        st.caption("按方向")
                        for _v, _s in stats.get("按方向", {}).items():
                            if _s["总数"] >= 3:
                                st.markdown(f"{_v}：{_s['一致率']:.0f}%（{_s['总数']}次）")
                            elif _s["总数"] > 0:
                                st.markdown(f"{_v}：样本太少（{_s['总数']}次），暂不统计")
                    with _breakdown_cols[1]:
                        st.caption("按市场")
                        _market_label = {"A": "A股", "HK": "港股", "US": "美股"}
                        for _m, _s in stats.get("按市场", {}).items():
                            if _s["总数"] >= 3:
                                st.markdown(f"{_market_label.get(_m, _m)}：{_s['一致率']:.0f}%（{_s['总数']}次）")
                            elif _s["总数"] > 0:
                                st.markdown(f"{_market_label.get(_m, _m)}：样本太少（{_s['总数']}次），暂不统计")
                else:
                    st.caption("还没有满7天可回看的记录。")

                history = get_history(_uemail, limit=10)
                for h in history:
                    verdict_color = {"偏多": UP_COLOR, "偏空": DOWN_COLOR, "中性": NEUTRAL_COLOR}.get(h["verdict"], NEUTRAL_COLOR)
                    line = f"{h.get('name') or h['symbol']}（{h['symbol']}） {h['created_at'][:10]}"
                    st.markdown(
                        f"<div style='font-size:0.78rem;margin:6px 0'>{line}　"
                        f"<span style='color:{verdict_color}'>{h['verdict']}</span>　"
                        f"当时{h['price_at_analysis']:.2f}"
                        + (f" → 现在{h['review_price']:.2f}" if h.get("review_price") else "（未到7天）")
                        + "</div>",
                        unsafe_allow_html=True,
                    )

            with st.expander("应用指南"):
                st.markdown(
                    "**定位**\n\n"
                    "Invest Agent 是一个多市场（A股/港股/美股）行情查询和数据交叉验证工具，"
                    "把行情、财务、新闻这几类原始数据放在一起给你看，AI 只做交叉核对和总结，"
                    "不做黑箱荐股，不直接给买卖判断。\n\n"
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
                    "**自选股**\n\n"
                    "右上角放大镜可以按代码或名称搜索添加，支持按市场筛选，"
                    "卡片显示迷你走势图和实时涨跌，点卡片进详情页，点 × 删除。\n\n"
                    "**历史回看**\n\n"
                    "每次生成「综合数据分析」时会记录当时价格和 AI 判断的方向倾向，"
                    "满 7 天后自动补录当时的价格做对照，统计一个方向一致率——"
                    "这是历史记录的客观统计，不代表未来表现，不是胜率承诺。\n\n"
                    "**重要说明**\n\n"
                    "本应用所有分析、评分、资讯摘要仅基于公开数据的整理和交叉核对，"
                    "不构成任何投资建议，不保证数据的完整性和及时性，据此操作的风险自负。"
                )

        st.markdown(
            """
            <div class='fa-flex-row' style='background:#e02020;margin:-1rem -1rem 0 -1rem;padding:14px 24px;
                        display:flex;align-items:center;justify-content:space-between'>
                <span style='color:#fff;font-size:1.3rem;font-weight:700;letter-spacing:.02em'>Invest Agent</span>
                <span style='color:#fff;font-size:0.8rem;opacity:0.85'>行情 · 财务 · 新闻交叉验证</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


        # "行情"分区的快速搜索框去掉了——用户反馈是累赘（"自选股"分区里
        # "新增自选股"自己就有搜索框，两边都放显得重复）。指数/个股的浏览
        # 入口保留在下面的指数卡片列表和涨跌幅排行榜里。

        # 用 radio 手动实现 tab 切换，不用 st.tabs()——st.tabs() 选中哪个是纯前端状态，
        # 代码控制不了；从自选股点进详情页再返回时，需要能把选中项强制拨回"自选股"。
        st.session_state.setdefault("_active_section", "行情")

        active_section = st.radio(
            "分区", ["行情", "自选股"], key="_active_section", horizontal=True, label_visibility="collapsed",
        )

        if active_section == "行情":
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

        elif active_section == "自选股":
            _email = st.session_state["user_email"]
            watched = get_watchlist(_email)

            st.markdown(
                "<style>"
                "[class*='st-key-wl_search_icon'] button {"
                "  display: flex; align-items: center; justify-content: center;"
                "  height: 100%; min-height: 44px;"
                "}"
                "[class*='st-key-wl_search_icon'] span[data-testid='stIconMaterial'] {"
                "  font-size: 1.6rem !important;"
                "}"
                "</style>",
                unsafe_allow_html=True,
            )
            title_col, search_col = st.columns([11, 1], vertical_alignment="center")
            if search_col.button("", icon=":material/search:", key="wl_search_icon", type="tertiary"):
                _show_add_watchlist_dialog(_email)

            if not watched:
                st.write("")
                _, mid_empty, _ = st.columns([1, 2, 1])
                with mid_empty:
                    st.markdown(
                        "<div style='text-align:center;color:var(--fa-muted);padding:20px 0 10px'>"
                        "还没有关注任何股票<br>"
                        "<span style='font-size:0.82rem'>点右上角的搜索按钮添加</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )

            if watched:
                # 市场筛选固定显示"全部/A股/港股/美股"四个选项——不管当前自选股
                # 里有没有对应市场的股票，选项本身应该是稳定的，不随内容忽隐忽现。
                wl_market_tab = st.radio(
                    "市场筛选", ["全部", "A股", "港股", "美股"],
                    key="_wl_market_tab", horizontal=True, label_visibility="collapsed",
                )
                _wl_code_to_label = {"A": "A股", "HK": "港股", "US": "美股"}
                watched_filtered = (
                    watched if wl_market_tab == "全部"
                    else [i for i in watched if _wl_code_to_label.get(i.get("market", "A")) == wl_market_tab]
                )
                _render_watchlist_rows(watched_filtered, _email)


