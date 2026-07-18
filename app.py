"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import os
import re
import urllib.parse
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
    resolve_symbol_by_name,
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
        return "<span style='color:#ccc;font-size:0.7rem'>--</span>"
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
            color, zone = "#e02020", "偏多"
        elif score <= 35:
            color, zone = "#22a06b", "偏空"
        else:
            color, zone = "#888", "中性"
        st.markdown(
            f"<div style='margin-bottom:14px'>"
            + f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:6px'>"
            + f"<span style='font-size:1.6rem;font-weight:700;color:{color}'>{score}</span>"
            + f"<span style='font-size:0.85rem;color:#888'>/ 100 "
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
    """
    placeholder = st.empty()
    full_text = placeholder.write_stream(gen)
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
        for _, r in news.iterrows():
            _title = r["新闻标题"]
            _title_html = (
                f"<a href='{r.get('url', '')}' target='_blank' style='color:#0f172a;text-decoration:none'>{_title}</a>"
                if idx_clickable else f"<span style='color:#0f172a'>{_title}</span>"
            )
            st.markdown(
                f"<div style='margin:6px 0;font-size:0.9rem'>"
                f"<span style='color:#888;font-size:0.78rem'>{r.get('日期', '') or ''}</span>　"
                f"{_title_html}　"
                f"<span style='color:#888;font-size:0.75rem'>{r.get('分类', '')}</span>"
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
            f"<a href='{r.get('url', '')}' target='_blank' style='color:#0f172a;text-decoration:none'>{title}</a>"
            if clickable else f"<span style='color:#0f172a'>{title}</span>"
        )
        st.markdown(
            f"<div style='margin:6px 0;font-size:0.9rem'>"
            f"<span style='color:#888;font-size:0.78rem'>{date}</span>　"
            f"{title_html}　"
            f"<span style='color:#888;font-size:0.75rem'>{tag}</span>"
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
                ai_text = st.write_stream(summarize_news(symbol, news_summary))
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
                    ai_text = st.write_stream(summarize_financials(symbol, financial_summary))
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
                    ai_text = st.write_stream(summarize_benchmark(symbol, stock_pct, bm_name, bm_pct))
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
                ai_text = st.write_stream(
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


def _inject_auto_refresh(seconds: int, key: str):
    """定时自动刷新——分时价格/图表这些字段缓存TTL就20-30秒，光靠用户手动交互
    触发rerun的话，数字看着就像"点进来那一刻定住了"。用JS定时器点一个隐藏按钮
    触发rerun，配合后端缓存TTL自然过期重新拉数据，效果上数字就会自己动起来。
    """
    marker = f"自动刷新-{key}"
    if st.button(marker, key=f"_autorefresh_trigger_{key}"):
        pass
    _cv1.html(
        f"""
        <script>
        (function() {{
            function bind(attemptsLeft) {{
                const doc = window.parent.document;
                const buttons = Array.from(doc.querySelectorAll('button'));
                const hiddenBtn = buttons.find(function(b) {{ return b.innerText.trim() === "{marker}"; }});
                if (hiddenBtn) {{
                    const wrap = hiddenBtn.closest('[data-testid="stButton"]');
                    if (wrap) wrap.style.display = 'none';
                    const flagKey = "_autorefresh_timer_{key}";
                    if (window.parent[flagKey]) {{ clearInterval(window.parent[flagKey]); }}
                    window.parent[flagKey] = setInterval(function() {{ hiddenBtn.click(); }}, {seconds * 1000});
                }} else if (attemptsLeft > 0) {{
                    setTimeout(function() {{ bind(attemptsLeft - 1); }}, 200);
                }}
            }}
            bind(15);
        }})();
        </script>
        """,
        height=0,
    )


_PRICE_FLASH_CSS = (
    "<style>"
    "@keyframes priceFlashUp { 0% { background: rgba(224,32,32,0.28); } 100% { background: transparent; } }"
    "@keyframes priceFlashDown { 0% { background: rgba(34,160,107,0.28); } 100% { background: transparent; } }"
    ".price-flash-up { animation: priceFlashUp 1.4s ease-out; }"
    ".price-flash-down { animation: priceFlashDown 1.4s ease-out; }"
    "</style>"
)


@st.fragment(run_every=15)
def _render_price_header(symbol: str, market: str):
    """价格区块单独做成 fragment，每15秒自己刷新，不带动AI模块、新闻这些重的部分
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
    color = "#e02020" if change >= 0 else "#22a06b"

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
    st.caption(f"{_src} · {spot.get('更新时间', '-')} · 每 15 秒自动刷新")
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


@st.fragment(run_every=15)
def _render_index_price_header(name: str, market: str):
    """指数版的实时价格区块，逻辑跟_render_price_header一样，独立的 fragment。"""
    try:
        idx_snap = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
    except Exception:
        idx_snap = None
    if not idx_snap:
        st.caption("实时行情暂时取不到。")
        return

    color = "#e02020" if idx_snap["涨跌"] >= 0 else "#22a06b"
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
    st.caption("每 15 秒自动刷新")


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
    """
    _inject_wl_card_css()
    for _, row in df.iterrows():
        mv_symbol = str(row["代码"])
        mv_color = "#e02020" if row["涨跌幅"] >= 0 else "#22a06b"
        href = (
            f"?open_symbol={urllib.parse.quote(mv_symbol)}"
            f"&open_market={urllib.parse.quote(market)}"
            f"&open_name={urllib.parse.quote(str(row['名称']))}"
            f"{_auth_qs()}"
        )
        with st.container(border=True):
            st.markdown(
                f"<a class='wl-card-link' href='{href}' target='_self'>"
                f"<div style='display:flex;align-items:center'>"
                f"<div style='flex:2;font-weight:600;color:#0f172a;text-decoration:none'>"
                f"{row['名称']}（{mv_symbol}）</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{mv_color}'>{row['最新价']:.2f}</div>"
                f"<div style='flex:1;text-align:right;color:{mv_color}'>{row['涨跌幅']:+.2f}%</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


def _render_index_top_movers(market: str):
    """指数详情页的"成分股"板块——不是严格的官方成分股清单，是这个市场里
    涨幅最大的一批股票（get_index_top_movers 的说明里有详细原因：A股几百上千
    只成分股没法全拉一遍实时行情，港股/美股也没找到带股票代码的免费成分股源）。
    默认显示前10，点"展开"再显示到前30，卡片点击直接跳去那只股票的详情页。
    """
    try:
        movers = get_index_top_movers(market, limit=30)
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


def _render_stock_detail(symbol: str, market: str, name: str):
    _inject_auto_refresh(30, f"stock_{symbol}_{market}")
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

    st.divider()
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
    _inject_auto_refresh(30, f"index_{code}_{market}")
    if st.button("返回", key="idx_back"):
        for k in ("_index_detail_code", "_index_detail_market", "_index_detail_name"):
            st.session_state.pop(k, None)
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

    st.divider()
    st.subheader("成分股")
    _render_index_top_movers(market)

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
                ai_text = st.write_stream(summarize_index_news(name, news_summary))
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
                ai_text = st.write_stream(analyze_index(name, technical_summary, news_summary))
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


@st.fragment(run_every=15)
def _render_watchlist_rows(watched_filtered: list, _email: str):
    """自选股列表本体单独做成 fragment，价格/涨跌幅每15秒自己刷新，效仿长桥的
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
    _head_content_col, _head_del_col = st.columns([9, 1])
    _head_content_col.markdown(
        "<div style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:#888'>"
        "<div style='flex:2.1'>名称/代码</div>"
        "<div style='flex:1.1;text-align:center'>走势</div>"
        "<div style='flex:1.3;text-align:right'>最新/成交额</div>"
        "<div style='flex:1;text-align:right'>涨跌幅</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # 先把所有行的数据一次性取完（不带任何渲染），再统一画出来——之前是
    # 边取数据边画一行，用户反馈"一个一个蹦出来很慢"。取数据本身的耗时省不掉
    # （网络请求），但至少不会让用户看着页面一行一行往外挤，而是等一下之后
    # 整批一起出现，观感上干脆很多。
    _rows_data = []
    with st.spinner("加载中..."):
        for item in watched_filtered:
            item_market = item.get("market", "A")
            symbol = item["symbol"]
            try:
                wspot = get_stock_realtime(symbol, market=item_market)
            except Exception:
                wspot = {}
            closes = _fetch_sparkline_closes(symbol, item_market)
            _rows_data.append((item, item_market, symbol, wspot, closes))

    for item, item_market, symbol, wspot, closes in _rows_data:
        spark_color = "#999"
        if wspot and wspot.get("最新价") and wspot.get("昨收"):
            spark_color = "#e02020" if wspot["最新价"] >= wspot["昨收"] else "#22a06b"
        spark_svg = _build_sparkline_svg(closes, spark_color)

        if wspot and wspot.get("最新价"):
            wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
            wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
            color = "#e02020" if wchange >= 0 else "#22a06b"

            flash_key = f"_wl_last_price_{symbol}_{item_market}"
            prev = st.session_state.get(flash_key)
            st.session_state[flash_key] = wspot["最新价"]
            flash_class = ""
            if prev is not None and prev != wspot["最新价"]:
                flash_class = "price-flash-up" if wspot["最新价"] > prev else "price-flash-down"

            price_html = (
                f"<div class='{flash_class}' style='text-align:right;border-radius:4px'>"
                f"<div style='font-weight:600;color:{color}'>{wspot['最新价']:.2f}</div>"
                f"<div style='font-size:0.72rem;color:#999'>{_fmt_turnover(wspot.get('成交额'))}</div>"
                f"</div>"
            )
            badge_html = (
                f"<div style='text-align:right'>"
                f"<span style='background:{color};color:#fff;font-size:0.78rem;font-weight:600;"
                f"padding:3px 7px;border-radius:5px;display:inline-block;min-width:58px;text-align:center'>"
                f"{wchange_pct:+.2f}%</span></div>"
            )
        else:
            price_html = "<div style='text-align:right;color:#999'>—</div>"
            badge_html = ""

        with st.container(border=True):
            link_col, del_col = st.columns([9, 1], vertical_alignment="center")
            href = (
                f"?open_symbol={urllib.parse.quote(symbol)}"
                f"&open_market={urllib.parse.quote(item_market)}"
                f"&open_name={urllib.parse.quote(item['name'])}"
                f"&open_from=wl"
                f"{_auth_qs()}"
            )
            link_col.markdown(
                f"<a class='wl-card-link' href='{href}' target='_self'>"
                f"<div style='display:flex;align-items:center'>"
                # 颜色直接写在这个div自己身上，不靠继承父级<a>的color——之前靠
                # a.wl-card-link{{color:inherit!important}}死活压不过浏览器
                # 默认的a:link蓝色，元素自己的inline style优先级天然最高，不用
                # 再跟CSS特异性较劲。
                f"<div style='flex:2.1;font-weight:600;color:#0f172a;text-decoration:none'>{item['name']}（{symbol}）</div>"
                f"<div style='flex:1.1;display:flex;justify-content:center'>{spark_svg}</div>"
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
    try:
        add_spot = get_stock_realtime(add_symbol, market=market_code)
    except Exception:
        add_spot = {}
    if not add_spot or not add_spot.get("最新价"):
        st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return False
    add_to_watchlist(email, add_symbol, add_spot.get("名称", add_symbol), market=market_code)
    add_search_history(email, q, market_code)
    return True


@st.dialog("添加自选股")
def _show_add_watchlist_dialog(email: str):
    add_query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_wl_add_query_dialog")
    add_market_label = st.selectbox("市场", ["A股", "港股", "美股"], key="_wl_add_market_dialog")
    if st.button("添加", type="primary", use_container_width=True, key="_wl_add_btn_dialog") and add_query:
        add_market_code = {"A股": "A", "港股": "HK", "美股": "US"}[add_market_label]
        if _do_add_watchlist(email, add_query, add_market_code):
            st.rerun()

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "A股", "HK": "港股", "US": "美股"}
        for h in history:
            hcol1, hcol2 = st.columns([4, 1])
            hcol1.write(f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）")
            if hcol2.button("再加", key=f"_wl_hist_add_{h['id']}"):
                if _do_add_watchlist(email, h["query"], h["market"]):
                    st.rerun()


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
                else:
                    st.caption("还没有满7天可回看的记录。")

                history = get_history(_uemail, limit=10)
                for h in history:
                    verdict_color = {"偏多": "#e02020", "偏空": "#22a06b", "中性": "#888"}.get(h["verdict"], "#888")
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
                    "价格每 15 秒自动刷新一次。\n\n"
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
            <div style='background:#e02020;margin:-1rem -1rem 0 -1rem;padding:14px 24px;
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
            mkt_pick = st.radio("市场", ["A股", "港股", "美股"], horizontal=True, key="_market_overview_pick")
            mkt_code = {"A股": "A", "港股": "HK", "美股": "US"}[mkt_pick]

            try:
                idx_list = get_multi_index_snapshot(mkt_code)
            except Exception:
                idx_list = []

            _idx_code_by_name = dict(_MULTI_INDICES.get(mkt_code, []))

            if idx_list:
                st.markdown(
                    "<style>"
                    "a.idx-card-link, a.idx-card-link:link, a.idx-card-link:visited {"
                    "  text-decoration: none !important; color: inherit !important;"
                    "  display: block; cursor: pointer;"
                    "}"
                    "a.idx-card-link:hover { opacity: 0.85; }"
                    "</style>"
                    "<div style='display:flex;padding:4px 8px;font-size:0.78rem;color:#888'>"
                    "<div style='flex:2.4'>指数</div>"
                    "<div style='flex:1;text-align:right'>最新</div>"
                    "<div style='flex:1;text-align:right'>涨幅</div>"
                    "<div style='flex:1;text-align:right'>涨跌</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
                for idx in idx_list:
                    color = "#e02020" if idx["涨跌"] >= 0 else "#22a06b"
                    idx_code = _idx_code_by_name.get(idx["名称"], "")
                    href = (
                        f"?open_index_code={urllib.parse.quote(idx_code)}"
                        f"&open_index_market={urllib.parse.quote(mkt_code)}"
                        f"&open_index_name={urllib.parse.quote(idx['名称'])}"
                        f"{_auth_qs()}"
                    )
                    with st.container(border=True):
                        st.markdown(
                            f"<a class='idx-card-link' href='{href}' target='_self'>"
                            f"<div style='display:flex;align-items:center'>"
                            f"<div style='flex:2.4;font-weight:600;color:#0f172a;text-decoration:none'>{idx['名称']}</div>"
                            f"<div style='flex:1;text-align:right;font-weight:600;color:{color}'>{idx['最新']:,.2f}</div>"
                            f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
                            f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌']:+.2f}</div>"
                            f"</div></a>",
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

            elif mkt_code == "HK":
                try:
                    south = get_southbound_flow()
                except Exception:
                    south = None
                if south:
                    _s_color = "#e02020" if south["净买额"] >= 0 else "#22a06b"
                    st.markdown(
                        f"<div style='margin:4px 0 12px'>南向资金净买额　"
                        f"<span style='color:{_s_color};font-weight:700;font-size:1.2rem'>"
                        f"{south['净买额']:+.2f}亿</span></div>",
                        unsafe_allow_html=True,
                    )
                st.markdown("**港股核心股（按热度排）**")
                try:
                    with st.spinner("加载中（第一次会慢一些）..."):
                        hk_movers = get_hk_famous_movers(15)
                    if hk_movers is not None and not hk_movers.empty:
                        _render_stock_movers_cards(hk_movers, "HK")
                    else:
                        st.caption("暂时获取不到数据。")
                except Exception as e:
                    st.caption(f"获取失败：{e}")

            else:
                st.markdown("**美股核心股**")
                try:
                    us_movers = get_us_famous_movers(15)
                    if us_movers is not None and not us_movers.empty:
                        _render_stock_movers_cards(us_movers, "US")
                    else:
                        st.caption("暂时获取不到数据。")
                except Exception as e:
                    st.caption(f"获取失败：{e}")

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
                        "<div style='text-align:center;color:#888;padding:20px 0 10px'>还没有关注任何股票</div>",
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


