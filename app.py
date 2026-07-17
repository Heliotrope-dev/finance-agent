"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import json
import os
import re
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
    get_southbound_flow,
    get_us_famous_movers,
    resolve_symbol_by_name,
)
from analysis import (
    cross_validate, summarize_financials, summarize_news, summarize_benchmark,
    extract_verdict, analyze_index, summarize_overall, extract_score,
)
from tracker import (
    log_analysis, get_history, get_due_for_review, record_review, get_accuracy_stats,
    add_to_watchlist, remove_from_watchlist, is_in_watchlist, get_watchlist,
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
    """AI 模块按需加载：每个模块独立缓存，点开哪个才跑哪个的 AI 调用，不会一次性全跑。"""
    mod_key = f"_detail_mod_{symbol}_{market}_{module}"
    if mod_key not in st.session_state:
        with st.spinner("分析中..."):
            try:
                if module == "news":
                    stock_name = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
                    news, _ = _fetch_news_items(stock_name, symbol, market)
                    news_summary = _news_to_summary(news)
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
                    news, _ = _fetch_news_items(stock_name, symbol, market)
                    news_summary = _news_to_summary(news)

                    try:
                        _intraday_for_signal = (
                            get_stock_intraday_a(symbol) if market == "A" else get_stock_intraday_futu(symbol, market)
                        )
                    except Exception:
                        _intraday_for_signal = None
                    realtime_signal = compute_realtime_signal(spot, _intraday_for_signal)
                    technical_summary = compute_technical_signal(hist) + " 【盘中实时信号】" + realtime_signal
                    ai_text = cross_validate(symbol, history_summary, financial_summary, news_summary, technical_summary)
                    current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
                    verdict = extract_verdict(ai_text)
                    stock_name = spot.get("名称", symbol) if spot else symbol
                    log_analysis(
                        st.session_state["user_email"], symbol, float(current_price), ai_text,
                        verdict=verdict, market=market, name=stock_name,
                    )
                    st.session_state[mod_key] = {
                        "ai_text": ai_text, "stats": stats, "technical_summary": technical_summary,
                    }
            except Exception as e:
                st.error(f"分析失败：{e}")
                return

    data = st.session_state[mod_key]

    if module == "news":
        # 原始新闻列表已经在页面上方单独一块展示了（_render_news_section），
        # 这里不重复摆一次，只放AI解读，避免同一份数据在页面上出现两遍。
        st.markdown(data["ai_text"])
    elif module == "financial":
        if data["fin"] is not None and not data["fin"].empty:
            st.dataframe(data["fin"], use_container_width=True, hide_index=True)
            if data["ai_text"]:
                st.caption("AI 解读")
                st.markdown(data["ai_text"])
        else:
            st.caption("暂无财务数据。")
    elif module == "benchmark":
        if data["benchmark"] is not None and not data["benchmark"].empty:
            st.plotly_chart(
                build_benchmark_comparison(hist, data["benchmark"], benchmark_name=data["bm_name"]),
                use_container_width=True,
            )
            st.caption("AI 解读")
            st.markdown(data["ai_text"])
        else:
            st.caption("基准数据暂时获取不到。")
    else:
        stats = data.get("stats") or {}
        if stats:
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", stats.get("区间收益率", "—"))
            scol2.metric("年化波动率", stats.get("年化波动率", "—"))
            scol3.metric("最大回撤", stats.get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", stats.get("夏普比率(简化)", "—"))
        if data.get("technical_summary"):
            st.markdown(f"**技术面信号**：{data['technical_summary']}")
        if hist is not None and not hist.empty:
            st.plotly_chart(build_return_histogram(hist), use_container_width=True)
        st.caption("AI 解读（交叉验证消息面、财务、技术面是否一致）")
        st.markdown(data["ai_text"])


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


@st.fragment(run_every=8)
def _render_price_header(symbol: str, market: str):
    """价格区块单独做成 fragment，每8秒自己刷新，不带动AI模块、新闻这些重的部分
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
    st.caption(f"{_src} · {spot.get('更新时间', '-')} · 每 8 秒自动刷新")
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


@st.fragment(run_every=8)
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
    st.caption("每 8 秒自动刷新")


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
    st.subheader("AI 深度分析")
    st.caption(
        "打开详情页自动生成，多个独立 AI 调用分别交叉验证新闻、财务、大盘对比、"
        "技术面与消息面是否一致——只呈现数据和依据，不给买卖建议，请自行判断。"
    )
    module_defs = (
        ("news", "资讯解读"), ("financial", "财务摘要"), ("benchmark", "对比大盘"), ("cross", "综合数据分析（交叉验证）"),
    )
    for mod_key, mod_label in module_defs:
        with st.container(border=True):
            st.markdown(f"**{mod_label}**")
            _render_module(mod_key, symbol, market, hist, spot)

    summary_key = f"_detail_summary_{symbol}_{market}"
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if summary_key not in st.session_state:
            with st.spinner("汇总中..."):
                try:
                    section_texts = {
                        mod_label: st.session_state.get(f"_detail_mod_{symbol}_{market}_{mod_key}", {}).get("ai_text", "")
                        for mod_key, mod_label in module_defs
                    }
                    st.session_state[summary_key] = summarize_overall(symbol, section_texts)
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
    _render_news_section(name, is_index=True)

    st.divider()
    st.subheader("AI 深度分析")
    st.caption("打开详情页自动生成，结合技术面信号和相关资讯做交叉验证——只呈现依据，不给操作建议。")

    idx_ai_key = f"_idx_analysis_{code}_{market}"
    with st.container(border=True):
        st.markdown("**资讯解读**")
        if f"{idx_ai_key}_news" not in st.session_state:
            with st.spinner("分析中..."):
                try:
                    news, _ = get_index_news(name, limit=8)
                    news_summary = _news_to_summary(news)
                    ai_text = summarize_news(name, news_summary)
                    st.session_state[f"{idx_ai_key}_news"] = {"ai_text": ai_text, "summary": news_summary}
                except Exception as e:
                    st.session_state[f"{idx_ai_key}_news"] = {"ai_text": f"获取失败：{e}", "summary": "无相关新闻"}
        st.markdown(st.session_state[f"{idx_ai_key}_news"]["ai_text"])

    with st.container(border=True):
        st.markdown("**综合数据分析**")
        if f"{idx_ai_key}_cross" not in st.session_state:
            with st.spinner("分析中..."):
                try:
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
                    news_summary = st.session_state.get(f"{idx_ai_key}_news", {}).get("summary", "无相关新闻")
                    ai_text = analyze_index(name, technical_summary, news_summary)
                    st.session_state[f"{idx_ai_key}_cross"] = {
                        "ai_text": ai_text, "stats": stats, "technical_summary": technical_summary,
                        "daily_hist": daily_hist if has_hist else None,
                    }
                except Exception as e:
                    st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": f"分析失败：{e}", "stats": {}, "technical_summary": "", "daily_hist": None}
        cross_data = st.session_state[f"{idx_ai_key}_cross"]
        if cross_data.get("stats"):
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", cross_data["stats"].get("区间收益率", "—"))
            scol2.metric("年化波动率", cross_data["stats"].get("年化波动率", "—"))
            scol3.metric("最大回撤", cross_data["stats"].get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", cross_data["stats"].get("夏普比率(简化)", "—"))
        if cross_data.get("technical_summary"):
            st.markdown(f"**技术面信号**：{cross_data['technical_summary']}")
        if cross_data.get("daily_hist") is not None:
            st.plotly_chart(build_return_histogram(cross_data["daily_hist"]), use_container_width=True)
        st.caption("AI 解读")
        st.markdown(cross_data["ai_text"])

    idx_summary_key = f"{idx_ai_key}_summary"
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if idx_summary_key not in st.session_state:
            with st.spinner("汇总中..."):
                try:
                    section_texts = {
                        "资讯解读": st.session_state.get(f"{idx_ai_key}_news", {}).get("ai_text", ""),
                        "综合数据分析": st.session_state.get(f"{idx_ai_key}_cross", {}).get("ai_text", ""),
                    }
                    st.session_state[idx_summary_key] = summarize_overall(name, section_texts)
                except Exception as e:
                    st.session_state[idx_summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[idx_summary_key])


@st.fragment(run_every=8)
def _render_watchlist_rows(watched_filtered: list, _email: str):
    """自选股列表本体单独做成 fragment，价格/涨跌幅每8秒自己刷新，效仿长桥的
    紧凑列表样式：名称代码 + 迷你走势图 + 现价/成交额 + 涨跌幅色块。数字真变了
    背景闪一下（复用详情页那套red/green flash动画），列表长按删除手势不受影响
    （JS绑定逻辑本来就是幂等的，每次刷新重跑一遍不会重复绑定）。
    """
    if not watched_filtered:
        st.caption("这个分类下暂时没有自选股。")
        return

    st.markdown(
        _PRICE_FLASH_CSS
        + "<div style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:#888;border-bottom:1px solid #eee'>"
        + "<div style='flex:2.1'>名称/代码</div>"
        + "<div style='flex:1.1;text-align:center'>走势</div>"
        + "<div style='flex:1.3;text-align:right'>最新/成交额</div>"
        + "<div style='flex:1;text-align:right'>涨跌幅</div>"
        + "</div>",
        unsafe_allow_html=True,
    )

    wl_symbols = []
    for item in watched_filtered:
        item_market = item.get("market", "A")
        symbol = item["symbol"]
        try:
            wspot = get_stock_realtime(symbol, market=item_market)
        except Exception:
            wspot = {}

        name_col, spark_col, price_col, badge_col = st.columns([2.1, 1.1, 1.3, 1])
        row_label = f"{item['name']}（{symbol}）"
        if name_col.button(row_label, key=f"wl_open_{symbol}", use_container_width=True):
            st.session_state["_detail_symbol"] = symbol
            st.session_state["_detail_market"] = item_market
            st.session_state["_detail_name"] = item["name"]
            st.rerun()

        closes = _fetch_sparkline_closes(symbol, item_market)
        spark_color = "#999"
        if wspot and wspot.get("最新价") and wspot.get("昨收"):
            spark_color = "#e02020" if wspot["最新价"] >= wspot["昨收"] else "#22a06b"
        spark_col.markdown(
            f"<div style='display:flex;justify-content:center;padding-top:4px'>"
            f"{_build_sparkline_svg(closes, spark_color)}</div>",
            unsafe_allow_html=True,
        )

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

            price_col.markdown(
                f"<div class='{flash_class}' style='text-align:right;padding-top:2px;border-radius:4px'>"
                f"<div style='font-weight:600;color:{color}'>{wspot['最新价']:.2f}</div>"
                f"<div style='font-size:0.72rem;color:#999'>{_fmt_turnover(wspot.get('成交额'))}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            badge_col.markdown(
                f"<div style='text-align:right;padding-top:6px'>"
                f"<span style='background:{color};color:#fff;font-size:0.78rem;font-weight:600;"
                f"padding:3px 7px;border-radius:5px;display:inline-block;min-width:58px;text-align:center'>"
                f"{wchange_pct:+.2f}%</span></div>",
                unsafe_allow_html=True,
            )
        else:
            price_col.markdown(
                "<div style='text-align:right;padding-top:4px;color:#999'>—</div>", unsafe_allow_html=True
            )
            badge_col.markdown("")

        if st.button(f"长按删除-{symbol}", key=f"wl_lp_trigger_{symbol}"):
            _confirm_delete_dialog(_email, symbol, item["name"])

        wl_symbols.append(symbol)

    if wl_symbols:
        _cv1.html(
            f"""
            <script>
            (function() {{
                const symbols = {json.dumps(wl_symbols)};
                function bind(attemptsLeft) {{
                    const doc = window.parent.document;
                    const buttons = Array.from(doc.querySelectorAll('button'));
                    let allBound = true;
                    symbols.forEach(function(sym) {{
                        const marker = "长按删除-" + sym;
                        const hiddenBtn = buttons.find(function(b) {{ return b.innerText.trim() === marker; }});
                        const rowBtn = buttons.find(function(b) {{ return b.innerText.indexOf("（" + sym + "）") !== -1; }});
                        if (hiddenBtn) {{
                            const wrap = hiddenBtn.closest('[data-testid="stButton"]');
                            if (wrap) wrap.style.display = 'none';
                        }}
                        if (rowBtn && hiddenBtn) {{
                            if (!rowBtn.dataset.lpBound) {{
                                rowBtn.dataset.lpBound = "1";
                                let timer = null;
                                let fired = false;
                                const start = function() {{
                                    fired = false;
                                    timer = setTimeout(function() {{
                                        fired = true;
                                        hiddenBtn.click();
                                    }}, 3000);
                                }};
                                const cancel = function() {{
                                    if (timer) {{ clearTimeout(timer); timer = null; }}
                                }};
                                const end = function(e) {{
                                    if (fired) {{ e.preventDefault(); e.stopPropagation(); }}
                                    cancel();
                                }};
                                rowBtn.addEventListener('touchstart', start, {{passive: true}});
                                rowBtn.addEventListener('touchend', end);
                                rowBtn.addEventListener('touchmove', cancel);
                                rowBtn.addEventListener('mousedown', start);
                                rowBtn.addEventListener('mouseup', end);
                                rowBtn.addEventListener('mouseleave', cancel);
                            }}
                        }} else {{
                            allBound = false;
                        }}
                    }});
                    if (!allBound && attemptsLeft > 0) {{
                        setTimeout(function() {{ bind(attemptsLeft - 1); }}, 200);
                    }}
                }}
                bind(15);
            }})();
            </script>
            """,
            height=0,
        )


@st.dialog("确认删除")
def _confirm_delete_dialog(email: str, symbol: str, name: str):
    st.write(f"确定要把「{name}」（{symbol}）从自选股里删除吗？")
    dc1, dc2 = st.columns(2)
    if dc1.button("确认删除", type="primary", use_container_width=True):
        remove_from_watchlist(email, symbol)
        st.rerun()
    if dc2.button("取消", use_container_width=True):
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


        def _auto_detect_market(q: str) -> str | None:
            if re.match(r"^\d{6}$", q):
                return "A"
            if re.match(r"^\d{4,5}$", q):
                return "HK"
            if re.match(r"^[A-Za-z.]{1,6}$", q):
                return "US"
            return None


        # 用 radio 手动实现 tab 切换，不用 st.tabs()——st.tabs() 选中哪个是纯前端状态，
        # 代码控制不了；从自选股点进详情页再返回时，需要能把选中项强制拨回"自选股"。
        st.session_state.setdefault("_active_section", "行情")

        if st.session_state["_active_section"] == "行情":
            # 快速搜索只在"行情"分区显示——"自选股"分区已经有自己的"新增自选股"
            # 搜索框了，两个搜索框同时出现是重复的，用户明确反馈要去掉。
            qcol, bcol = st.columns([5, 1])
            quick_query = qcol.text_input(
                "快速搜索代码、指数名称或知名公司名称，直接进详情页",
                value="", key="_quick_search", placeholder="600519 / 00700 / AAPL / 腾讯 / 特斯拉 / 恒生指数",
            )
            if bcol.button("搜索", key="_quick_search_btn", use_container_width=True) and quick_query:
                q = quick_query.strip()
                idx_hit = None
                for _mkt, _idx_list in _MULTI_INDICES.items():
                    for _name, _code in _idx_list:
                        if q == _name or q.lower() == _name.lower():
                            idx_hit = (_name, _code, _mkt)
                            break
                    if idx_hit:
                        break
                if idx_hit:
                    idx_name, idx_code, idx_mkt = idx_hit
                    st.session_state["_index_detail_code"] = idx_code
                    st.session_state["_index_detail_market"] = idx_mkt
                    st.session_state["_index_detail_name"] = idx_name
                    st.rerun()
                else:
                    # 名称匹配优先——不然"Tesla"这种纯字母输入会被代码格式的正则先一步误判成
                    # "看着像美股代码"，根本轮不到名称匹配生效。
                    # A股名称匹配放在最前面——像宁德时代这种A+H两地上市的公司，用中文名搜
                    # 大概率是想找A股这一支（更常被交易/讨论），不能让港股那边的模糊搜索
                    # 抢先命中，把人带去一个新闻源覆盖不到、也不是本意的市场。
                    a_matches = []
                    try:
                        a_matches = search_stock_by_name(q)
                    except Exception:
                        pass
                    if a_matches:
                        sym, detected = a_matches[0]["code"], "A"
                    else:
                        name_hit = resolve_symbol_by_name(q, "HK") or resolve_symbol_by_name(q, "US")
                        if name_hit:
                            sym, detected = name_hit, ("HK" if name_hit.isdigit() else "US")
                        else:
                            detected = _auto_detect_market(q)
                            sym = (q.zfill(5) if detected == "HK" else (q.upper() if detected == "US" else q)) if detected else None
                    if detected is None:
                        st.warning("没识别出来——支持直接输代码、指数名称，或者知名公司的中英文名称（覆盖范围有限，查不到不代表没上市）。")
                    else:
                        st.session_state["_detail_symbol"] = sym
                        st.session_state["_detail_market"] = detected
                        st.session_state["_detail_name"] = sym
                        st.rerun()

        active_section = st.radio(
            "分区", ["行情", "自选股"], key="_active_section", horizontal=True, label_visibility="collapsed",
        )

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
                    "<div style='display:flex;padding:4px 8px;font-size:0.78rem;color:#888;border-bottom:1px solid #eee'>"
                    "<div style='flex:2.4'>指数</div>"
                    "<div style='flex:1;text-align:right'>最新</div>"
                    "<div style='flex:1;text-align:right'>涨幅</div>"
                    "<div style='flex:1;text-align:right'>涨跌</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
                for idx in idx_list:
                    name_col, num_col = st.columns([2.4, 3])
                    if name_col.button(idx["名称"], key=f"idx_open_{mkt_code}_{idx['名称']}", use_container_width=True):
                        st.session_state["_index_detail_code"] = _idx_code_by_name.get(idx["名称"], "")
                        st.session_state["_index_detail_market"] = mkt_code
                        st.session_state["_index_detail_name"] = idx["名称"]
                        st.rerun()
                    color = "#e02020" if idx["涨跌"] >= 0 else "#22a06b"
                    num_col.markdown(
                        f"<div style='display:flex;padding-top:8px'>"
                        f"<div style='flex:1;text-align:right;font-weight:600;color:{color}'>{idx['最新']:,.2f}</div>"
                        f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
                        f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌']:+.2f}</div>"
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

        elif active_section == "自选股":
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
                    add_query = addcol1.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_wl_add_query")
                    add_market_label = addcol2.selectbox("市场", ["A股", "港股", "美股"], key="_wl_add_market")
                    if addcol3.button("添加", key="_wl_add_btn", use_container_width=True) and add_query:
                        add_market_code = {"A股": "A", "港股": "HK", "美股": "US"}[add_market_label]
                        q = add_query.strip()
                        by_name = resolve_symbol_by_name(q, add_market_code)
                        if by_name:
                            add_symbol = by_name
                        else:
                            add_symbol = q.zfill(5) if add_market_code == "HK" else (q.upper() if add_market_code == "US" else q)
                        try:
                            add_spot = get_stock_realtime(add_symbol, market=add_market_code)
                        except Exception:
                            add_spot = {}
                        if not add_spot or not add_spot.get("最新价"):
                            st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
                        else:
                            add_to_watchlist(_email, add_symbol, add_spot.get("名称", add_symbol), market=add_market_code)
                            st.session_state["_show_wl_add"] = False
                            st.rerun()

            if watched:
                _wl_markets_present = sorted({item.get("market", "A") for item in watched})
                _wl_tab_labels = ["全部"] + [{"A": "A股", "HK": "港股", "US": "美股"}[m] for m in _wl_markets_present]
                if len(_wl_tab_labels) > 2:
                    wl_market_tab = st.radio(
                        "市场筛选", _wl_tab_labels, key="_wl_market_tab", horizontal=True, label_visibility="collapsed",
                    )
                else:
                    wl_market_tab = "全部"
                _wl_code_to_label = {"A": "A股", "HK": "港股", "US": "美股"}
                watched_filtered = (
                    watched if wl_market_tab == "全部"
                    else [i for i in watched if _wl_code_to_label.get(i.get("market", "A")) == wl_market_tab]
                )
                st.caption("长按股票 3 秒可删除自选 · 每 8 秒自动刷新")
                _render_watchlist_rows(watched_filtered, _email)


